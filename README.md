# Proxmox BetterDockers

[![Version 0.20.0](https://img.shields.io/badge/version-0.20.0-blue?style=flat-square)](RELEASE_NOTES.md)
[![Proxmox VE 9.1 and 9.2](https://img.shields.io/badge/Proxmox_VE-9.1_%7C_9.2-E57000?style=flat-square&logo=proxmox&logoColor=white)](#requirements)

BetterDockers adds Docker projects to the Proxmox VE web interface as **DCT** guests. Deploy Compose files or images, manage services, configure networking and storage, and use PVE tasks, permissions, snapshots, backups, pools, and cluster operations.

Docker runs on the PVE host. A DCT can contain several services under one guest ID.

> [!WARNING]
> Version 0.20.0 is a prerelease. Production acceptance is not complete. Use it only on small, non-critical installations, preferably a single node. It is not recommended for commercial or complex deployments.

## Requirements

- Proxmox VE 9.1 or 9.2 on Debian 13. Other PVE versions are not supported.
- Root access to each installation node and internet access for dependencies and images.
- Docker Engine with Compose 5.6.0 or newer.
- Install BetterDockers on every cluster node before creating DCTs, including nodes that will not run Docker guests. All nodes must reserve the same guest IDs. Install it on a new node before joining a cluster with DCTs, then run Repair after joining to register the node.
- A Linux bridge and available addresses for the DCT endpoints.
- Rootless mode also requires cgroup v2, delegated CPU/memory/PID controllers, rootless Docker binaries, and unused `10.240.0.0/16` and `169.254.240.0/30` ranges.

Choose the runtime when installing. Updates and repair preserve the node's mode.

| Mode | Docker runtime | Container AppArmor | Writable-layer quotas |
| --- | --- | --- | --- |
| Rootless | Dedicated shared `betterdocker` account | Not applied | Unavailable |
| Rootful | Host-root daemon | Managed enforcing profile for Hardened guests | Requires `overlay2` on XFS with project quotas |

PVE still manages networking, storage, and permissions as root in both modes. Rootless is the default for new installations. Neither mode provides VM isolation. See [SECURITY.md](SECURITY.md).

## Install the prerelease

Download the signed installer from the [0.20.0 release](https://github.com/GDLev/Proxmox-BetterDockers/releases/tag/v0.20.0). Follow [release verification and trust setup](SECURITY.md#verify-a-release) before executing it. Run as root on a test PVE node:

```bash
bash install.sh
```

The installer offers Install / Update, Repair, and Uninstall. Select nodes with Space and continue with Enter. Cluster installations use PVE's existing SSH trust. Read the per-node results, then press Enter to reload the panel and API services.

Refresh PVE with Ctrl+Shift+R. Click **Create DCT**, select a node, import a Compose file or choose an image, and configure the network. Check the task result before starting the guest.

For an unattended installation after verification:

```bash
bash install.sh install --runtime=rootless --yes
```

Add `--nodes=pve1,pve2` to select all cluster nodes on first installation, or `--no-restart` to defer service reload. Later updates and repairs can target individual installed nodes. [CONTRIBUTING.md](CONTRIBUTING.md) covers development checkouts, package installation and signing.

New DCT IDs are blocked until installation and the panel/API reload have finished on every node. An offline node with a completed installation retains its registration.

## Update and repair

For a new release, verify its installer and choose Install / Update. To repair the installed version:

```bash
bash /var/lib/betterdocker/installer/source/start.sh repair --yes
```

Install / Update updates the extension. Repair reapplies its integration to the installed PVE files. Back up workloads before maintenance. PVE package updates can require repair; unsupported native layouts are rejected.

Do not change runtime mode during an update. Offline conversion has separate prerequisites and a recovery journal, described in [CONTRIBUTING.md](CONTRIBUTING.md#runtime-conversion).

## Uninstall

Back up and remove all DCTs, retained managed disks, DCT HA registrations and replication jobs across the cluster first. Uninstall is blocked while any DCT metadata or guest ID reservation remains, even on another node. Run the reviewed installer and choose Uninstall, or run:

```bash
bash /var/lib/betterdocker/installer/source/start.sh uninstall --yes
```

For a DEB installation, use `apt remove proxmox-betterdockers` so the package and its repair triggers are removed too.

Uninstall removes the web/API integration and guest CLI. Docker, runtime configuration, remaining files and network/firewall maintenance remain. It does not erase external bind mounts or unrelated Docker workloads. Export DCT backups before deleting guests; their ID protection cannot be removed while they exist.

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

## License

Copyright 2026 GDLev and contributors. Licensed under the [GNU Affero General Public License, version 3 or later](LICENSE). Proxmox VE is a separate project maintained by Proxmox Server Solutions GmbH.
