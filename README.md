# Proxmox BetterDockers

Run Docker directly on a Proxmox VE node and manage projects from its web interface. BetterDockers adds **Create Docker**, a three-step wizard, Docker resources in the machine tree with a white icon, and a dedicated management screen.

There is no OS template, guest OS, LXC, VMID or separate VM to configure. Docker Engine and Compose run on the Proxmox host. Each project publishes its ports through a dedicated additional IPv4 address on a selected Linux bridge, rather than the Proxmox management IP.

## Install

Run as **root on a Proxmox VE 8 or 9 host**:

```bash
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/raw/HEAD/install.sh | bash
```

The remote script downloads the default branch into a temporary directory, runs `start.sh`, and removes the download. No Git installation is required. The raw script and archive must be available without login. For a private repository, download an authenticated copy and run `bash start.sh install` locally.

The installer runs `apt-get update`, installs Python and networking dependencies, and installs Docker Engine plus Compose from Docker's Debian repository when necessary. An existing working Engine and Compose installation is reused. A new engine is configured with `ip-forward-no-drop` so Docker does not change the host's default forwarding policy to DROP. Existing engine configuration is preserved.

Install on every node in a cluster. The installer checks the expected Proxmox file structure, saves integration backups under `/var/backups/betterdocker/`, and restarts `pvedaemon` and `pveproxy`. After installation, refresh the browser with **Ctrl+Shift+R** and sign in as **root@pam**. Docker management routes are restricted to that account.

Local commands:

```bash
bash start.sh check
bash start.sh install
bash start.sh repair
bash start.sh uninstall
```

`check` requires Python to be present and does not install packages. `repair` reapplies the integration after Proxmox package updates. Running the remote installer again downloads and installs the current version.

## Create a project

The wizard is in English and has three steps:

1. **Deployment** — choose the node, project name and Docker Compose or Docker image. Compose is the default. Paste your YAML and optionally provide `.env` contents. Image and port fields appear only in image mode.
2. **Network** — choose a Linux bridge and an unused IPv4 address within its connected subnet, outside its DHCP pool.
3. **Review** — review the project and address, then deploy. Progress appears in the standard Proxmox task viewer.

Example Compose YAML:

```yaml
services:
  web:
    image: nginx:stable
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - web-data:/usr/share/nginx/html
volumes:
  web-data:
```

If the project IP is `192.0.2.50`, the application is available at `http://192.0.2.50:8080`. Compose defines images, ports, environment, internal networks and volumes. You do not re-enter these settings in separate wizard steps. Image mode accepts an image such as `nginx:stable` and one port mapping per line:

```text
8080:80
5353:53/udp
```

## Dedicated IP and networking

The project IP is a `/32` alias on the selected host bridge. Docker's published ports are bound to that alias by converting the supplied Compose configuration into a canonical runtime configuration and setting every published port's `host_ip`. The original YAML is retained. Port targets and protocols remain as supplied.

The application has a separate address from the Proxmox management address, while still using the host's network stack and Docker's normal forwarding/NAT. This is not macvlan or a separately isolated network namespace for the entire project. Containers keep their normal Docker internal addresses, and services in one project share its external IP through distinct published ports. Outgoing traffic uses the normal Docker/host routing path.

The installer does not change `/etc/network/interfaces`. A systemd service restores project IP aliases after the Proxmox cluster filesystem and network are available, before Docker starts. Application startup after reboot follows the `restart` policy in your Compose YAML; image mode uses `unless-stopped`.

Before assigning an IP, the backend rejects host addresses, existing project reservations and addresses outside the selected bridge subnet, then performs ARP duplicate-address detection. ARP probing cannot guarantee that an offline device or a future DHCP lease will not use that address, so choose a reserved static address. Network/firewall behavior must be verified on your actual Proxmox bridge and firewall configuration.

Compose `network_mode` bypasses the managed port-binding model and is rejected. Build contexts and profile selection are not supported; supply image-based services without profiles. Use absolute host paths for bind mounts. Referenced local config/secret files must already exist on the node; the UI uploads YAML and `.env` only. Published ports must specify a published value, such as `8080:80`, rather than requesting a random host port.

## Manage projects

Docker resources appear under their owning node in the normal machine tree. Selecting one opens its own screen with **Start**, **Stop**, **Restart**, **Edit deployment**, **Refresh**, **Remove**, and **Logs**. Edit deployment changes the YAML/image settings and reapplies Compose while retaining the project name, bridge and IP. Removed services are stopped and removed on redeployment; named Docker volumes are retained.

Project IDs are UUIDs, independent of VM/CT IDs. Public summaries are stored in `/etc/pve/betterdocker/` for cluster resource discovery. Deployment YAML, `.env` and canonical runtime configuration are root-only local files under `/var/lib/betterdocker/projects/<id>/`. A timer refreshes statuses every 15 seconds. Resource discovery does not expose YAML or environment secrets.

For host-console access to a project:

```bash
cd /var/lib/betterdocker/projects/PROJECT_ID
docker compose -p bd-PROJECT_ID -f compose.runtime.json ps
docker compose -p bd-PROJECT_ID -f compose.runtime.json logs --tail=100
```

Failed deployments remain visible for inspection, editing and retrying. Removing a project runs Compose down, releases its dedicated IP and removes its managed configuration. Docker volumes are retained. This is a host Docker workload: native VM/CT backup, HA, migration, console and resource-limit controls do not apply. Configure resource limits in Compose and back up the Docker volumes and managed deployment files separately.

## Upgrade from the first LXC release

Previously created Docker-in-LXC machines remain LXC machines with their data. Their Docker tag still supplies a white Docker icon. This release creates new projects directly on the host; it does not automatically migrate or delete earlier LXC guests.

## Uninstall

`bash start.sh uninstall` removes the web/API integration and stops status refresh. It leaves Docker Engine, projects, volumes, configuration and the IP-restoration service in place so existing applications can continue running. It does not revert Proxmox package files to older backup versions. Remove unwanted projects from the Docker screen before uninstalling the integration.

## Validation

Local validation covers Compose-driven setup without an image field, dedicated-IP port binding, preservation of internal networks and volumes, rejection of the management IP, ARP probe failures, secret file permissions, fail-before-allocation behavior, API permissions, Docker resource discovery, reversible package patches, and ExtJS compatibility.

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh start.sh scripts/docker-install.sh
node --check frontend/betterdocker.js
node tests/test_frontend.js
```

The installer supports the structure of Proxmox VE 8/9. **Full installation, GUI behavior, boot persistence and traffic forwarding still require validation on a live Proxmox node.** The integration uses internal ExtJS classes and patches `Nodes.pm`, `Cluster.pm` and `index.html.tpl`; package upgrades may require `repair` or an addon update.

Sources: [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/), [Docker forwarding policy](https://docs.docker.com/engine/network/packet-filtering-firewalls/), [Docker installation on Debian](https://docs.docker.com/engine/install/debian/), [Proxmox cluster resource API](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm).
