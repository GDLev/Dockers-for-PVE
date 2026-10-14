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

## Run checks

Use Python 3.12 or newer, Node.js, and Docker Compose 5.6.0 or newer:

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

GitLab uses a Linux Docker executor tagged `betterdockers-linux`. Job containers need neither privileged mode nor the host Docker socket. GitHub jobs have read-only repository permissions. Images, actions, Compose, and Python dependencies are pinned. Review pin changes against upstream and repeat the checks.

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

Sign reviewed artifacts on a separate signing machine:

```bash
python3 packaging/sign-release.py --key /secure/betterdockers-release.pem --output dist
```

Use an RSA key of at least 3072 bits. Keep the private key outside the repository and ordinary CI. Publish the metadata, signature, public key, installer, source archive, and DEB together. Do not rebuild after signing or replace an existing release with different bytes.

No production key is provisioned by this repository. Administrators must confirm its DER SHA-256 fingerprint through an independently authenticated channel before saving `/etc/betterdocker/release-key.pem` and `/etc/betterdocker/release-key.sha256` as root-owned files. A key and fingerprint downloaded together do not establish trust.

Install a signed DEB through the verified release helper:

```bash
python3 scripts/release.py deb proxmox-betterdockers_0.20.0_all.deb \
    --manifest RELEASE.json --signature RELEASE.sig --version 0.20.0
```

Direct `dpkg -i` requires the helper's verified authorization. Source transfers verify signatures and hashes before isolated extraction. Package configuration and repair verify installed source. Trust and signed-version state remain after uninstall. For key rotation and withdrawal, see [SECURITY.md](SECURITY.md#release-security).

Until the first production release, the project targets the current development state. Older experimental builds are not supported upgrade targets. Recovery journals and data-retention safeguards still protect interrupted operations and existing workloads.

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

`<id>` is the internal project identifier, not the numeric guest ID. Rootful Docker normally stores its data in `/var/lib/docker/`; external bind mounts can be elsewhere. Rootless uses a shared account and subordinate-ID ranges. Do not renumber them manually. Uninstall retains workload data and required host maintenance. Recovery files are not disposable cache.
