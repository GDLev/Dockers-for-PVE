# Proxmox BetterDockers

BetterDockers lets you manage Docker projects from the Proxmox VE web interface: deploy Compose files or images, control services, view logs, open consoles, set limits and take snapshots.

Projects appear as **DCT** in the resource tree, with a guest ID and their own IPv4 address. Docker runs directly on the Proxmox node.

> [!WARNING]
> At its current stage of development, this project is recommended only for small installations, preferably on a single node. It is not recommended for commercial use or complex deployments. Do not rely on it for critical workloads.

![Docker project overview](assets/screenshots/demo.png)

## Installation

Run as **root** on a Proxmox VE 9.1 or 9.2 node with internet access. Other versions are not currently supported.

```bash
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/raw/HEAD/install.sh | bash
```

The terminal installer lets you choose **Install / Update**, **Repair** or **Uninstall**, select one or more cluster nodes and choose rootful or rootless for new installations. Use the arrow keys, Space to select nodes and Enter to continue. It uses PVE's existing SSH trust to reach other nodes.

After applying changes, it shows a result and log path for each node. Press **Enter** at the final prompt to reload the PVE panel and API services. Then refresh the browser with **Ctrl+Shift+R** and click **Create DCT**. If you cancel that prompt, use the printed `finish` command later.

Run the same command to update or repair. Existing nodes keep their saved runtime mode. Rootless is the default for fresh installations. For an unattended installation, choose explicitly:

```bash
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/raw/HEAD/install.sh | bash -s -- install --runtime=rootless --yes
```

Add `--nodes=pve1,pve2` to target specific cluster nodes, `--no-restart` to defer service reload, or `--lang=pl` for Polish. Without a terminal, specify an operation; no menu is opened. Running `bash start.sh` from a downloaded checkout opens the same installer. DEB configuration and PVE update triggers keep their package workflow and reload services automatically.

Rootless runs Docker under the dedicated `betterdocker` account. PVE still manages host networking, storage and guest permissions as root. Both modes use the same web interface. Updates and repair preserve the saved choice; existing installations stay rootful. A DEB installation uses the package configuration choice.

Rootless needs cgroup v2, delegated CPU/memory/PID controllers, compatible Docker rootless binaries and unused `10.240.0.0/16` and `169.254.240.0/30` network ranges. The installer rejects incompatible setups. Rootless uses PVE-managed forwarding for published ports, including 80 and 443. Tested versions and results are recorded in [todo.md](todo.md).

Rootless containers use user namespaces and Docker's seccomp filtering. The Docker AppArmor profile is unavailable in this mode on the tested PVE kernels; containers use `apparmor=unconfined`. The extension does not disable host AppArmor.

## Changing runtime mode

Conversion is an offline maintenance operation for stopped DCTs with static addresses, owned local mounts, no snapshot history, no HA registration and no replication state disk. The destination runtime must be empty. Unrelated Docker workloads block conversion. Back up the node first.

```bash
# Dry run: list guests and blockers without changing the node.
/usr/local/lib/betterdocker/migrate-runtime.py --runtime=rootless
# Convert only after resolving all blockers.
/usr/local/lib/betterdocker/migrate-runtime.py --runtime=rootless --apply
```

Use `--runtime=rootful` for the other direction. The script retains the original runtime data and a journal under `/var/lib/betterdocker/runtime-migrations/<journal>/`. It prints the journal ID. After interruption, repeat with `--runtime=<target> --resume=<journal> --apply`. To return to the original data, stop the converted guests and run `--rollback=<journal> --apply`. Rollback discards changes made in the converted runtime. Keep the retained data until verification finishes; remove it manually only after a separate backup.

For a controlled rootless Docker restart, run `/usr/local/lib/betterdocker/runtime.py service restart` as root. This preserves the running guest set and rebuilds host forwarding.

## Uninstallation

Run the installation command again as **root** and choose **Uninstall**. For unattended removal:

```bash
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/raw/HEAD/install.sh | bash -s -- uninstall --yes
```

Remove DCT HA resources and replication jobs before uninstalling. Uninstallation removes the CLI and web/API integration. Interactive runs wait for Enter before reloading PVE services. **Docker, existing workloads, their data, network and firewall maintenance services remain on the node.** The uninstaller restores the restart policies declared for the services. Removing a DCT through the interface also retains its persistent data; permanent data cleanup is a separate manual step.

## CLI and API

Use `dct list`, `dct status <vmid>` and `dct --help` as root on a PVE node. See [API_DOCS.md](API_DOCS.md) for HTTP requests, task handling and CLI examples.

## Extension files

These paths are on the Proxmox node after installation.

| Path | Contents |
| --- | --- |
| `/usr/sbin/dct` | Guest CLI using numeric DCT IDs and the PVE API. Removed on uninstall. |
| `/usr/local/lib/betterdocker/` | Deployment, networking and integration scripts, including the native hook adapter. |
| `/usr/share/perl5/PVE/API2/BetterDocker.pm` | Extension API. |
| `/usr/share/perl5/PVE/BetterDocker/`, `/usr/share/perl5/PVE/VZDump/Docker.pm`, `/usr/share/perl5/PVE/HA/Resources/Docker.pm` | PVE firewall, backup, replication and HA providers. |
| `/usr/share/pve-manager/js/betterdocker.js` | Web interface. |
| `/usr/share/pve-manager/js/betterdocker-locales/` | Separate English and Polish translations. |
| `/usr/share/pve-manager/css/betterdocker.css` | Interface styles. |
| `/usr/share/pve-manager/images/betterdocker.svg` | DCT icon. |
| `/etc/systemd/system/betterdocker-*` | Network, firewall and integration services and timers. |
| `/etc/apparmor.d/local/betterdocker-dhclient` | DHCP rules included by the distribution dhclient profile. Retained with network maintenance on uninstall. |
| `/var/backups/betterdocker/` | Files backed up by the installer. |
| `/var/lib/betterdocker/installer/` | Private installation logs, cached installer source and pending panel reload state. Retained for recovery after uninstall. |
| `/etc/betterdocker/runtime.json` | Root-owned, node-local runtime mode, account, socket and UID/GID mapping. Retained on uninstall. |
| `/var/lib/betterdocker/runtime/.config/` | Rootless daemon and user service configuration. Retained on uninstall. |
| `/etc/systemd/system/user@<uid>.service.d/betterdocker.conf` | Rootless cgroup delegation and startup dependencies. Retained on uninstall. |

The installer also updates `/usr/share/pve-manager/index.html.tpl`, `/usr/share/perl5/PVE/API2/Nodes.pm`, `/usr/share/perl5/PVE/API2/Cluster.pm`, `/usr/share/perl5/PVE/Cluster.pm` and `/usr/share/perl5/PVE/VZDump.pm` to register the extension and protect DCT IDs from unsupported native operations. Backup, replication and HA integration also adds marked sections to their native PVE modules and `pvemanagerlib.js`. Repair and uninstall update these sections without restoring old package files.

## DCT files

`<id>` below is the internal project identifier, not the numeric guest ID shown in the resource tree. Project files and snapshots are local to the node running the DCT; only metadata under `/etc/pve/` is shared through the PVE cluster filesystem.

| Path | Contents |
| --- | --- |
| `/etc/pve/betterdocker/<id>.json` | DCT metadata, including its guest ID and node. |
| `/etc/pve/betterdocker/ids/<vmid>.json` | Reservation of the numeric guest ID. |
| `/etc/pve/firewall/<vmid>.fw` | Native PVE firewall rules, options, aliases and IP sets for the DCT. |
| `/var/lib/betterdocker/firewall/` | Last applied node rules and firewall status. Retained on uninstall to keep existing DCT protection. |
| `/var/lib/betterdocker/projects/<id>/` | Deployment configuration, settings, notes and network state. |
| `/var/lib/betterdocker/projects/<id>/compose.source.yaml` | Imported Compose configuration. |
| `/var/lib/betterdocker/projects/<id>/.env` | Imported environment variables. |
| `/var/lib/betterdocker/projects/<id>/compose.runtime.json` | Generated configuration used to run the services. |
| `/var/lib/betterdocker/projects/<id>/pending.json` | Service settings waiting for Start or Reboot. |
| `/var/lib/betterdocker/projects/<id>/ha.json` | Local HA enrollment marker. Docker restart policies stay disabled while enrolled. |
| `/var/lib/betterdocker/ha-recoveries/<id>.json` | Interrupted HA recovery ownership journal. |
| `/var/lib/betterdocker/hooks/<id>.json` | Guest hook execution journal used for interrupted lifecycle operations. |
| `/var/lib/betterdocker/projects/<id>/replication.json` | Owned replication state disk and successful target checkpoints. |
| `/var/lib/betterdocker/betterdocker-recovery-<id>.json` | Temporary ZFS recovery clone ownership. Retained if cleanup is interrupted. |
| `/var/lib/betterdocker/projects/<id>/storage.json` | Managed PVE data disk ID and mount mapping. Older installations may still use `storage.ext4`. |
| `/var/lib/betterdocker/projects/<id>/storage-data/` | Mounted data disk containing managed application data. |
| `/var/lib/betterdocker/snapshots/<id>/<name>/` | Snapshot configuration, container images and application data. |

Without a managed data disk, application data stays in the bind-mount directories specified in Compose or in Docker volumes. Docker manages container layers, images and volumes in its configured storage location, usually `/var/lib/docker/`. External bind mounts can be elsewhere on the node.

In rootless mode, Docker data lives in `/var/lib/betterdocker/runtime/docker/`, the socket is `/run/user/<uid>/docker.sock`, and the daemon runs as a systemd user service with lingering. Those files, the account, its subordinate-ID ranges and host maintenance remain after uninstall. Backups store container-visible owners so restores can use a different subordinate-ID range. Do not manually renumber the account or its ranges.

Permanent removal is a separate maintenance operation. Remove the DCTs and purge their retained managed disks with `/usr/local/lib/betterdocker/provision.py purge-retained-storage <project-id>` before uninstalling. Stop the rootless user Docker service, disable its host service and lingering, then run `/usr/local/lib/betterdocker/host.py remove` before deleting the runtime configuration. Remove the dedicated runtime directory, owned rootless units, user delegation, account and its subordinate-ID entries last. Keep shared networking and firewall helpers while any retained workloads still use them.

## Limitations

- **Delegation:** guest permissions are supported, with the additional restrictions below. Privileged deployments and host resources remain administrator-only. Rootful Docker has host root privileges. Rootless Docker shares one unprivileged runtime account across DCTs on a node; it does not provide a separate account or daemon for each guest. Rootless DCTs accept only project-owned bind mounts. API checks do not replace runtime isolation.
- **Resource pools:** DCTs use native PVE pools and inherit their guest permissions. Choose a pool when creating, cloning or restoring a DCT; use the pool's Members screen to add, move or remove existing guests. Migration keeps membership. Clones and restored guests join only the pool selected for the new guest.
- **Hook scripts:** root can select an executable script from snippet storage in Options. Scripts receive the numeric guest ID and `pre-start`, `post-start`, `pre-stop` or `post-stop`, as in LXC. A failed `pre-start` blocks startup; other failures produce task warnings. The snippet must exist on the destination before migration or restore. Hooks cover PVE-controlled guest operations, including boot and HA. Docker-internal restarts and individual service actions do not invoke guest hooks. If a hook is killed, inspect Task History and set or remove the hookscript to acknowledge it before retrying; its external side effects cannot be undone automatically.
- **Backup and restore:** native vzdump, backup jobs and PBS support DCTs, including Backup All. Restore creates a stopped DCT with a new guest ID and endpoint. Backups include deployment files, settings, images, writable layers and owned application data. External mounts, shared volumes, non-local volume drivers, custom Docker IPAM and fixed internal Docker addresses are unsupported. All service instances must exist before backup or cloning. Snapshots stay on the node and are not backups.
- **Clones and migration:** full clones have independent IDs, addresses and data. Templates must be stopped and support full clones only. Migration requires a stopped DCT, a static IPv4 endpoint and no snapshot history. Install the extension on both nodes and use the same runtime mode for migration, replication and HA. Automatic cross-mode transfers are rejected; use the offline conversion script instead. A replicated DCT needs a successful complete checkpoint on its destination; replication jobs reverse direction after the move.
- **Replication:** native PVE jobs and ZFS transport copy an additional disk containing deployment files, images, writable layers and owned data. Setup requires a managed ZFS data disk. Capturing state briefly pauses services and needs extra space. Snapshot history is not included. Remove HA management, replication jobs and their state disk before deleting a DCT.
- **HA:** the native PVE manager controls power and fencing. Install the extension on every cluster node. Recovery requires a successful complete replica and quorum after a node fails. Restrict HA placement to nodes with complete replicas using native node-affinity rules. Changes made after the replica can be lost. Relocation stops and transfers the DCT; it is not live migration. Automatic recovery passed a software-watchdog failure test on the test cluster. Production reliability and hardware watchdogs have not been verified.
- **Bulk operations:** native node and datacenter start, shutdown and migration include DCTs. Ordinary DCT migration requires a stopped guest; running guests are reported as ineligible. HA-managed DCTs use relocation with downtime. Suspend remains VM-only.
- **Networking:** one IPv4 endpoint per DCT, used for published ports. Changes made while running are queued until the next full DCT start or reboot and can be reverted. Stopped DCTs apply changes immediately. Apply or revert pending network changes before migration. Restored DCTs allow firewall changes; endpoint changes require redeployment. Changes recreate containers; volumes are retained, but writable container layers are replaced. Routing and outbound traffic use the host network. DHCP needs a reachable server and a network that allows an additional MAC address. Available Linux bridges and SDN VNets can be selected using native SDN permissions. The bridge must have a connected IPv4 subnet on the node. SDN zones using automatic IPAM or SDN DHCP allocation are unsupported and rejected. Multiple interfaces, IPv6, VLAN tags and bandwidth limits are unsupported. This endpoint does not provide LXC-style network isolation.
- **Compose:** use existing images and explicit published ports. `build`, profiles and `network_mode` are unsupported. Only YAML and `.env` can be imported; other referenced files must already exist on the host.
- **Resource limits:** CPU and memory limits apply per service instance. Supported resource limits and restart policies can be changed while running. Settings that cannot be changed while running remain pending until Start or Reboot. Revert discards pending changes. Deployment edits, data disk changes and Restore Compose require stopping the affected service or DCT. Writable-layer disk limits require rootful `overlay2` on XFS with project quotas. Rootless rejects writable-layer quotas; managed data disks still limit application data by filesystem capacity.
- **Data disk:** managed disks appear under Docker Volumes in the storage view. Supports local directory, LVM-thin and ZFS storage with `rootdir` content. Use named volumes before moving application data to a managed disk. Create, grow and move require stopped services; shrinking is unsupported. The initial data migration recreates containers to change their mounts, replacing their writable layers. Grow and move preserve existing containers. Remove snapshots before growing or moving. Moving retains the previous disk. Deleting a DCT retains its disks and reserves its guest ID. After removing its snapshots and DCT, root can permanently free those disks with `/usr/local/lib/betterdocker/provision.py purge-retained-storage <project-id>`. Thin disks do not reserve physical space. A mount failure can prevent Docker from starting on the node.
- **Snapshots:** LVM-thin and ZFS data disks use PVE storage snapshots; other data uses copies. Docker writable layers are saved separately in both cases. Capturing data briefly freezes services and requires additional space. RAM, Docker logs, external writable paths and shared volumes are unsupported. Rollback restores the captured network configuration and leaves the DCT stopped. Network edits after rollback still require redeployment; a later DHCP address change may require restoring again. After an interrupted rollback, retry that snapshot before starting the DCT.
- **Startup:** Options controls Start at boot and start/shutdown order. Node Start All/Stop All includes DCTs. Order applies to stopped guests; already running services are skipped. Controlled Docker restarts preserve which instances were running. For guests outside HA, Docker restart policies can run before PVE ordering after an abrupt power loss or daemon crash. HA-managed DCTs use the native HA manager instead of Docker restart policies or Start All ordering.
- **Firewall:** uses native PVE guest rules, macros, aliases, IP sets, security groups and logs. Enable the datacenter firewall, the DCT firewall and the network's Firewall flag. Incoming published connections use the endpoint address and published port in rules; direct container traffic uses its internal address and port. Outgoing and cross-DCT traffic is filtered. Services inside the same DCT can communicate internally. Filtering requires owned IPv4 Docker bridge networks. Privileged deployments and shared networks are rejected. LXC MAC/IP filters, NDP, router advertisements, IPS and per-guest DHCP exceptions are unsupported. IPv6 traffic across protected DCT boundaries is blocked. Rules are applied in a separate nftables table without replacing PVE or Docker tables. Firewall maintenance stays installed when the UI extension is uninstalled.
- **PVE updates:** changes to Proxmox can require an extension update or integration repair.

## Support the project

Bug reports and pull requests are welcome. You can also support development on Ko-fi.

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/gdlev)

## Permissions

Grant guest privileges on `/vms/<vmid>` or through a native resource pool. Creation needs `VM.Allocate` on the chosen ID, `/vms` or the selected `/pool/<poolid>`. Changing membership uses native pool permissions. Only `root@pam` can set or remove a hookscript.

| Operation | Privileges |
| --- | --- |
| View status and settings | `VM.Audit` |
| Read logs or open a shell | `VM.Console` |
| Start, stop or restart | `VM.PowerMgmt` |
| Edit CPU, memory or options | Corresponding `VM.Config.CPU`, `VM.Config.Memory` or `VM.Config.Options` |
| Edit networking, DNS or firewall rules | `VM.Config.Network` |
| Edit the complete deployment | All five: `VM.Config.CPU`, `VM.Config.Memory`, `VM.Config.Network`, `VM.Config.Disk`, `VM.Config.Options` |
| Create or resize a data disk | `VM.Config.Disk` and storage `Datastore.AllocateSpace` |
| Create or delete snapshots | `VM.Snapshot` |
| Roll back a snapshot | `VM.Snapshot.Rollback` |
| Clone | Source `VM.Clone`, destination `VM.Allocate`, applicable storage permissions |
| Backup and restore | Native backup/storage permissions; restore also needs destination `VM.Allocate` |
| Migrate | `VM.Migrate` and applicable storage permissions |
| Convert to template or remove | `VM.Allocate` |

Selecting a bridge requires native `SDN.Use` access to that bridge. Restricted API tokens use the intersection of user and token permissions. Interactive consoles require a user session.

Delegated Compose supports project-owned bind mounts, local volumes and bridge networks. Host namespaces, privileged mode, added capabilities, devices, Docker sockets, external resources and custom drivers require administrator access. Only the imported `.env` may be referenced as an environment file. Delegated pulls do not use the administrator's Docker credentials. Existing deployments with unsafe settings stay administrator-only for deployment, console and state-management operations.

Deployment editors can read imported environment variables and source files. Console users can see application secrets through logs or a shell. Backup access includes the secrets stored in that backup. Audit-only responses do not include deployment source or environment variables.
