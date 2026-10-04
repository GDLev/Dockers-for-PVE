# Proxmox BetterDockers

Run Docker directly on a Proxmox VE node and manage projects from its web interface. BetterDockers adds **Create Docker**, a three-step wizard, Docker resources in the machine tree with a white icon, and a dedicated management screen.

There is no OS template, guest OS, LXC, VMID or separate VM to configure. Docker Engine and Compose run on the Proxmox host. Each project publishes its ports through a dedicated IPv4 address on a managed interface attached to a selected Linux bridge, rather than the Proxmox management IP. Choose DHCP or static IPv4.

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
2. **Network** — an LXC-style two-column screen with Bridge, MAC address, MTU, and an IPv4 Static/DHCP selector. DHCP is the default. Static mode enables IPv4/CIDR, for example `192.0.2.50/24`. The gateway display reflects host routing; it does not change the host gateway.
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

New projects get a separate macvlan interface on the selected Linux bridge, with a stable generated MAC or the supplied MAC. IPv4 addresses are configured as `/32` addresses because forwarding uses the host's existing routes. The bridge must have a connected IPv4 subnet; this release does not configure a different per-project default gateway, VLAN or IPv6 network.

In DHCP mode, a dedicated systemd-managed DHCP client obtains and renews a lease for the project MAC. Its custom hook configures only the project interface and records the lease. It never applies DHCP gateway or DNS settings to Proxmox. The DHCP server must be reachable on the selected bridge, and the surrounding network must allow an additional MAC address. The first deployment waits up to 60 seconds for a lease and retains the project with an error if none arrives.

Docker's published ports are bound to the assigned address by converting the supplied Compose configuration into a canonical runtime configuration and setting every published port's `host_ip`. The original YAML is retained. Port targets and protocols remain as supplied. If DHCP changes the address, the maintenance timer updates the port bindings and recreates running services. If a lease expires, the project is stopped until a valid lease is available; an explicitly stopped project is not restarted merely because its address changes. A DHCP reservation avoids address changes and reconnections.

The project has a separate address from Proxmox management but still uses the host network stack and Docker's normal forwarding/NAT. Containers keep their normal Docker internal addresses, and services in one project share its external IP through distinct published ports. Outgoing traffic uses the normal Docker/host routing path. Existing alias-only projects from the previous release keep their old networking without automatic migration.

The installer does not change `/etc/network/interfaces`. A systemd service restores interfaces and starts project DHCP clients after the cluster filesystem and network become available, before Docker starts. Valid cached leases can be restored; expired leases must be renewed. The maintenance timer reconciles late DHCP leases and IP changes. Application startup after reboot follows the `restart` policy in your Compose YAML; image mode uses `unless-stopped`.

For static IPv4, choose an unused address within the bridge subnet, outside its DHCP pool. The backend rejects host addresses, existing project reservations and addresses outside the selected subnet, then performs ARP duplicate-address detection. Network forwarding, DHCP and firewall behavior require validation on your actual Proxmox bridge and firewall configuration.

Compose `network_mode` bypasses the managed port-binding model and is rejected. Build contexts and profile selection are not supported; supply image-based services without profiles. Relative bind mounts such as `./data:/data` resolve under the permanent project directory `/var/lib/betterdocker/projects/<id>/`, and retain the same paths on redeployment. Absolute host paths are also supported. Directory bind mounts follow normal Compose creation behavior. Referenced file mounts, configs and secret files must already contain the required files; pasting YAML does not upload or generate those file contents. The UI uploads YAML and `.env` only. Published ports must specify a published value, such as `8080:80`, rather than requesting a random host port.

## Manage projects

Docker resources appear under their owning node in the normal machine tree. Selecting one opens the native Proxmox vertical navigation layout with **Summary**, **Console**, **Resources**, **Network**, **DNS**, **Options**, **Deployment**, **Logs**, and **Task History**. The top toolbar provides Start, Stop, Restart, Console and a More menu for deployment editing, refresh and removal.

Summary displays service/container state and live Docker CPU, memory, network and disk I/O usage. Resources uses an LXC-style settings grid and edit dialog for memory, swap, CPU limit, CPU units and PID limit, alongside a mount list. Select a Compose service first: these limits apply to each container of that service, not to an aggregate project budget. A zero CPU/memory/PID limit means unlimited; with a memory limit, zero swap disables swap and -1 permits unlimited swap. CPU units map to Docker CPU shares. DNS edits container DNS servers/search domains; Options edits restart policy.

Applying settings recreates running containers of the selected service. Stopped services remain stopped, and dependencies are not restarted. Persistent GUI overrides take precedence over the corresponding settings in Compose YAML, including after redeployment. Mounts and published ports can be edited through Deployment. Network shows the dedicated interface settings and port mappings; project bridge/MAC/IP mode stay fixed.

Console provides an interactive xterm.js terminal through Proxmox's authenticated termproxy/websocket transport. Select a running container (including the instance for replicated services), choose `/bin/sh` or `/bin/bash` and Connect. The selected shell must exist in the image. It runs inside that container using its default user. Task History lists recent API operations and console sessions for this project; double-click opens the normal task viewer. Tasks created by earlier versions without a project ID are not included.

 Edit deployment changes the YAML/image settings and reapplies Compose while retaining the project name and network settings; DHCP may update the assigned IP. Removed services are stopped and removed on redeployment; named Docker volumes are retained.

Project IDs are UUIDs, independent of VM/CT IDs. Public summaries are stored in `/etc/pve/betterdocker/` for cluster resource discovery. Deployment YAML, `.env` and canonical runtime configuration are root-only local files under `/var/lib/betterdocker/projects/<id>/`. A timer refreshes statuses every 15 seconds. Resource discovery does not expose YAML or environment secrets.

For host-console access to a project:

```bash
cd /var/lib/betterdocker/projects/PROJECT_ID
docker compose -p bd-PROJECT_ID -f compose.runtime.json ps
docker compose -p bd-PROJECT_ID -f compose.runtime.json logs --tail=100
```

Failed deployments remain visible for inspection, editing and retrying. Removing a project runs Compose down, releases its dedicated IP/interface and removes its cluster entry. Docker volumes and the local project directory, including bind-mount data and deployment configuration, are retained for recovery or manual cleanup. This is a host Docker workload: native VM/CT backup, HA, replication, snapshots, migration and firewall controls do not apply. Back up the Docker volumes and managed deployment files separately.

## Upgrade from the first LXC release

Previously created Docker-in-LXC machines remain LXC machines with their data. Their Docker tag still supplies a white Docker icon. This release creates new projects directly on the host; it does not automatically migrate or delete earlier LXC guests.

## Uninstall

`bash start.sh uninstall` removes the web/API integration. It leaves Docker Engine, projects, volumes, configuration and interface restoration and DHCP/port-binding maintenance services in place so existing applications can continue running. It does not revert Proxmox package files to older backup versions. Remove unwanted projects from the Docker screen before uninstalling the integration.

## Validation

Local validation covers service-scoped resource overrides, stopped-service preservation, console target ownership, native management navigation, and the real Compose parser for relative bind mounts and `.env` resolution during edits, DHCP hook isolation, lease-driven port rebinding, and retaining bind data on removal. It also covers Compose-driven setup without an image field, dedicated-IP port binding, preservation of internal networks and volumes, rejection of the management IP, ARP probe failures, secret file permissions, fail-before-allocation behavior, API permissions, Docker resource discovery, reversible package patches, and ExtJS compatibility.

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh start.sh scripts/docker-install.sh
node --check frontend/betterdocker.js
node tests/test_frontend.js
```

The installer supports the structure of Proxmox VE 8/9. **Full installation, GUI behavior, boot persistence and traffic forwarding still require validation on a live Proxmox node.** The integration uses internal ExtJS classes and patches `Nodes.pm`, `Cluster.pm` and `index.html.tpl`; package upgrades may require `repair` or an addon update.

Sources: [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/), [Docker forwarding policy](https://docs.docker.com/engine/network/packet-filtering-firewalls/), [Docker installation on Debian](https://docs.docker.com/engine/install/debian/), [Proxmox cluster resource API](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm).
