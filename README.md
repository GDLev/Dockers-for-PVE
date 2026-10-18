<p align="center">
  <img src="assets/readme-banner.svg" alt="Dockers for Proxmox VE" width="517" />
</p>

<p align="center">
  <a href="RELEASE_NOTES.md"><img src="https://img.shields.io/badge/version-0.20.0-blue?style=flat-square" alt="Version 0.20.0" /></a>
  <a href="#requirements"><img src="https://img.shields.io/badge/Proxmox_VE-9.2-E57000?style=flat-square&amp;logo=proxmox&amp;logoColor=white" alt="Proxmox VE 9.2" /></a>
</p>

## About

Dockers for Proxmox VE brings Docker projects into the Proxmox VE web interface as **DCT** guests. Each guest has its own ID and can run a single image or a Compose stack. Docker runs directly on the PVE host, using either a rootful or rootless runtime.

https://github.com/user-attachments/assets/65829a65-72af-4b6d-b681-c7ac81ec3553

> [!WARNING]
> Version 0.20.0 is a prerelease intended for small, non-critical installations. It is not recommended for commercial or complex deployments. Independent security review has not been completed.

## Support

Support development on Ko-fi.

<a href="https://ko-fi.com/gdlev"><img src="https://storage.ko-fi.com/cdn/kofi5.png?v=6" alt="Support GDLev on Ko-fi" height="36" /></a>

## Features

- Deploy Docker images and Compose stacks from the PVE interface.
- Start, stop, restart, clone, and manage guests from the guest tree and context menus.
- Open a console, read service logs, and view CPU, memory, network, and disk metrics.
- Configure service resources, network endpoints, published ports, and managed data disks.
- Create snapshots, roll back changes, and back up or restore through PVE storage, including Proxmox Backup Server.
- Use PVE permissions, API tokens, resource pools, tags, templates, and task history.
- Migrate between compatible nodes, replicate guest data, and configure HA recovery.
- Manage guests from the `dct` CLI or the PVE HTTP API.

## Requirements

- Fully updated Proxmox VE 9.2 on Debian 13, with pve-manager 9.2.21 or newer and a running PVE kernel of at least 7.0.14-23-pve. Update PVE and reboot before installation. Other PVE releases are not supported.
- Root access to each installation node and internet access for dependencies and images.
- For a new installation, use a host without existing Docker workloads, preferably without Docker Engine installed.
- Install Dockers for Proxmox VE on every cluster node before creating DCTs, including nodes that will not run Docker guests. All nodes must reserve the same guest IDs. Install it on a new node before joining a cluster with DCTs, then run Repair after joining to register the node.
- A Linux bridge and available addresses for the DCT endpoints.
- For rootless mode, the `10.240.0.0/16` and `169.254.240.0/30` ranges must not overlap your host's network routes.

Tested with PVE 9.2.21 and kernel 7.0.14-24-pve. PBS backup/restore uses native PVE storage and was checked with PBS 4.2.0-1. Other combinations have not been verified.

Choose the runtime when installing. Updates and repair preserve the node's mode.

| Mode | Docker runtime | Container AppArmor | Writable-layer quotas |
| --- | --- | --- | --- |
| Rootless | Dedicated shared `betterdocker` account | Not applied | Unavailable |
| Rootful | Host-root daemon | Managed enforcing profile for Hardened guests | Requires `overlay2` on XFS with project quotas |

PVE still manages networking, storage, and permissions as root in both modes. Rootless is the default for new installations. Neither mode provides VM isolation. See [SECURITY.md](SECURITY.md).

> [!NOTE]
> Containers share the host kernel. Rootful Docker has host-root authority; rootless guests share one runtime account per node. Neither mode is suitable for hostile workloads. Delegated deployments require the Hardened policy. Administrator access is required for host namespaces, privileged containers, devices, external mounts, and custom drivers.
>
> Resource limits apply per service replica. Aggregate budgets cover configured allocations, while data-disk quotas do not cover all images, logs, writable layers, or snapshots. Snapshot and backup consistency depends on the application; keep independent backups.
>
> Migration requires downtime. Migration, replication, and HA require compatible runtime modes and storage. HA recovery also needs quorum and a complete replica on an eligible node.

## Installation

Set up [release verification](SECURITY.md#verify-a-release), then run as root on your PVE node:

```bash
curl -fsSL https://raw.githubusercontent.com/GDLev/Dockers-for-PVE/v0.20.0/install.sh | bash
```

You can also download and verify `install.sh` from [Releases](https://github.com/GDLev/Dockers-for-PVE/releases), then run `bash install.sh`.

Choose **Install / Update**, select the target nodes and runtime mode, and follow the prompts. Use Space to select nodes and Enter to continue. After installation, press Enter to reload the PVE panel and API services. Cluster installations use PVE's existing SSH access between nodes.

Refresh PVE with Ctrl+Shift+R. Dockers for Proxmox VE is now ready to use.

For unattended installation, run:

```bash
curl -fsSL https://raw.githubusercontent.com/GDLev/Dockers-for-PVE/v0.20.0/install.sh | bash -s -- install --runtime=rootless --yes
```

Add `--nodes=pve1,pve2` to select all cluster nodes on first installation, or `--no-restart` to defer service reload. Later updates and repairs can target individual installed nodes. For DEB installation, see [package installation](CONTRIBUTING.md#build-and-sign).

In a cluster, finish installation and reload the PVE services on every node before creating DCTs.

## Update and repair

To update, download and verify the installer from the release you want to install, then choose **Install / Update**. Back up your workloads before maintenance.

If the integration needs repair, choose **Repair** in the installer or run:

```bash
bash /var/lib/betterdocker/installer/source/start.sh repair --yes
```

Repair restores the extension's integration with PVE without changing its version or runtime mode. Check the integration after PVE updates and run Repair if needed. In a cluster, repair affected nodes before creating guests to keep guest IDs protected.

Updates keep your selected runtime mode. To switch modes, follow the separate [runtime conversion instructions](CONTRIBUTING.md#runtime-conversion).

## Uninstall

Back up and remove all DCTs, retained managed disks, DCT HA registrations and replication jobs across the cluster first. Uninstall is blocked while any DCT metadata or guest ID reservation remains, even on another node. Choose **Uninstall** in the installer, or run:

```bash
bash /var/lib/betterdocker/installer/source/start.sh uninstall --yes
```

For a DEB installation, use `apt remove proxmox-betterdockers` so the package and its repair triggers are removed too.

Uninstall removes the web/API integration and guest CLI. Docker, runtime configuration, remaining files and network/firewall maintenance remain. It does not erase external bind mounts or unrelated Docker workloads.

## Use the CLI and API

Run `dct` as root on a PVE node:

```bash
dct list
dct status 150
dct help
dct help create
```

See [API_DOCS.md](API_DOCS.md) for HTTP requests, task handling, permissions, and configuration routes. [CONTRIBUTING.md](CONTRIBUTING.md#installed-files) lists installed extension and guest data paths.

## License and security

Copyright 2026 GDLev and contributors. Licensed under the [GNU Affero General Public License, version 3 or later](LICENSE).

Report vulnerabilities privately to [security@gdlev.dev](mailto:security@gdlev.dev). See [SECURITY.md](SECURITY.md) for release verification and the security model.

Dockers for Proxmox VE is an independent community project, not an official Proxmox product. Proxmox VE is a product of Proxmox Server Solutions GmbH.
