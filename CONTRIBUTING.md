# Contributing

Keep changes focused. Include a regression check and distinguish mocked contracts from actual integration results. Do not change supported PVE versions or data formats without documenting the decision.

## Source layout

| Area | Source |
| --- | --- |
| PVE API and permissions | `backend/PVE/API2/BetterDocker.pm`, `backend/PVE/API2/BetterDocker/` |
| Native CLI | `backend/PVE/CLI/dct.pm`, `scripts/dct.py` |
| Guest operations | `scripts/dctlib/`, grouped by subsystem |
| Host runtime and networking | `scripts/runtime.py`, `scripts/host.py`, `scripts/firewall.py` |
| Backup, replication, and HA | `backend/PVE/BetterDocker/`, native providers, `scripts/guest_state.py` |
| Web interface | `frontend/`, ordered by `frontend/sources.json` |
| Installation | `install.sh`, `start.sh`, `scripts/installer.py` |
| Packages and signatures | `packaging/`, `scripts/release.py` |

The frontend runs in PVE's ExtJS environment and has no npm dependencies. Edit source files, not the generated or installed bundle.

## Development installation

Review and check out the intended commit on a disposable PVE node, then run `bash start.sh --development-source`. This explicitly accepts unsigned source. Do not use that flag for downloaded release archives or packages.

## Run checks

Use Python 3.12 or newer, Node.js 22, and Docker Compose 5.6.0 or newer:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --require-hashes -r requirements-test.txt
python3 tests/check_unit.py
python3 scripts/build_frontend.py | node --check
node tests/test_frontend.js
bash -n install.sh start.sh scripts/docker-install.sh packaging/build-deb.sh packaging/build-release.sh
```

The required CI checks run on a disposable Linux worker as root inside the job container. `scripts/ci-dependencies.sh` installs dependencies and copies source to a protected, readable directory. Do not run that setup on a PVE node. `tests/check_release_ci.py` fails on skipped required tests. Ordinary unit tests isolate node-control files and mock external services.

GitHub Actions runs the checks on hosted Linux workers with read-only repository permissions. Actions, Compose, and Python dependencies are pinned. Review pin changes against upstream and repeat the checks.

On a supported node or a saved PVE filesystem, run:

```bash
python3 tests/check_native.py /
```

This checks native integration and reversible patch application in memory. It does not install or restart services.

Live tests require an explicit private configuration. On the disposable test node, save a JSON file with mode `0600`, owned by the user running the test:

```json
{
  "nodes": {"primary": "pve1", "secondary": "pve2", "rootless": "pve3"},
  "api_host": "pve1.example",
  "addresses": "192.0.2.0/24",
  "backup_storage": "backup",
  "source": "/root/Proxmox-BetterDockers",
  "package": "/root/proxmox-betterdockers_0.20.0_all.deb"
}
```

Replace the documentation subnet with a reserved test subnet, and use the API certificate's hostname. Set `BETTERDOCKER_TEST_CONFIG` to the file's absolute path. Each script has additional prerequisites; this configuration alone does not prepare fixtures or make a scenario safe to run.

Live tests can change test guests or host configuration. Read their prerequisites and use disposable nodes. Keep fixture journals private, record ownership before creation, and clean up only registered identities. Never use a name prefix alone to delete resources. Installation, restart, uninstall, real traffic, backup/restore, and cluster recovery require live verification; mocks do not establish those results.

## Build and sign

Build on Linux with `dpkg-deb`:

```bash
bash packaging/build-deb.sh
bash packaging/build-release.sh
python3 tests/check_release_artifacts.py
```

The artifact check requires Linux root in a disposable worker. It uses a disposable signing key and never installs a package. The version comes from `scripts/dctlib/version.py`.

CI builds unsigned artifacts and does not publish releases. Test the final commit and exact package bytes, including clean installation, repair, uninstall, service restart, and guest operations in each supported environment.

Download the `betterdocker-deb` artifact from the successful GitHub `Validate` run for the frozen commit. Sign those exact DEB and source archive bytes from its reviewed checkout, on a separate Linux signing machine with `dpkg-deb`:

```bash
python3 packaging/sign-release.py --key /secure/betterdockers-release.pem --output dist
```

Use an RSA key of at least 3072 bits. Keep the private key outside the repository and ordinary CI. Publish the metadata, signature, public key, installer, source archive, and DEB together. Do not rebuild after signing or replace an existing release with different bytes.

The release public key is in `packaging/release-key.pem`. The installer does not provision trust automatically. Administrators must confirm its DER SHA-256 fingerprint through an independently authenticated channel before saving `/etc/betterdocker/release-key.pem` and `/etc/betterdocker/release-key.sha256` as root-owned files. A key and fingerprint downloaded together do not establish trust.

Install a signed DEB through the verified release helper:

```bash
python3 scripts/release.py deb proxmox-betterdockers_0.20.0_all.deb \
    --manifest RELEASE.json --signature RELEASE.sig --version 0.20.0
```

The signer also writes `SHA256SUMS`. Keep the accepted CI artifact unchanged. `packaging/verify-release.py <signed-assets> --build <downloaded-ci-artifact>` verifies the signature, reviewed source, public key, checksums and original build bytes.

The separate `Publish prerelease` workflow accepts a successful Validate run ID and a draft release ID bound to the same full commit SHA. It verifies the draft before approval and repeats verification after the `release` environment approval. It publishes the existing assets without rebuilding or accessing the private signing key. Publishing also requires `RELEASE_PUBLICATION_ENABLED=true`, a public repository and configured required reviewers. Leave publication disabled until the candidate is accepted. Do not give public PR workflows access to signing keys, lab credentials or PVE runners.

Direct `dpkg -i` requires the helper's verified authorization. Source transfers verify signatures and hashes before isolated extraction. Package configuration and repair verify installed source. Trust and signed-version state remain after uninstall. For key rotation and withdrawal, see [SECURITY.md](SECURITY.md#release-security).

Recovery journals and data-retention safeguards protect interrupted operations and existing workloads. Do not remove them as an installation shortcut.

## Runtime conversion

`migrate-runtime.py` is an offline operation. All DCTs must be stopped, with static addresses, owned local mounts, no snapshot history, HA enrollment, or replication state disk. The destination runtime must be empty. Unrelated Docker workloads block conversion. Back up data first.

```bash
/usr/local/lib/betterdocker/migrate-runtime.py --runtime=rootless
/usr/local/lib/betterdocker/migrate-runtime.py --runtime=rootless --apply
```

The first command lists blockers. Conversion retains original data and records a journal in `/var/lib/betterdocker/runtime-migrations/`. Resume with `--runtime=<target> --resume=<journal> --apply`. Roll back with `--rollback=<journal> --apply` after stopping converted guests. Rollback discards changes made in the converted runtime. Retain the source data until verification and backup complete.

## Installed files

Extension files on the PVE node:

| Path | Purpose |
| --- | --- |
| `/usr/sbin/dct`, `/usr/share/perl5/PVE/CLI/dct.pm` | Guest CLI |
| `/usr/local/lib/betterdocker/` | Runtime and integration helpers |
| `/usr/share/perl5/PVE/API2/BetterDocker*`, `/usr/share/perl5/PVE/BetterDocker/` | API and native providers |
| `/usr/share/pve-manager/js/betterdocker*`, `/usr/share/pve-manager/css/betterdocker.css` | Panel and translations |
| `/etc/systemd/system/betterdocker-*`, `/etc/apparmor.d/betterdocker` | Host services and rootful container profile |
| `/etc/betterdocker/` | Runtime, administrator policies, and release trust |
| `/var/lib/betterdocker/installer/`, `/var/backups/betterdocker/` | Private installer logs, cache, and recovery copies |

The installer adds marked sections to native PVE modules and panel assets. Repair and uninstall transform the current files; they do not restore stale package backups.

DCT files and data:

| Path | Purpose |
| --- | --- |
| `/etc/pve/betterdocker/`, `/etc/pve/betterdocker/ids/` | Shared guest metadata and ID reservations |
| `/etc/pve/firewall/<vmid>.fw` | Guest firewall configuration |
| `/var/lib/betterdocker/projects/<id>/` | Private deployment configuration, environment, settings, and managed data mounts |
| `/var/lib/betterdocker/snapshots/<id>/` | Local snapshot state |
| `/var/lib/betterdocker/runtime/docker/` | Rootless Docker data |
| `/var/lib/betterdocker/firewall/`, `/var/lib/betterdocker/hooks/`, `/var/lib/betterdocker/ha-recoveries/` | Host maintenance and operation journals |

`<id>` is the internal project identifier, not the numeric guest ID. Rootful Docker normally stores its data in `/var/lib/docker/`; external bind mounts can be elsewhere. Rootless uses a shared account and subordinate-ID ranges. Do not renumber them manually. Uninstall requires an empty cluster DCT inventory and no retained disk reservations. It retains Docker, runtime configuration and required host maintenance. Recovery files are not disposable cache.

Installation records in `/etc/pve/betterdocker/nodes/` identify nodes with working integration. Publish a record only after installation succeeds. Removal clears it under PVE's `datacenter.cfg` lock after checking the cluster guest and reservation inventory. ID allocation uses the same lock and requires records for all current PVE members. An offline installed node retains its record. New or uninstalled nodes block new DCT IDs until installed or repaired. For DEB installations, install the verified package on every node before creating DCTs. Do not delete these records manually to bypass removal checks.
