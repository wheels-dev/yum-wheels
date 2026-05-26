# yum-wheels

Source repository backing **<https://yum.wheels.dev>**, the native Fedora/RHEL
package repository for the [Wheels](https://wheels.dev) CFML framework CLI.

This repo holds the **workflow + scripts + landing page + signing key + .repo files**.
The yum metadata tree (`<channel>/repodata/`) and the `.rpm` pool (`<channel>/packages/`)
live in Cloudflare R2 (bucket `wheels-yum`), served at https://yum.wheels.dev via
R2 custom-domain. The receiver workflow regenerates metadata + uploads to R2 on every
release. End users do:

```bash
# Stable
sudo dnf config-manager --add-repo https://yum.wheels.dev/wheels.repo
sudo dnf install wheels

# Bleeding-edge — distinct package name (`wheels-be`) so it coexists with stable
sudo dnf config-manager --add-repo https://yum.wheels.dev/wheels-be.repo
sudo dnf install wheels-be
```

## How updates flow into this repo

Releases land in `wheels-dev/wheels` (or `wheels-dev/wheels-snapshots` for
bleeding-edge). The release workflow fires `repository_dispatch` here with
event type `wheels-released` and a `{version, channel}` payload. The receiver
at [`.github/workflows/wheels-released.yml`](.github/workflows/wheels-released.yml)
downloads the new `.rpm` from the upstream GitHub Release, slots it into
`<channel>/packages/`, signs each `.rpm` with `rpm --addsign`, regenerates
`repodata/repomd.xml` via `createrepo_c`, GPG-signs `repomd.xml` (detached →
`repomd.xml.asc`), exports the public key copy to `repomd.xml.key`, and
commits the whole tree back. Cloudflare Pages picks up the push and republishes
within ~30s.

The receiver also supports `workflow_dispatch` for backfill / disaster-recovery:

```bash
gh workflow run wheels-released.yml \
  --repo wheels-dev/yum-wheels \
  -f version=4.0.0 -f channel=stable
```

## Contents

| Path | Purpose |
|------|---------|
| `.github/workflows/wheels-released.yml` | Receiver workflow — fires on `repository_dispatch` from `wheels-dev/wheels`. |
| `scripts/regenerate-yum-metadata.sh` | Pure-bash wrapper around `rpm --addsign` + `createrepo_c` + `gpg --detach-sign`. Idempotent. |
| `wheels.repo` | The `.repo` file users grab via `dnf config-manager --add-repo`. Served at `https://yum.wheels.dev/wheels.repo`. |
| `wheels-be.repo` | Bleeding-edge `.repo` file, served at `https://yum.wheels.dev/wheels-be.repo`. |
| `index.html` | Plain-HTML landing page served at the apex. |
| `wheels.gpg.placeholder` | Reminder file — **must be replaced with the real ASCII-armored public key** committed as `wheels.gpg` at the repo root before the first release publish. See "Operational setup" below. |


## Distribution layout (post-first-publish)

```
yum.wheels.dev/
├── wheels.gpg                                       # ASCII-armored public key
├── wheels.repo                                      # stable channel .repo file
├── wheels-be.repo                                   # bleeding-edge .repo file
├── stable/
│   ├── repodata/
│   │   ├── repomd.xml
│   │   ├── repomd.xml.asc                           # detached GPG signature
│   │   ├── repomd.xml.key                           # public key copy (some clients fetch this)
│   │   ├── primary.xml.gz
│   │   ├── filelists.xml.gz
│   │   └── other.xml.gz
│   └── packages/
│       └── wheels-<v>.x86_64.rpm
└── bleeding-edge/
    └── ... (mirror of the stable tree)
```

## Package name & channel convention

Identical to the [apt side](https://github.com/wheels-dev/apt-wheels):

| Channel | Package name | Pool path | `dnf install` |
|---------|--------------|-----------|---------------|
| Stable | `wheels` | `stable/packages/` | `dnf install wheels` |
| Bleeding-edge | `wheels-be` | `bleeding-edge/packages/` | `dnf install wheels-be` |

Distinct package names mean stable and bleeding-edge can be installed
side-by-side on the same host.

## Filename: `~`-form vs `.`-form

Same gotcha as the apt side — GitHub Releases rewrites `~` to `.` on upload.
The receiver workflow downloads the `.`-form (the only form the GitHub Release
URL exposes), then renames to the canonical `~`-form before slotting into
`<channel>/packages/`. The version field *inside* the RPM metadata always
carries `~`, so `rpmvercmp` orders snapshot releases below the next GA
correctly.

## Operational setup

Before this repo will publish a usable repository:

1. **GPG signing key** — same key as the parallel
   [`apt-wheels`](https://github.com/wheels-dev/apt-wheels) bucket (one key
   signs apt `Release`/`InRelease`, yum `repomd.xml.asc`, AND each individual
   `.rpm` via `rpm --addsign`). Private key + passphrase live in 1Password at
   `op://Wheels/wheels-linux-repo-signing/` (Wheels project vault on
   `my.1password.com` — **not** the PAI `op://Infrastructure/` vault).
   Public key (ASCII-armored) replaces `wheels.gpg.placeholder` with a
   committed `wheels.gpg` at the repo root.

2. **Cloudflare R2 bucket** — create the `wheels-yum` bucket, attach the
   `yum.wheels.dev` custom domain. R2 has no per-object size limit (vs.
   Cloudflare Pages' 25 MiB) which is why we serve from R2 rather than Pages.

3. **CI secrets** at
   <https://github.com/wheels-dev/yum-wheels/settings/secrets/actions>:
   - `WHEELS_REPO_GPG_PRIVATE_KEY` — ASCII-armored private key
   - `WHEELS_REPO_GPG_PASSPHRASE` — passphrase
   - `CLOUDFLARE_API_TOKEN` — token with `Workers R2 Storage:Edit` on this account

4. **Upstream dispatch token** — on `wheels-dev/wheels`, add
   `LINUX_REPO_DISPATCH_TOKEN` (fine-grained PAT with `actions: write` on this
   repo and on `wheels-dev/apt-wheels`). The release workflow's dispatch step
   skips silently when this secret is unset.

5. **Smoke-test** — once the GPG key is in place and the secrets are set, run
   the receiver manually to backfill the current GA release:

   ```bash
   gh workflow run wheels-released.yml \
     --repo wheels-dev/yum-wheels \
     -f version=4.0.1 -f channel=stable
   ```

   Then verify on a fresh Fedora/RHEL host:

   ```bash
   sudo dnf config-manager --add-repo https://yum.wheels.dev/wheels.repo
   sudo dnf install wheels
   wheels --version
   sudo dnf --refresh check-update wheels
   # dnf prints "Importing GPG key 0x<keyid>: ..." on first refresh
   ```

   A `repomd.xml.asc verify failed` error means the bucket's GPG signing step
   didn't run, or the public key at `/wheels.gpg` doesn't match the private
   key that signed.

## Why `rpm --addsign` AND `gpg` on `repomd.xml`

The `.repo` files served by this bucket set `gpgcheck=1` (per-package signature
check) and `repo_gpgcheck=1` (metadata signature check). That means we need
**both**:

- Each `.rpm` is signed with `rpm --addsign`, which embeds the signature in
  the RPM header. `createrepo_c` then records the signed-package hashes in
  `primary.xml.gz`. `dnf` checks `gpgcheck` against this signature.
- `repomd.xml` itself is GPG-detached-signed → `repomd.xml.asc`. `dnf` checks
  `repo_gpgcheck` against this. Because every other metadata file's checksum
  lives inside `repomd.xml`, signing it transitively signs the whole tree.

If either signature is missing, `dnf install wheels` fails with
`"Package is not signed: GPG check FAILED"` or
`"repomd.xml.asc verify failed"`. Re-runs of the receiver are safe — `rpm
--addsign` replaces existing signatures idempotently.

## Upstream source-of-truth

This repo was seeded from the templates at
[`wheels-dev/wheels` → `tools/distribution-drafts/yum-repo/`](https://github.com/wheels-dev/wheels/tree/develop/tools/distribution-drafts/yum-repo).
Material changes to the workflow / scripts should generally land in the
upstream template too. See
[issue #2605](https://github.com/wheels-dev/wheels/issues/2605) for the
Phase 2 rollout plan.

## License

MIT — see [LICENSE](LICENSE).
