#!/usr/bin/env python3
"""Keep a yum channel's repodata internally consistent, and prove it.

Why this exists (wheels-dev/wheels#3690)
----------------------------------------
repair-repodata.py rewrites primary/filelists/other in place and patches only
<checksum> and <size> in repomd.xml. That leaves three fields describing the
PRE-repair file: the payload's filename (createrepo_c names it after the
sha256 of the compressed file), <open-checksum> and <open-size>. dnf verifies
<checksum>, so installs keep working, but the same URL now serves different
bytes — a caching proxy or mirror holding the old object fails the checksum —
and anything that validates the uncompressed payload rejects the repo.

Commands
--------
  repodata-integrity.py normalize <channel-dir>
      Recompute every <data> entry in repodata/repomd.xml from the files on
      disk: rename each payload to <sha256>-<kind>.<ext>, and set <location
      href>, <checksum>, <size>, <open-checksum> and <open-size>. The stale
      file is removed. Idempotent: a consistent repo is left byte-identical.

  repodata-integrity.py verify <channel-dir>
      Self-check before publishing: every referenced file exists, its name
      carries its own sha256, and every checksum/size (compressed and open)
      matches the bytes. Exits 1 with one line per mismatch.

  repodata-integrity.py exists <channel-dir> <pkg> <version>
      Exit 0 if primary lists packages/<pkg>-<version>.<arch>.rpm, else 1. Used
      by the publish guard: a published version is immutable.
"""

import gzip
import hashlib
import os
import re
import sys

OPEN_DECODERS = {".gz": gzip.decompress}


def _repomd_path(channel):
    return os.path.join(channel, "repodata", "repomd.xml")


def _blocks(repomd):
    """(start, end, kind, body) for every <data type="..."> element."""
    for m in re.finditer(r'<data type="([A-Za-z0-9_]+)">(.*?)</data>', repomd, re.S):
        yield m.start(2), m.end(2), m.group(1), m.group(2)


def _field(body, tag):
    m = re.search(rf"<{tag}\b[^>]*>([^<]*)</{tag}>", body)
    return m.group(1) if m else None


def _set_field(body, tag, value):
    new, count = re.subn(rf"(<{tag}\b[^>]*>)[^<]*(</{tag}>)", lambda m: m.group(1) + value + m.group(2), body)
    if count != 1:
        raise SystemExit(f"ERROR: expected exactly one <{tag}> in a repomd <data> block, found {count}")
    return new


def _href(body):
    m = re.search(r'<location href="([^"]+)"', body)
    return m.group(1) if m else None


def _measure(path):
    raw = open(path, "rb").read()
    info = {"checksum": hashlib.sha256(raw).hexdigest(), "size": str(len(raw))}
    ext = os.path.splitext(path)[1]
    if ext in OPEN_DECODERS:
        opened = OPEN_DECODERS[ext](raw)
        info["open-checksum"] = hashlib.sha256(opened).hexdigest()
        info["open-size"] = str(len(opened))
    return info


def _expected_name(href, checksum):
    """repodata/<old-sha>-primary.xml.gz -> repodata/<checksum>-primary.xml.gz"""
    head, base = os.path.split(href)
    suffix = base.split("-", 1)[1] if re.match(r"^[0-9a-f]{32,128}-", base) else base
    return os.path.join(head, f"{checksum}-{suffix}")


def normalize(channel):
    path = _repomd_path(channel)
    repomd = open(path, encoding="utf-8").read()
    out, last, changes = [], 0, []
    for start, end, kind, body in _blocks(repomd):
        href = _href(body)
        if not href:
            raise SystemExit(f"ERROR: <data type=\"{kind}\"> has no <location href>")
        file_path = os.path.join(channel, href)
        if not os.path.isfile(file_path):
            raise SystemExit(f"ERROR: {kind}: {href} is referenced by repomd.xml but missing")
        info = _measure(file_path)
        new_href = _expected_name(href, info["checksum"])
        if new_href != href:
            os.replace(file_path, os.path.join(channel, new_href))
            body = body.replace(f'<location href="{href}"', f'<location href="{new_href}"')
            changes.append(f"{kind}: renamed {os.path.basename(href)} -> {os.path.basename(new_href)}")
        for tag in ("checksum", "size", "open-checksum", "open-size"):
            if tag not in info:
                continue
            current = _field(body, tag)
            if current is None:
                raise SystemExit(f"ERROR: {kind}: repomd.xml has no <{tag}>")
            if current != info[tag]:
                body = _set_field(body, tag, info[tag])
                changes.append(f"{kind}: {tag} {current[:16]} -> {info[tag][:16]}")
        out.append(repomd[last:start])
        out.append(body)
        last = end
    out.append(repomd[last:])
    new_repomd = "".join(out)
    if new_repomd != repomd:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_repomd)
    for line in changes:
        print(f"  normalized {line}")
    if not changes:
        print("  repodata already consistent; nothing to normalize")


def verify(channel):
    repomd = open(_repomd_path(channel), encoding="utf-8").read()
    problems, checked = [], 0
    for _, _, kind, body in _blocks(repomd):
        href = _href(body)
        file_path = os.path.join(channel, href or "")
        if not href or not os.path.isfile(file_path):
            problems.append(f"{kind}: referenced file {href!r} is missing")
            continue
        info = _measure(file_path)
        if os.path.basename(href) != os.path.basename(_expected_name(href, info["checksum"])):
            problems.append(f"{kind}: filename {os.path.basename(href)} does not carry its sha256 {info['checksum']}")
        for tag, actual in info.items():
            recorded = _field(body, tag)
            if recorded != actual:
                problems.append(f"{kind}: <{tag}> is {recorded} but the file measures {actual}")
        checked += 1
    if checked == 0:
        problems.append("repomd.xml lists no metadata components")
    for p in problems:
        print(f"::error::repodata self-check: {p}")
    if problems:
        return 1
    print(f"  repodata self-check passed ({checked} components: names, checksums and sizes match)")
    return 0


def exists(channel, pkg, version):
    repomd = open(_repomd_path(channel), encoding="utf-8").read()
    for _, _, kind, body in _blocks(repomd):
        if kind != "primary":
            continue
        raw = open(os.path.join(channel, _href(body)), "rb").read()
        text = gzip.decompress(raw).decode("utf-8", "replace") if _href(body).endswith(".gz") else raw.decode("utf-8", "replace")
        pattern = r'<location href="packages/' + re.escape(f"{pkg}-{version}") + r'\.[A-Za-z0-9_]+\.rpm"'
        return 0 if re.search(pattern, text) else 1
    return 1


def main(argv):
    if len(argv) >= 3 and argv[1] == "normalize":
        normalize(argv[2])
        return 0
    if len(argv) >= 3 and argv[1] == "verify":
        return verify(argv[2])
    if len(argv) >= 5 and argv[1] == "exists":
        return exists(argv[2], argv[3], argv[4])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
