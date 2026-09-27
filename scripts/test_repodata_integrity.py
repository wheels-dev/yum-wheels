#!/usr/bin/env python3
"""Tests for scripts/repodata-integrity.py (wheels-dev/wheels#3690).

Builds small synthetic channels in a temp dir; stdlib only:

    python3 scripts/test_repodata_integrity.py
"""

import gzip
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "repodata-integrity.py")

PRIMARY = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<metadata xmlns="http://linux.duke.edu/metadata/common" packages="2">\n'
    '<package type="rpm"><name>wheels</name><location href="packages/wheels-4.0.6.noarch.rpm"/></package>\n'
    '<package type="rpm"><name>wheels</name><location href="packages/wheels-4.1.0.noarch.rpm"/></package>\n'
    "</metadata>\n"
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def make_channel(root, primary_text=PRIMARY, stale=False, with_primary=True):
    """A channel whose repomd.xml is consistent, or (stale=True) describes an
    earlier version of each payload: old filename, old open-checksum/size."""
    repodata = os.path.join(root, "repodata")
    os.makedirs(repodata)
    blocks = []
    kinds = [("primary", primary_text), ("other", "<otherdata packages=\"2\"/>\n")]
    if not with_primary:
        kinds = kinds[1:]
    for kind, text in kinds:
        opened = text.encode("utf-8")
        gz = gzip.compress(opened, 9, mtime=0)
        name_sha, open_sha, open_size = _sha(gz), _sha(opened), len(opened)
        if stale:
            earlier = (text + " ").encode("utf-8")  # the pre-repair payload
            name_sha, open_sha, open_size = _sha(gzip.compress(earlier, 9, mtime=0)), _sha(earlier), len(earlier)
        href = f"repodata/{name_sha}-{kind}.xml.gz"
        with open(os.path.join(root, href), "wb") as fh:
            fh.write(gz)
        blocks.append(
            f'  <data type="{kind}">\n'
            f'    <checksum type="sha256">{_sha(gz)}</checksum>\n'
            f'    <open-checksum type="sha256">{open_sha}</open-checksum>\n'
            f'    <location href="{href}"/>\n'
            f"    <timestamp>1789669252</timestamp>\n"
            f"    <size>{len(gz)}</size>\n"
            f"    <open-size>{open_size}</open-size>\n"
            f"  </data>\n"
        )
    with open(os.path.join(repodata, "repomd.xml"), "w") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<repomd xmlns="http://linux.duke.edu/metadata/repo">\n'
                 "  <revision>1789669252</revision>\n" + "".join(blocks) + "</repomd>\n")
    return root


def run(*args):
    proc = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


class RepodataIntegrityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def channel(self, **kw):
        return make_channel(os.path.join(self.tmp, "ch"), **kw)

    def test_consistent_channel_verifies_and_normalize_is_a_noop(self):
        ch = self.channel()
        self.assertEqual(run("verify", ch)[0], 0)
        with open(os.path.join(ch, "repodata", "repomd.xml")) as fh:
            before = fh.read()
        code, out = run("normalize", ch)
        self.assertEqual(code, 0)
        self.assertIn("nothing to normalize", out)
        with open(os.path.join(ch, "repodata", "repomd.xml")) as fh:
            self.assertEqual(before, fh.read())

    def test_stale_channel_fails_verify_then_normalize_fixes_it(self):
        ch = self.channel(stale=True)
        code, out = run("verify", ch)
        self.assertEqual(code, 1)
        for field in ("does not carry its sha256", "<open-checksum>", "<open-size>"):
            self.assertIn(field, out)
        self.assertEqual(run("normalize", ch)[0], 0)
        self.assertEqual(run("verify", ch)[0], 0, run("verify", ch)[1])
        names = sorted(os.listdir(os.path.join(ch, "repodata")))
        self.assertEqual(len([n for n in names if n.endswith(".gz")]), 2, names)  # stale files renamed, not copied

    def test_exists_listed_and_absent(self):
        ch = self.channel()
        self.assertEqual(run("exists", ch, "wheels", "4.1.0")[0], 0)
        self.assertEqual(run("exists", ch, "wheels", "9.9.9")[0], 1)
        self.assertEqual(run("exists", ch, "wheels", "4.1")[0], 1)  # no prefix match

    def test_exists_fails_closed_when_primary_file_is_missing(self):
        ch = self.channel()
        for name in os.listdir(os.path.join(ch, "repodata")):
            if name.endswith("-primary.xml.gz"):
                os.remove(os.path.join(ch, "repodata", name))
        self.assertEqual(run("exists", ch, "wheels", "4.1.0")[0], 2)

    def test_exists_fails_closed_on_truncated_gzip(self):
        ch = self.channel()
        for name in os.listdir(os.path.join(ch, "repodata")):
            if name.endswith("-primary.xml.gz"):
                path = os.path.join(ch, "repodata", name)
                with open(path, "rb") as fh:
                    data = fh.read()
                with open(path, "wb") as fh:
                    fh.write(data[: len(data) // 2])
        self.assertEqual(run("exists", ch, "wheels", "4.1.0")[0], 2)

    def _primary_path(self, ch):
        return next(os.path.join(ch, "repodata", n) for n in os.listdir(os.path.join(ch, "repodata"))
                    if n.endswith("-primary.xml.gz"))

    def _flip_to(self, ch, exc_type):
        """Flip one byte of primary so that gzip.decompress raises exc_type."""
        path = self._primary_path(ch)
        with open(path, "rb") as fh:
            original = fh.read()
        for i in range(10, len(original)):  # past the gzip header
            data = bytearray(original)
            data[i] ^= 0xFF
            try:
                gzip.decompress(bytes(data))
            except exc_type:
                with open(path, "wb") as fh:
                    fh.write(bytes(data))
                return i
            except Exception:
                continue
        self.fail(f"no single-byte flip of the fixture raises {exc_type.__name__}")

    def test_exists_fails_closed_on_byte_flipped_gzip(self):
        # A flip inside the deflate stream raises zlib.error, which escaped the
        # old except list and exited 1 = "new". A flip that survives inflate
        # fails the CRC (BadGzipFile). Both must be 2.
        for exc_type in (zlib.error, gzip.BadGzipFile):
            with self.subTest(exc=exc_type.__name__):
                ch = make_channel(os.path.join(self.tmp, f"flip-{exc_type.__name__}"))
                self._flip_to(ch, exc_type)
                code, out = run("exists", ch, "wheels", "4.1.0")
                self.assertEqual(code, 2, out)

    def test_exists_fails_closed_when_bytes_do_not_match_repomd_checksum(self):
        # Valid gzip, valid primary XML, but not the bytes repomd.xml vouches for.
        ch = self.channel()
        other = PRIMARY.replace("4.0.6", "4.0.5").encode("utf-8")
        for name in os.listdir(os.path.join(ch, "repodata")):
            if name.endswith("-primary.xml.gz"):
                with open(os.path.join(ch, "repodata", name), "wb") as fh:
                    fh.write(gzip.compress(other, 9, mtime=0))
        code, out = run("exists", ch, "wheels", "4.1.0")
        self.assertEqual(code, 2, out)
        self.assertIn("<checksum>", out)

    def test_exists_fails_closed_on_non_primary_payload(self):
        ch = self.channel(primary_text="<html>404 Not Found</html>\n")
        self.assertEqual(run("exists", ch, "wheels", "4.1.0")[0], 2)

    def test_exists_fails_closed_without_a_primary_component(self):
        ch = self.channel(with_primary=False)
        self.assertEqual(run("exists", ch, "wheels", "4.1.0")[0], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
