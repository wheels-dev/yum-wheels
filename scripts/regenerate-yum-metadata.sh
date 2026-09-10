#!/bin/bash
# Incrementally regenerates yum metadata for one dispatched channel, then signs
# `repomd.xml` with GPG (detached → `repomd.xml.asc`).
#
# This is the incremental replacement for the old full-channel scan. The old
# flow ran `createrepo_c --update` over EVERY historical .rpm, which forced the
# workflow to re-download the whole channel (~18 GB, hundreds of files, ~15 min
# and growing) on every publish. Instead:
#
#   * the workflow slots the single new .rpm into <channel>/packages/ and
#     downloads the channel's existing repodata/ from R2 into
#     <channel>/repodata/,
#   * this script signs the new .rpm, builds a throwaway single-package repo,
#     and merges it with the existing repodata via `mergerepo_c --all` — which
#     preserves every historical version WITHOUT needing those .rpm files on
#     disk (createrepo_c --update alone would DROP them, verified empirically).
#
# Inputs (env vars):
#   GPG_PASSPHRASE  — passphrase for the imported signing key
#   GPG_KEY_ID      — long-form key ID (set by the workflow after `gpg --import`)
#   CHANNEL         — single channel to regenerate ("stable" or "bleeding-edge")

set -euo pipefail

if [ -z "${GPG_KEY_ID:-}" ]; then
  echo "::error::GPG_KEY_ID is unset — sign step would default to an arbitrary secret key."
  exit 1
fi

CHANNEL="${CHANNEL:-}"
if [ -z "$CHANNEL" ]; then
  echo "::error::CHANNEL is unset — must be the single dispatched channel."
  exit 1
fi

CHANNEL_DIR="$CHANNEL"
PKG_DIR="${CHANNEL_DIR}/packages"

# Only the just-slotted .rpm is present locally (the channel is not synced).
NEW_RPM=$(find "$PKG_DIR" -type f -name '*.rpm' 2>/dev/null | head -1 || true)
if [ -z "$NEW_RPM" ]; then
  echo "::error::No .rpm under ${PKG_DIR} to publish."
  exit 1
fi
echo "── Incrementally updating ${CHANNEL_DIR}/ with $(basename "$NEW_RPM") ──"

# nfpm produces unsigned .rpm files; the .repo files set gpgcheck=1, so dnf
# REJECTS unsigned packages. rpm --addsign embeds the signature in the .rpm
# header, which createrepo_c then records in primary.xml.gz.
#
# Requires rpm-sign for the rpm command. (Same rationale + macro setup as the
# previous full-scan script; see there for why a custom %__gpg_sign_cmd.)
PASS_FILE="${RUNNER_TEMP:-/tmp}/wheels-rpm-pass.$$"
umask 077
printf '%s' "${GPG_PASSPHRASE:-}" > "$PASS_FILE"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP" "$PASS_FILE"' EXIT

cat > ~/.rpmmacros <<RPMMACROS
%_signature gpg
%_gpg_name ${GPG_KEY_ID}
%_gpg_path ${GNUPGHOME:-${HOME}/.gnupg}
%__gpg $(command -v gpg)
%__gpg_sign_cmd %{__gpg} --batch --no-armor --no-secmem-warning --pinentry-mode loopback --passphrase-file ${PASS_FILE} --local-user "%{_gpg_name}" --sign --detach-sign --output %{__signature_filename} %{__plaintext_filename}
RPMMACROS

mkdir -p "${GNUPGHOME:-${HOME}/.gnupg}"
cat > "${GNUPGHOME:-${HOME}/.gnupg}/gpg-agent.conf" <<GPGAGENT
allow-loopback-pinentry
GPGAGENT
cat > "${GNUPGHOME:-${HOME}/.gnupg}/gpg.conf" <<GPGCONF
use-agent
pinentry-mode loopback
GPGCONF
gpg-connect-agent reloadagent /bye >/dev/null 2>&1 || true

# --- 1) Sign the new .rpm (the only one present). ---
echo "── Signing ${NEW_RPM} ──"
rpm --addsign "$NEW_RPM" >/dev/null
echo "  ✓ signed $(basename "$NEW_RPM")"

# --- 2) Build a throwaway single-package repo around it. ---
mkdir -p "$TMP/newrepo/packages"
cp "$NEW_RPM" "$TMP/newrepo/packages/"
createrepo_c --quiet "$TMP/newrepo"

# --- 3) Merge existing repodata (pulled by the workflow) with the new repo. ---
# --all keeps every historical version of same-name packages; --omit-baseurl
# keeps `location href` relative (packages/<file>.rpm) so dnf resolves them
# against <channel>/, where the historical .rpm files live in R2; --compress-type
# gz matches the existing repo's primary index format.
echo "── Merging existing repodata with the new package ──"
if [ -f "${CHANNEL_DIR}/repodata/repomd.xml" ]; then
  mergerepo_c \
    --repo "$CHANNEL_DIR" \
    --repo "$TMP/newrepo" \
    -o "$TMP/merged" \
    --compress-type gz \
    --all \
    --omit-baseurl

  rm -rf "${CHANNEL_DIR}/repodata"
  mv "$TMP/merged/repodata" "${CHANNEL_DIR}/repodata"
else
  # First publish on this channel: no existing repodata to merge — the
  # throwaway single-package repo becomes the first repodata.
  rm -rf "${CHANNEL_DIR}/repodata"
  mv "$TMP/newrepo/repodata" "${CHANNEL_DIR}/repodata"
fi

# --- 4) Sign the merged repomd.xml + export the public key. ---
REPOMD="${CHANNEL_DIR}/repodata/repomd.xml"
if [ ! -f "$REPOMD" ]; then
  echo "::error::mergerepo_c didn't produce ${REPOMD}"
  exit 1
fi

rm -f "${REPOMD}.asc"
gpg --batch --yes \
  --pinentry-mode loopback \
  --passphrase "${GPG_PASSPHRASE:-}" \
  --default-key "$GPG_KEY_ID" \
  --armor --detach-sign \
  --output "${REPOMD}.asc" \
  "$REPOMD"

# Some dnf clients fetch repomd.xml.key alongside repomd.xml.asc on first
# refresh. Export the public key there so installs don't fail with
# "GPG key not available" on hosts that don't pre-trust the key.
gpg --armor --export "$GPG_KEY_ID" > "${CHANNEL_DIR}/repodata/repomd.xml.key"

echo "  ✓ repodata + repomd.xml.asc + repomd.xml.key written for ${CHANNEL}"
echo "Done."
