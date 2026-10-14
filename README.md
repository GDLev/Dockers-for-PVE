# Proxmox BetterDockers

[![Version 0.20.0](https://img.shields.io/badge/version-0.20.0-blue?style=flat-square)](RELEASE_NOTES.md)
[![Proxmox VE 9.1 and 9.2](https://img.shields.io/badge/Proxmox_VE-9.1_%7C_9.2-E57000?style=flat-square&logo=proxmox&logoColor=white)](#requirements)

BetterDockers adds Docker projects to the Proxmox VE web interface as **DCT** guests. Deploy Compose files or images, manage services, configure networking and storage, and use PVE tasks, permissions, snapshots, backups, pools, and cluster operations.

Docker runs on the PVE host. A DCT can contain several services under one guest ID.

> [!WARNING]
> This is a development build, with no production release or established production signing key. Use it only on small, non-critical installations, preferably a single node. It is not recommended for commercial or complex deployments.

## Requirements

- Proxmox VE 9.1 or 9.2 on Debian 13. Other PVE versions are not supported.
- Root access to each installation node and internet access for dependencies and images.
- Docker Engine with Compose 5.6.0 or newer.
- A Linux bridge and available addresses for the DCT endpoints.
- Rootless mode also requires cgroup v2, delegated CPU/memory/PID controllers, rootless Docker binaries, and unused `10.240.0.0/16` and `169.254.240.0/30` ranges.

Choose the runtime when installing. Updates and repair preserve the node's mode.

| Mode | Docker runtime | Container AppArmor | Writable-layer quotas |
| --- | --- | --- | --- |
| Rootless | Dedicated shared `betterdocker` account | Not applied | Unavailable |
| Rootful | Host-root daemon | Managed enforcing profile for Hardened guests | Requires `overlay2` on XFS with project quotas |

PVE still manages networking, storage, and permissions as root in both modes. Rootless is the default for new installations. Neither mode provides VM isolation. See [SECURITY.md](SECURITY.md).

## Install the development build

Review the code and select the commit you intend to install. Run as root on a test PVE node:

```bash
git clone https://github.com/GDLev/Proxmox-BetterDockers.git
cd Proxmox-BetterDockers
# Check out your reviewed commit before continuing.
bash start.sh --development-source
```

`--development-source` explicitly accepts unsigned source. It is not a substitute for a signed production release.

The installer offers Install / Update, Repair, and Uninstall. Select nodes with Space and continue with Enter. Cluster installations use PVE's existing SSH trust. Read the per-node results, then press Enter to reload the panel and API services.

Refresh PVE with Ctrl+Shift+R. Click **Create DCT**, select a node, import a Compose file or choose an image, and configure the network. Check the task result before starting the guest.

For an unattended development installation:

```bash
bash start.sh install --development-source --runtime=rootless --yes
```

Add `--nodes=pve1,pve2` to select nodes or `--no-restart` to defer service reload. For signed artifact verification and package installation, see [CONTRIBUTING.md](CONTRIBUTING.md#build-and-sign).

## Update and repair

Review and check out the desired commit, then run:

```bash
bash start.sh install --development-source
bash start.sh repair --development-source
```

Install / Update updates the extension. Repair reapplies its integration to the installed PVE files. Back up workloads before maintenance. PVE package updates can require repair; unsupported native layouts are rejected.

Do not change runtime mode during an update. Offline conversion has separate prerequisites and a recovery journal, described in [CONTRIBUTING.md](CONTRIBUTING.md#runtime-conversion).

## Uninstall

Remove DCT HA registrations and replication jobs first. Run the reviewed installer and choose Uninstall, or run:

```bash
bash start.sh uninstall --yes
```

Uninstall removes the web/API integration and guest CLI. **Docker, workloads, persistent data, runtime configuration, and required network/firewall maintenance remain.** Removing retained data is a separate, explicit operation. Do not delete runtime directories while workloads use them.

## Use the CLI and API

Run `dct` as root on a PVE node:

```bash
dct list
dct status 150
dct help
dct help create
```

See [API_DOCS.md](API_DOCS.md) for HTTP requests, task handling, permissions, and configuration routes. [CONTRIBUTING.md](CONTRIBUTING.md#installed-files) lists installed extension and guest data paths.

## Operational limits

- All containers share the host kernel. Rootful Docker has host-root authority; rootless DCTs share a runtime account per node. Do not use either mode for hostile workloads.
- Delegated deployments use mandatory Hardened settings. Host namespaces, privileged containers, devices, external mounts, and custom drivers require administrator access.
- Per-service limits apply to each replica. Aggregate budgets control configured allocations, not one shared cgroup. Managed data-disk limits do not cover all images, logs, writable layers, or snapshots.
- Snapshot and backup consistency depends on the application. Snapshots are not a replacement for independently stored backups.
- Migration has downtime. Migration, replication, and HA require compatible runtime modes and storage. HA recovery needs quorum and a complete replica on an eligible node.
- Full installation lifecycle acceptance, the complete runtime/storage/firewall matrix, and independent security review remain release requirements. Mocked tests do not establish those guarantees.

Security reports: [security@gdlev.dev](mailto:security@gdlev.dev).
