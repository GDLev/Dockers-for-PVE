# Proxmox BetterDockers

BetterDockers lets you manage Docker projects from the Proxmox VE web interface: deploy Compose files or images, control services, view logs, open consoles, set limits and take snapshots.

Projects appear as **DCT** in the resource tree, with a guest ID and their own IPv4 address. Docker runs directly on the Proxmox node.

> [!WARNING]
> At its current stage of development, this project is recommended only for small installations, preferably on a single node. It is not recommended for commercial use or complex deployments. Do not rely on it for critical workloads.

![Docker project overview](assets/screenshots/demo.png)

## Installation

Run as **root** on a Proxmox VE 8 or 9 node with internet access. Tested on PVE 9.1.1.

```bash
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/raw/HEAD/install.sh | bash
```

The installer adds the required dependencies, including Docker and Compose when needed, and restarts the PVE web services. Refresh the interface with **Ctrl+Shift+R**, then click **Create DCT**.

Run the same command to update the extension. In a cluster, install it on every node.

## Uninstallation

Run these commands as **root**. They download the project files needed by the uninstaller:

```bash
betterdocker_dir=$(mktemp -d)
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/archive/HEAD/Proxmox-BetterDockers-HEAD.tar.gz \
  -o "$betterdocker_dir/source.tar.gz" &&
tar -xzf "$betterdocker_dir/source.tar.gz" --strip-components=1 -C "$betterdocker_dir" &&
bash "$betterdocker_dir/start.sh" uninstall
```

Uninstallation removes the web/API integration and restarts the PVE web services. **Docker, existing workloads, their data and network maintenance services remain on the node.** Removing a DCT through the interface also retains its persistent data; permanent data cleanup is a separate manual step.

## Extension files

These paths are on the Proxmox node after installation.

| Path | Contents |
| --- | --- |
| `/usr/local/lib/betterdocker/` | Deployment, networking and integration scripts. |
| `/usr/share/perl5/PVE/API2/BetterDocker.pm` | Extension API. |
| `/usr/share/pve-manager/js/betterdocker.js` | Web interface. |
| `/usr/share/pve-manager/js/betterdocker-locales/` | Separate English and Polish translations. |
| `/usr/share/pve-manager/css/betterdocker.css` | Interface styles. |
| `/usr/share/pve-manager/images/betterdocker.svg` | DCT icon. |
| `/etc/systemd/system/betterdocker-*` | Network maintenance and integration services and timers. |
| `/etc/apparmor.d/local/betterdocker-dhclient` | DHCP rules included by the distribution dhclient profile. Retained with network maintenance on uninstall. |
| `/var/backups/betterdocker/` | Files backed up by the installer. |

The installer also updates `/usr/share/pve-manager/index.html.tpl`, `/usr/share/perl5/PVE/API2/Nodes.pm`, `/usr/share/perl5/PVE/API2/Cluster.pm` and `/usr/share/perl5/PVE/Cluster.pm` to register the extension with PVE.

## DCT files

`<id>` below is the internal project identifier, not the numeric guest ID shown in the resource tree. Project files and snapshots are local to the node running the DCT; only metadata under `/etc/pve/` is shared through the PVE cluster filesystem.

| Path | Contents |
| --- | --- |
| `/etc/pve/betterdocker/<id>.json` | DCT metadata, including its guest ID and node. |
| `/etc/pve/betterdocker/ids/<vmid>.json` | Reservation of the numeric guest ID. |
| `/var/lib/betterdocker/projects/<id>/` | Deployment configuration, settings, notes and network state. |
| `/var/lib/betterdocker/projects/<id>/compose.source.yaml` | Imported Compose configuration. |
| `/var/lib/betterdocker/projects/<id>/.env` | Imported environment variables. |
| `/var/lib/betterdocker/projects/<id>/compose.runtime.json` | Generated configuration used to run the services. |
| `/var/lib/betterdocker/projects/<id>/pending.json` | Service settings waiting for Start or Reboot. |
| `/var/lib/betterdocker/projects/<id>/storage.ext4` | Optional data disk image. |
| `/var/lib/betterdocker/projects/<id>/storage-data/` | Mounted data disk containing managed application data. |
| `/var/lib/betterdocker/snapshots/<id>/<name>/` | Snapshot configuration, container images and application data. |

Without a managed data disk, application data stays in the bind-mount directories specified in Compose or in Docker volumes. Docker manages container layers, images and volumes in its configured storage location, usually `/var/lib/docker/`. External bind mounts can be elsewhere on the node.

## Limitations

- **Permissions:** CPU, memory, DNS and options can be delegated with `VM.Config.CPU`, `VM.Config.Memory`, `VM.Config.Network` and `VM.Config.Options` on `/vms/<vmid>`. Viewing a DCT requires `VM.Audit`; power operations use `VM.PowerMgmt`.
- **Host access:** DCTs use Docker and the host kernel. Compose, images, disks, deployment removal, logs and consoles require root or node `Sys.Console`. Per-DCT configuration permissions do not grant those operations.
- **PVE features:** native resource pools, guest hook scripts, VM/CT backup, HA, migration, replication and per-DCT firewall are unavailable. Back up application data separately.
- **Networking:** one IPv4 endpoint per DCT, used for published ports. Stop the DCT to change its bridge, static address/DHCP, MAC or MTU. Changes recreate stopped containers; volumes are retained, but writable container layers are replaced. Routing and outbound traffic use the host network. DHCP needs a reachable server and a network that allows an additional MAC address. Multiple interfaces, IPv6, VLAN tags, bandwidth limits, PVE guest firewall and SDN are unsupported. This endpoint does not provide LXC-style network isolation.
- **Compose:** use existing images and explicit published ports. `build`, profiles and `network_mode` are unsupported. Only YAML and `.env` can be imported; other referenced files must already exist on the host.
- **Resource limits:** CPU and memory limits apply per service instance. Supported resource limits and restart policies can be changed while running. Settings that cannot be changed while running remain pending until Start or Reboot. Revert discards pending changes. Deployment edits, data disk changes and Restore Compose require stopping the affected service or DCT. Writable-layer disk limits require `overlay2` on XFS with project quotas.
- **Data disk:** creating or enlarging it requires stopped services and private project directories or local volumes. The interface supports enlargement only. It does not reserve physical host space. A mount failure can prevent Docker from starting on the node.
- **Snapshots:** full copies consume additional space and freeze services while capturing data. RAM, Docker logs, external writable paths and shared volumes are unsupported. Address, bridge and MAC edits require removing existing snapshots; network edits after rollback require redeployment. Restoration leaves the DCT stopped. A later DHCP address change may require restoring again.
- **Startup:** Options controls Start at boot and start/shutdown order. Node Start All/Stop All includes DCTs. Order applies to stopped guests; already running services are skipped. Controlled Docker restarts preserve which instances were running. After an abrupt power loss or daemon crash, Docker restart policies can run before PVE ordering.
- **PVE updates:** changes to Proxmox can require an extension update or integration repair.

## Support the project

Bug reports and pull requests are welcome. You can also support development on Ko-fi.

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/gdlev)
