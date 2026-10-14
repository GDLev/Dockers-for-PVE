# HTTP API

BetterDockers extends the PVE API on port 8006. Use HTTPS with the node's trusted certificate and an ordinary PVE API token. Tokens need the same guest, pool and storage permissions as the web interface. With privilege separation enabled, both the user and token must have those permissions.

```bash
export PVE_URL=https://pve.example:8006
export PVE_TOKEN='user@pve!automation=your-token-secret'
curl --fail --silent --show-error \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN" \
  "$PVE_URL/api2/json/cluster/resources?type=vm"
```

Find an entry with `type: "docker"`. Its `vmid` is the visible guest number. Its `id` is `docker/<id>`, where `<id>` is the internal project identifier. Use that identifier and the current `node` in the following routes. Resolve them again after migration.

Responses use PVE's `{"data": ...}` envelope. The table below uses `G=/nodes/<node>/docker/<id>`, relative to `/api2/json`.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/nodes/<node>/docker` | List visible DCTs on a node. |
| GET | `/nodes/<node>/docker?diagnostics=1` | Administrator diagnostics, including the node's `runtime` and `runtime_health`. Returns an array with one diagnostic object. |
| POST | `/nodes/<node>/docker` | Create a DCT; send `deployment` as a JSON-encoded string and optional `pool` and `start`. Omitted `start` starts the DCT immediately. |
| GET | `G/management` | Status, services, capabilities and configuration digest. |
| GET / PUT | `G/config` | Notes, tags, protection, startup and hook configuration. |
| GET / PUT | `G/network` | Endpoint settings, pending changes and firewall flag. PUT `revert=1` with `digest` to discard pending network changes. |
| GET / PUT | `G/pending` | Service effective/pending values. PUT `service`, comma-separated `revert` keys and `digest` to discard selected changes. |
| GET / PUT | `G/security-policy` | Read policy and conversion incompatibilities. PUT `mode=standard` or `mode=hardened` with `digest`; requires node `Sys.Modify` and a stopped DCT. Conversion preserves writable files and volumes. |
| PUT | `G/settings` | Service settings; send `service`, JSON-encoded `settings` and `digest`. |
| POST | `G/images/preview` | Pull images and prepare a preview; send JSON-encoded `images` mapping service names to image references, plus `digest`. |
| GET | `G/images` | Read the prepared image IDs, token and recovery state. |
| POST | `G/images` | Update stopped services; send preview `token`, `digest` and a new `recovery` snapshot name. Requires configuration, snapshot and rollback privileges. |
| POST | `G/action` | `action`: `start`, `shutdown`, `force-stop` or `reboot`. Optional `timeout` in seconds. |
| POST | `G/migrate` | Move to `target`; running DCTs require `restart=1`. Optional `storage`, `timeout` and `digest`. HA uses native relocation. |
| POST | `G/clone` | Clone with `newid`, JSON-encoded destination `deployment`, optional `pool`, `storage` and `digest`. `full=0` requests an LVM-thin linked clone of a template. |
| GET | `G/snapshots` | List snapshots. |
| GET / PUT | `G/snapshot/<name>/config` | Snapshot metadata. PUT `description` with the digest returned by GET. |
| POST | `G/snapshot` | `action`: `create`, `restore` or `delete`; also send `snapname`. |
| GET / POST | `G/firewall/rules` | Read or add native PVE firewall rules. |
| PUT / DELETE | `G/firewall/rules/<position>` | Edit or remove a firewall rule. |
| GET / PUT | `G/firewall/options` | Read or edit firewall options. |
| GET | `G/rrddata?timeframe=hour&cf=AVERAGE` | Native PVE RRD history. Ranges: hour, day, week, month, year, decade. |
| DELETE | `G/volumes/<vmid>?volume=<volid>` | Remove one unused or retained DCT disk. |
| DELETE | `G` | Remove the DCT. Set `destroy-owned-data=1` to remove its owned data; otherwise retain it. Protection and HA restrictions apply. |

Network changes on a running DCT are queued. GET returns the effective fields and a `pending` object; `/management` includes `pending_network`. A full DCT start, reboot or restart applies them after all containers stop. Restarting one service does not apply them. Failed application keeps the pending changes and restores the previous network. Stopped guests apply edits immediately. Apply or revert pending network changes before migration. Clone/restore use the destination endpoint and discard the source's pending endpoint. Endpoint edits preserve writable container layers, including after snapshot rollback.

## Start a guest and follow its task

```bash
export G=/nodes/pve1/docker/your-project-id
curl --fail --silent --show-error \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN" \
  --data-urlencode action=start \
  "$PVE_URL/api2/json$G/action"
```

Long operations return a task identifier, `UPID:...`. A successful POST means the task was accepted, not that it finished. Read `GET /nodes/<task-node>/tasks/<upid>/status` until `status` is `stopped`, then check `exitstatus` is `OK`. Read `/log` on the same task for details. `DELETE /nodes/<task-node>/tasks/<upid>` requests cancellation. Cancellation does not undo completed work.

For updates that accept `digest`, use the value from the latest GET. A stale digest rejects the update; reload the configuration before retrying. Do not automatically retry a create or clone request after a lost response: first check the guest ID and task list.

## Create from Compose

Save the deployment as `deployment.json`:

```json
{
  "vmid": 150,
  "name": "web",
  "mode": "compose",
  "compose": "services:\n  web:\n    image: nginx:stable\n    ports:\n      - '8080:80'\n",
  "bridge": "vmbr0",
  "ipv4mode": "static",
  "cidr": "192.0.2.150/24"
}
```

Replace the bridge and address with an available address on your network.

```bash
curl --fail --silent --show-error \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN" \
  --data-urlencode deployment@deployment.json \
  "$PVE_URL/api2/json/nodes/pve1/docker"
```

Use native PVE routes for pools, backup jobs, storage, replication, HA and tasks. DCT HA resource IDs use `docker:<vmid>`. The installed PVE API schema describes each route's accepted parameters. Deployment source and environment variables require configuration privileges; audit access does not expose them.

Runtime mode is selected when installing on each node. Diagnostics are read-only; there is no mode-changing API. Migration, replication and HA require matching runtime modes. Backups can be restored in either mode when the deployment meets the destination's restrictions. Rootless rejects external bind mounts and writable-layer quotas. It does not fall back to the rootful socket when an operation fails.

## Local CLI

Run `dct` as root on an extension-enabled PVE node. It resolves the numeric guest ID and calls the PVE API, including when the owner is another node.

```bash
dct list
dct status 150
dct create 150 deployment.json
dct start 150
dct shutdown 150 --timeout 60
dct migrate 150 pve2
dct snapshot 150 before-update --description "Before application update"
dct set 150 --onboot 1 --tags 'web;demo'
dct set 150 --service web --memory 256 --cpulimit 1
dct listsnapshot 150
dct delsnapshot 150 before-update
dct help
dct help create
```

CLI creation leaves the DCT stopped unless `--start 1` is supplied. Commands wait for asynchronous tasks by default and return a nonzero exit status on failure. Interrupting the CLI stops waiting; use `dct task stop 'UPID:...'` to request cancellation. `dct destroy 150` removes the guest while retaining persistent data. `pct` does not manage DCTs.

`dct` uses the same PVE command handler as `pct`, including `help`, positional guest IDs and boolean options such as `--onboot 1`. `list` shows the local node; `--cluster 1` shows the cluster. `status` prints `status: running` or `status: stopped`. Use `--output-format json` for scripts.

The source for `create` can also be a Compose file or a Docker image:

```bash
dct create 150 compose.yaml --hostname website --net0 bridge=vmbr0,ip=192.0.2.150/24
dct create 151 nginx:stable --hostname proxy --net0 bridge=vmbr0,ip=192.0.2.151/24 --ports 80:80
dct clone 150 152 --hostname website-copy --net0 bridge=vmbr0,ip=192.0.2.152/24
```

Resource options apply per service instance. Omit `--service` for a single-service DCT. Multi-service DCTs require a service name when changing resources. Docker-specific runtime and networking restrictions still apply; LXC-only commands and options are not advertised by `dct help`.

## Project resource budgets

`GET /nodes/{node}/docker/{id}/limits` returns `cpus`, `memory` in MiB, `pids` and a configuration `digest`. Budgets may also include `services`, `replicas` and `exempt`. Reading requires `VM.Audit`.

`PUT` to the same path sets the resource ceilings. Send `cpus`, `memory`, `pids` and the last `digest`. Optional `services` and `replicas` limit the number of services and total replicas. Optional `exempt=1` explicitly exempts a DCT from aggregate budgets. Changing any of these values requires guest configuration privileges and `Sys.Modify` on the node. The DCT must be stopped, outside HA and not a template.

For administrator-managed budgets, zero removes a ceiling. New delegated DCTs require positive ceilings unless an administrator explicitly enables the exemption. An exemption does not remove existing container caps or the Hardened security policy.

For example, with the same authentication headers used above:

```http
PUT /api2/json/nodes/pve1/docker/{id}/limits
Content-Type: application/x-www-form-urlencoded

cpus=2&memory=1024&pids=512&digest={digest}
```

Every service must have a finite limit for each active ceiling. The extension sums those limits across all declared replicas and rejects configurations exceeding the budget. It checks pending edits, deployment changes, saved containers and snapshot rollback. Docker enforces the individual container caps; unused capacity is not shared dynamically between services. CPU and memory reservations are not hard limits.

Start, edits, scaling, clone and migration also check actual container caps through Docker inspection. Replacement checks the proposed Engine bodies together with unchanged containers before preparing resources. Clone and restore validate saved caps before creating the destination guest. New delegated clones and restores cannot inherit an administrator exemption; their inherited ceilings are capped by the destination node's defaults. Migration retains the existing guest's approved budget. These are per-DCT budgets, not a shared node-wide reservation.

Budgets are preserved in backups, clones and migration. Snapshot rollback retains the current administrator budget. Changes are synchronous and return the saved ceilings, without a task dialog.

From the node CLI:

```sh
dct limits 100
dct limits 100 --cpus 2 --memory 1024 --pids 512
dct limits 100 --exempt 1
```

### Defaults for new delegated DCTs

Creation without node `Sys.Modify` supplies missing per-container caps: 1 CPU, 512 MiB RAM and 128 PIDs. The DCT receives an aggregate budget of 4 CPUs, 4096 MiB RAM and 1024 PIDs, with at most 8 services and 16 total replicas. Explicit Compose caps are retained and checked against that budget. An explicit unlimited value is rejected rather than replaced silently.

The node administrator can change future defaults in root-owned `/etc/betterdocker/resource-policy.json`, with permissions `0600`:

```json
{
  "container": {"cpus": 1, "memory": 512, "pids": 128},
  "project": {"cpus": 4, "memory": 4096, "pids": 1024, "services": 8, "replicas": 16}
}
```

All defaults and ceilings must be positive; one container's defaults must fit within the project budget. Each new delegated DCT stores its budget in `limits.json` and its container defaults in `resource-defaults.json` in its project directory. Later changes to node defaults do not rewrite existing DCTs. Node administrators creating DCTs retain the existing explicit-budget behavior.

## Node operation limit

Each node admits at most two expensive DCT operations at once by default. This includes deployment/image previews, image pulls and commits, snapshots, export/import and migration. Nested work and inherited local helpers share one reservation; each migration node reserves its own place. A full node returns `DCT operation limit reached on this node; retry after another task finishes`. Stop and status reads do not require a reservation.

The node administrator can set a different limit in root-owned `/etc/betterdocker/operation-policy.json`, with permissions `0600`:

```json
{"max_concurrent": 2}
```

The value must be an integer from 1 to 64. Lowering it counts operations already running and prevents new admission until capacity is available. It does not cancel existing work. Kernel locks retain the reservation while an inherited child is active and release it when its last descriptor closes. This limits extension workers; it does not impose CPU or I/O quotas on the Docker daemon or workloads.

## Retention budgets

Root-owned `/etc/betterdocker/retention-policy.json` sets local artifact budgets. Use permissions `0600`. Without this file, the defaults are:

```json
{
  "snapshots": 16,
  "snapshot_bytes": 34359738368,
  "preserved_image_bytes": 8589934592,
  "log_file_bytes": 10485760,
  "log_files": 3,
  "transfer_bytes": 34359738368,
  "node_transfer_bytes": 68719476736
}
```

Byte limits are positive integers. Snapshot count and file bytes apply per DCT; preserved images have a separate per-DCT budget. Image accounting conservatively charges complete unique image sizes, including shared layers, plus projected new filesystem images. Native PVE disk snapshots are outside the snapshot file-byte budget; exported raw disks still count as temporary transfer data.

New or recreated containers using `json-file` or `local` logging require rotation within the configured file size/count. Privileged external logging drivers are outside this local rotation policy. Existing containers are not recreated automatically.

Transfers reserve the full per-operation allowance atomically against the node budget. Staging, unpacked state, copied history and the packed archive share that allowance. Leftover registered files and existing node transfer directories remain charged after a worker exits. Incoming archives stop at the byte limit; export/import also checks estimates and actual staged sizes. These are admission and retained-artifact checks, not filesystem quotas for arbitrary application writes.

An exceeded budget rejects the operation. It does not prune snapshots, delete application data or discard recovery files. Remove artifacts explicitly or increase the administrator policy. Free-space and peak-storage checks are separate from these retention budgets.

Before registering temporary transfer data, cleanup removes abandoned, explicitly enrolled `state.partial` receive files whose file and directory identities still match. It retains a directory containing an archive, extracted state, metadata or any other recovery file. Unregistered files are retained. Empty accounting records are removed; completed archives remain charged until explicitly removed. Admission locks and space reservations stay occupied while an inherited worker still holds its descriptor, even after the main worker exits. Destination space loans also track their active receiving workers.

A node administrator can run `/usr/local/lib/betterdocker/provision.py cleanup-temporary` to perform the same cleanup and remove inactive peak-space accounting records. It does not call Docker, recover guests, delete images or remove completed archives. Repeating it is safe.

## Peak-space checks

Before backup quiescence, snapshots, rollback, filesystem preservation and disk allocation, the extension plans temporary copies and reserves free capacity on the node. Paths on the same filesystem share one total. Directory storage joins that filesystem total; native storage uses PVE's reported available capacity, grouping aliases of the same thin or ZFS pool. Disk growth reserves the added size, rather than the disk's existing allocation.

Root-owned `/etc/betterdocker/space-policy.json`, with permissions `0600`, configures the safety margin:

```json
{"reserve_bytes": 1073741824, "overhead_percent": 10}
```

The defaults leave 1 GiB plus 10% of the estimated new space. Reservations are atomic and remain associated with the active DCT operation or helper. Native backup helpers retain the reservation through the parent PVE operation. An unavailable measurement or insufficient capacity rejects the operation before the guarded stop, pause or replacement.

Migration holds a destination reservation over SSH before stopping the source. It updates that plan using the generated archive's size before sending data. Receiving and preparing the transfer share the held destination reservation. If the destination check fails, transfer does not proceed.

Estimates include overlapping staging/archive data and Docker filesystem copies. Native allocation is accounted conservatively against storage capacity. These reservations coordinate extension operations; other host processes can still consume space, and growing source data may require a larger estimate. Retention budgets and filesystem quotas remain separate.

## Removing data

`DELETE G?destroy-owned-data=1` removes project files, managed disks and local Docker volumes owned exclusively by that DCT. External bind mounts and external volumes remain. Shared volumes, protection, snapshots, HA, replication and unfinished transfers prevent removal. Keep the default if you need recovery data.

The Docker Volumes storage tab can remove individual unused or retained PVE disks. Attached disks cannot be deleted there. For an existing DCT, removal requires `VM.Config.Disk` and `Datastore.AllocateSpace`. Retained disks require node `Sys.Modify` and storage `Datastore.AllocateSpace`, because the deleted guest no longer has ACLs.

The CLI accepts `dct destroy 150 --destroy-owned-data 1`. Deletion is permanent; take a backup first.

Charts use PVE's native guest RRD archives and consolidation. Long ranges fill as new measurements arrive; the previous JSON history is not imported.

## Migration and network interfaces

`POST /nodes/<node>/docker/<id>/migrate` accepts `target`, optional `storage`, `restart=1` and `timeout` in seconds. Running guests require restart mode: shutdown, transfer, then start the instances that were previously active. Stopped guests remain stopped. Snapshot history travels with the guest. Native disk snapshots require snapshot-capable destination storage. Runtime modes must match.

`GET /nodes/<node>/docker/<id>/network` returns an `interfaces` object keyed by `net0` through `net15`, current `digest` and pending changes. `PUT` edits one interface using `net=net1`, or replaces the whole map using JSON in `interfaces`. Supply the latest digest. `delete=1` removes the selected interface; `net0` is required. `revert=1` discards pending network changes. Running guests apply changes at their next full start or reboot.

Each interface accepts `bridge`, `hwaddr`, `mtu`, `firewall`, `tag`, `link_down`, `rate`, `ipv4mode`, `cidr`, `gw`, `ipv6mode`, `cidr6`, `gw6` and `ports`. IPv4 supports `static`, `dhcp` and `none`. IPv6 supports `static`, `auto`, `dhcp` and `none`; use `auto` for SLAAC. An interface can have neither family assigned. VLAN tags require a VLAN-aware bridge; transparent trunks are not supported. Rate limits use MiB/s in both directions. Unassigned published ports use net0; `ports=web:8080/tcp` assigns that service's published port to the selected endpoint.

These are published endpoints on the host, not extra NICs inside each Docker service. Gateways provide source routing for endpoint traffic without changing the node's management routes. Docker's private service networks and their default route remain separate. Published IPv6 ports pass DCT firewall policy before bypassing the separate node INPUT policy. Rootless IPv6 uses a host TCP/UDP adapter; it does not enable IPv6 inside the private Docker networks. Disconnect blocks endpoint traffic while keeping its address reserved.

Firewall reloads validate the complete candidate before applying it. Host tables are changed in one nftables transaction. Rootless reloads coordinate the private namespace and host through a temporary forwarding guard; a failed reload restores the previous tables, and failed rollback keeps rootless forwarding closed. Start and recovery check the installed generation, not just cached files. Native DHCP, NDP and router-advertisement options are respected; unsupported options fail explicitly.

Rootless private networks enforce their assigned IPv4 and MAC identities even when optional guest filtering is disabled. Direct traffic between private bridges of different rootless DCTs is blocked independently of optional guest filtering. Node-local published connections admit only replies to the original published address, port and protocol. Members of one DCT remain in the same trust domain; its private networks are not service-level isolation. IPv6 is supported at published endpoints, including the rootless TCP/UDP adapter, not inside private Docker bridges. The adapter accepts only explicit unicast endpoint addresses and targets in the managed rootless pool. Rootless endpoint addresses allow only their published application ports and acquisition control traffic, so wildcard host listeners are not exposed through them. Rootless resources cannot use external networks, volume drivers or driver options to mount host sockets.

DHCP and IPv6 acquisition validate address families, lifetimes and conflicts before storing a lease or assigning an address. New controlled addresses are quarantined before assignment and opened only after their current policy is installed; IPv6 neighbor discovery remains available for duplicate-address detection. VLAN endpoints receive the same conflict checks. Router advertisements must arrive on the selected interface from a link-local sender with hop limit 255 and a valid autonomous /64 prefix. These checks do not authenticate the DHCP server or router: dynamic acquisition requires a trusted LAN. Acquisition does not install management routes or DNS. SDN endpoints use existing VNets; automatic SDN DHCP/IPAM and transparent VLAN trunks remain unsupported.

Bounded HTTP acceptance verified published IPv4/IPv6, same-DCT DNS, cross-DCT published ports, private-address isolation and Disconnect/reconnect on rootful PVE 9.1 and rootless PVE 9.2. Native ACCEPT/DROP traffic under both firewall backends, VLAN/SDN traffic and the remaining version/mode matrix are unverified. Live source-spoofing and host failure injection were excluded. See [release notes](RELEASE_NOTES.md) for the acceptance scope.


```bash
curl --fail --silent --show-error -X PUT \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN" \
  --data-urlencode "digest=$DIGEST" \
  --data-urlencode 'net=net1' --data-urlencode 'bridge=vmbr0' \
  --data-urlencode 'cidr=192.0.2.151/24' --data-urlencode 'ports=web:8080/tcp' \
  "$PVE_URL/api2/json$G/network"
```

Use the returned task ID to follow completion. To disconnect the interface, PUT `net=net1`, `link_down=1` and a fresh digest. To remove it, send `delete=1` instead.

## Service security and image updates

PUT `G/settings` with `service`, `digest` and a JSON-encoded `settings` object. Security fields are `user` (name or UID[:GID]), `read_only` (boolean), `cap_drop` (array), `no_new_privileges` (boolean) and `apparmor` (profile name, or an empty string for Docker defaults). Security edits require `VM.Config.Options`; running services keep them pending until recreated after stopping. Settings changes preserve each instance’s writable files, ownership, permissions, volume mounts and network bindings. The operation keeps the old containers until every replacement is ready; interrupted changes recover before the next operation. Other services are left untouched. Tmpfs mounts remain temporary. Image updates remain a separate operation. Writable volumes remain writable unless their own mount is read-only. Clearing `user` restores the image's default user.

AppArmor requires rootful Docker and an enabled host AppArmor module. For Standard DCTs, custom profiles must already be loaded in enforce mode on every destination node. Hardened rootful DCTs require `betterdocker-default` and cannot switch to a custom profile or `unconfined`. The extension installs `betterdocker-default` for rootful Hardened workloads. It does not install administrator-supplied custom profiles. Guest administrators can use `docker-default` and `no-new-privileges`; custom profiles and `unconfined` require node administrator access. `/management` reports available profiles and the applied profile per container.

To update images, POST `G/images/preview` with `images={"web":"nginx:stable"}` and the current configuration digest. Wait for the returned task, then GET `G/images` to compare deployed base images. `candidate_base` is the selected platform digest; `candidate` is the image ID pinned during the update. Pulling does not recreate services. Stop the DCT and apply or revert pending settings, then POST `G/images` with the preview's `token`, `digest` and a new snapshot name in `recovery`. Previews expire after one hour and reject changed configurations or containers.

The update creates that snapshot and recreates only changed services using immutable local image IDs. It leaves the DCT stopped. Start it and check its healthchecks. Rollback uses the normal snapshot endpoint and restores both containers and captured data, discarding data changes made since the snapshot. A failed or interrupted update requires this rollback before another update or Start. Recovery snapshots remain until explicitly removed. Existing snapshot ownership and storage restrictions apply.

## Additional disks and recovery

PUT `G/disks/mp1` with `service`, `target`, `storage`, `size_mib` and the current `digest`. Optional `read_only` and `backup` values default to false and true. The DCT must be stopped. Repeat the request to grow the disk, change its path or move it to another supported storage. Sizes cannot shrink. PUT `detach=1` to detach it while retaining its volume under unused disks. Additional disks require an existing project data disk.

GET `G/management` returns these disks in `storage.disks`, storage measurements in `storage.usage`, and recovery hints in `recovery`. Image sizes are logical sizes and can share physical blocks. POST `G/recover` to recover interrupted local configuration operations. It requires the full deployment configuration privileges. A `recovery.rollback` value identifies an image or snapshot operation that must use normal snapshot rollback instead.

POST `G/clone` accepts `full=0` for linked clones of templates whose managed disks are on LVM-thin. The clone stays on the source storage. Full clones remain the default and work with other supported storage types. Linked clones use native thin snapshots; their temporary snapshot is removed after the clone is allocated.

Network requests accept `ipv6mode=auto` for SLAAC and `ipv6mode=dhcp` for DHCPv6. Runtime addresses appear in `endpoint_ipv6`. IPv4 can be disabled with `ipv4mode=none`. Interfaces without addresses cannot publish ports. Endpoint acquisition does not replace host routes or DNS. VLAN tags select one VLAN per endpoint; a transparent L2 trunk is outside this published-port network model.

Clone and restore `deployment` overrides accept the same IPv6 fields and an `interfaces` object for multiple endpoints. New guests receive new MAC addresses unless you specify them. Migration keeps the existing addresses and MACs.

New delegated DCTs use Hardened policy: no new privileges, default seccomp and a bounded capability allowlist. Rootful workloads also require the enforcing `betterdocker-default` AppArmor profile. Rootless workloads do not support container AppArmor profiles. Only a node administrator can change the mandatory policy.

A node administrator can require bounded writes for Hardened rootless DCTs by creating `/etc/betterdocker/filesystem-policy.json`, owned by root with mode `0600`:

```json
{"require_read_only_rootless": true}
```

The default is `false`. When enabled, the container root filesystem must be read-only. Writable mounts must use a managed project or additional data disk with a positive `size_mib`, or tmpfs with an explicit positive size. Docker named and anonymous volumes are not disk-capped and cannot be writable under this policy. Image-declared volumes need explicit mounts, too. Configure these mounts before enabling the policy. Images must be prebuilt; Compose and Engine creation use the inspected image ID, so a tag change cannot introduce an unchecked volume. Compose does not perform a later automatic pull. Image updates remain available through the DCT update workflow.

Enabling the requirement does not recreate existing guests. Existing writable containers must be stopped and converted through the security-policy endpoint before their next Start. Pending edits cannot disable the requirement. Clone, restore and rollback reject incompatible mount layouts before changing guest state. Rootful and Standard DCTs retain their current behavior. Rootless writable-layer quotas remain unsupported. A managed data disk limits its own contents, not the container image, logs or tmpfs; tmpfs uses memory.

## Security scope

PVE ACLs authorize HTTP operations. Node `Sys.Modify` is an administrator privilege; it controls policy changes and exemptions, not a stronger form of container isolation. Runtime conversion and host Docker access remain administrator operations outside guest ACLs.

New delegated DCTs use Hardened, but administrator-created and legacy DCTs can use Standard. Hardened does not require a non-root service user or read-only root filesystem by default. Rootless shares one runtime account and user namespace per node and applies no container AppArmor profile in the tested setup. Both modes share the PVE kernel.

A managed data disk limits only data on that disk. Images, logs, snapshots, writable history, retained data and backup staging have separate storage costs. Per-container rotation and extension retention/admission budgets are not a node-wide storage quota. See [SECURITY.md](SECURITY.md) for the privilege model, limit exclusions and private vulnerability reporting.
