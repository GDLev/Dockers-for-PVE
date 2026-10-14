# Working on BetterDockers

Keep each change focused on one feature or fix. Include the relevant tests and say how you checked it.

## Where to look

| Area | Source |
| --- | --- |
| PVE API permissions and native hooks | `backend/PVE/API2/BetterDocker.pm` |
| HTTP endpoints | `backend/PVE/API2/BetterDocker/`, grouped by feature |
| Guest CLI | `backend/PVE/CLI/dct.pm`, `scripts/dct.py` |
| Guest operations and command dispatch | `scripts/dctlib/projects.py` |
| Deployments and permissions | `scripts/dctlib/deployment.py`, `policy.py` |
| Networking, disks, settings and snapshots | Matching files in `scripts/dctlib/` |
| Node startup and guest hooks | `scripts/dctlib/lifecycle.py` |
| Runtime, firewall, backup and cluster transfers | Named helpers in `scripts/` and `backend/PVE/BetterDocker/` |
| PVE panel | `frontend/`, grouped by feature |
| Installation | `start.sh`, `install.sh`, `scripts/installer.py` |
| Debian package | `packaging/build-deb.sh` |

`provision.py` is the command entry point used by PVE and systemd. The Python modules share project paths and host operations through `dctlib.projects`; they use the selected runtime through `scripts/runtime.py`.

The panel uses PVE's ExtJS environment. `frontend/sources.json` sets the source order. The installer builds one `betterdocker.js` file, so the browser still loads a single script. Edit the source files, not the installed bundle. No npm dependencies are needed.

## Checks

From the repository root, with Python 3.12 or newer and Node.js installed:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --require-hashes -r requirements-test.txt
python3 tests/check_unit.py
python3 scripts/build_frontend.py | node --check
node tests/test_frontend.js
bash -n install.sh start.sh scripts/docker-install.sh packaging/build-deb.sh packaging/build-release.sh
```

Tests named `live_*.py` use a real PVE cluster and can change guests or host configuration. Read the test before running it and use a disposable installation. Ordinary unit tests do not connect to a cluster.

Check install, repair and removal patch compatibility against a supported PVE node or a saved filesystem:

```sh
python3 tests/check_native.py /
```

This check reads native files, applies integration patches in memory twice to exercise repair, and removes them in memory. It does not install packages or restart services. Run it on both PVE 9.1 and 9.2 after package updates. Full installer lifecycle tests remain in `tests/live_installation.py` and `tests/live_terminal_installer.py`; these change the host and require a disposable node.

## Managed disks

`storage.json` owns the primary data disk and the `additional` collection, keyed by `mp1` through `mp15`. Each additional disk records its PVE volume, service, container path, size, read-only flag and backup flag. `disks.configure()` changes this collection under the guest operation lock. Container replacement and configuration writes share one recovery transaction.

Backups can omit disk contents selected with `backup=0`; clones, migration, replication and snapshots include every managed disk. Imports allocate destination volumes before restoring mount data. A detached disk stays in `unused` until explicitly removed. PVE storage helpers check every attached, unused and pending allocation against its guest ID.

A separate file per disk was considered. One collection keeps configuration, ownership and rollback together and avoids a directory scan racing a disk edit. The primary disk layout stays readable by existing installations.

Release packages read their version from `scripts/dctlib/version.py`. Build the DEB with `bash packaging/build-deb.sh` and the source archive with `bash packaging/build-release.sh` on Linux. Ordinary CI produces unsigned build artifacts. These are not installation releases.

After reviewing the commit and its required check results, sign the exact artifacts on a separate signing machine:

```bash
python3 packaging/sign-release.py --key /secure/offline/betterdockers-release.pem --output dist
```

Use an RSA signing key of at least 3072 bits. Keep it outside the repository, build containers and ordinary CI secrets. Encrypt and back it up offline. Publish `RELEASE.json`, `RELEASE.sig`, `install.sh`, the source archive and the DEB and public key together under the matching versioned release. Never rebuild artifacts between testing, signing and publishing. Source hashes are read from the built archive; signing rejects a different installer.

The administrator establishes initial trust by comparing the public-key DER SHA-256 fingerprint through an independent channel, such as a previously authenticated maintainer contact or an already trusted release announcement. No production key or fingerprint is provisioned by this checkout. Do not publish installation instructions claiming otherwise.

For key rotation, announce the replacement fingerprint through the same independent channel before switching releases. Administrators compare it and replace both root-owned trust files on each node during maintenance. There is one active key per node; releases signed by other keys fail. If a key is compromised, stop release distribution and revoke it through the independent channel. Updating a key from the download server alone is not sufficient.

Install a DEB through a verified release's verifier, not directly with `dpkg -i`:

```bash
python3 scripts/release.py deb proxmox-betterdockers_0.20.0_all.deb \
    --manifest RELEASE.json --signature RELEASE.sig --version 0.20.0
```

This verifies the package before creating a private installation authorization. The package pre-install script checks that authorization and refuses unsigned direct installation. Cluster targets verify signed metadata before archive extraction, check the transfer hash received through authenticated SSH and unpack with the isolated nobody reader. Post-install and PVE repair triggers check the unpacked payload against signed source hashes. `--allow-downgrade` is an explicit administrator override. Trust files and version history remain after removal.

For development deployments use `bash start.sh --development-source`; it prints a warning and does not create a signed-release history entry. Signed source cannot use this bypass. A development repair cache needs the same explicit flag. This is for reviewed local work, not release downloads.

Required release checks run with Linux root privileges in disposable CI workers. `scripts/ci-dependencies.sh` installs a pinned Compose CLI without starting a daemon. `tests/check_release_ci.py` runs the non-invasive security contracts, Compose parsing, installer authentication and both PVE 9.1/9.2 source fixtures. Any skipped required test fails the job. The ordinary unit and frontend checks also run. `tests/check_unit.py` redirects node-control state to temporary files and mocks unrelated admission helpers for older Engine fixtures; the required policy suites exercise those helpers separately. Clean-node rootful/rootless installation remains a separate acceptance test before publication.

GitHub jobs have read-only repository permissions. External actions use commit hashes; GitLab images use registry digests. Compose uses a version and SHA-256 pin, and Python packages use hashes. CI workers still obtain distribution packages from their authenticated package repositories. Protect release tags and signing access; a successful untrusted pull-request job is not authorization to publish.

Review upstream security notices regularly. Update action commits, image digests, Compose and Python hashes in a dedicated PR. Obtain pins from the upstream repository, registry or release, review the change, rerun required checks and the supported PVE fixture matrix on that exact commit, then approve the new pins. Do not use automatic mutable-tag refreshes for releases.
