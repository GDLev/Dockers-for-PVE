# Proxmox BetterDockers

BetterDockers adds Docker management to the Proxmox VE web interface. Create projects from Docker Compose or a single image, manage services, view logs, open a console, set resource limits, and take snapshots.

Projects appear in the resource tree as **DCT**, each with its own guest ID and IPv4 address. Docker runs directly on the Proxmox node; a DCT has no separate operating system.

## Installation

You need Proxmox VE 8 or 9 and an internet connection. Live testing has been performed on **PVE 9.1.1**; PVE 8 still needs separate validation.

Run this in the node's shell as **root**:

```bash
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/raw/HEAD/install.sh | bash
```

The installer downloads the project and required packages. It installs Docker Engine and Compose if a working installation is missing, then restarts the PVE web services.

Refresh the web interface with **Ctrl+Shift+R** and sign in as **root@pam**. Click **Create DCT**, enter your application configuration, and choose a network bridge. Use DHCP or an unused static address in the bridge's subnet.

For a cluster, install the extension on every node. Run the same installation command again to update it.

If you have downloaded the project files, install from that directory:

```bash
bash start.sh install
```

After a Proxmox update, the integration may need repair. Run `bash start.sh repair` from the project directory.

## Uninstallation

From the project directory, run as root:

```bash
bash start.sh uninstall
```

If you used the online installer and do not have the project files, download them temporarily:

```bash
betterdocker_dir=$(mktemp -d)
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/archive/HEAD/Proxmox-BetterDockers-HEAD.tar.gz \
  | tar -xz --strip-components=1 -C "$betterdocker_dir"
bash "$betterdocker_dir/start.sh" uninstall
rm -rf -- "$betterdocker_dir"
```

If you installed the `.deb` package, use:

```bash
apt remove proxmox-betterdockers
```

Uninstallation removes the extension from the web interface and API, then restarts the PVE web services. **Docker, applications, data, and the services maintaining their network remain installed.**

To remove applications too, remove their DCTs in the web interface first. Removing a DCT retains its project directory, volumes, snapshots, and data disk. Permanent cleanup is a separate host administration task.

## Limitations

- **Isolation:** containers use Docker and the host kernel. The extension is intended for administrators; Compose configurations can grant containers access to host resources.
- **PVE features:** DCTs do not support native VM/CT backups, HA, migration, replication, or a per-DCT firewall. Back up application data separately.
- **Networking:** IPv4 uses an existing Linux bridge. IPv6, VLAN configuration, and separate project gateways are unsupported. DHCP requires a reachable server and a network that allows an additional MAC address. An address change can recreate running containers.
- **Compose:** `build`, profiles, and `network_mode` are unsupported. Use existing images and explicit published ports, such as `8080:80`. The interface imports YAML and `.env`; other referenced files must already exist on the host.
- **Resource changes:** CPU and memory limits apply to each instance of the selected service. Changing service settings or DNS can recreate containers and interrupt applications.
- **Disk limits:** a container's writable-layer limit requires `overlay2` on XFS with project quotas. A separate project data disk can limit owned directories and local volumes; creating or enlarging it requires stopped services. Manual shrinking or removal through the interface is unsupported. Disk limits do not reserve physical host space, and retained data copies also consume space. A data disk mount failure can prevent Docker from starting on the node.
- **Snapshots:** snapshots are full copies, so they can consume substantial space and freeze services for longer than native storage snapshots. They exclude RAM, Docker logs, and the contents of external read-only mounts. External writable paths, shared volumes, custom volume drivers, special files, and nested filesystems are unsupported. Restoring leaves the DCT stopped. If DHCP changes its address after restoration, another restoration may be needed.
- **Proxmox updates:** the extension uses internal PVE components. Updates may require repairing the integration or updating the extension.

For more detail, see the [LXC compatibility notes](docs/parity-lxc.md) and [validation results](docs/validation-pve9.md).

## Screenshots

Captured on a live Proxmox VE 9.1.1 demo node.

**Project overview**

![Docker project overview](docs/screenshots/demo-summary.jpg)

**Compose services**

![Nginx and Redis services](docs/screenshots/demo-services.jpg)

**Resource limits and data disk**

![Service resource limits and project data disk](docs/screenshots/demo-resources.jpg)

**Snapshots**

![Native snapshot tree](docs/screenshots/demo-snapshots.jpg)
