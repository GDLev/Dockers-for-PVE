# HTTP API

Dockers for Proxmox VE extends the PVE API on port 8006. Use HTTPS with the node's trusted certificate and an ordinary PVE API token. Tokens need the same guest, pool and storage permissions as the web interface. With privilege separation enabled, both the user and token must have those permissions.

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
| PUT | `/nodes/<node>/docker` | Preview the JSON-encoded `deployment` without creating a guest. Set `resources_only=1` to read service limits before choosing a network; network access is still checked on creation. |
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

Creation also accepts optional `resources` in the deployment object, keyed by Compose service name, with `cpus`, `cpuunits`, `memory`, `swap`, and `disk` overrides. Memory, swap, and writable-layer disk limits use MiB. Omitted values keep the Compose settings. Limits apply per replica. Writable-layer quotas require a supported runtime. Use the settings routes for later changes.

To allocate a managed data disk before the first start, add `data_disk: {"storage":"local-lvm","size_mib":8192}` to the deployment object. Compose must define private writable named volumes or project data directories. For image deployments, include `"target":"/data"` to choose the application mount point. Allocation requires `Datastore.AllocateSpace`. A failed disk setup leaves the guest stopped for recovery.

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

## Configuration and networking

Use the latest `digest` from GET when changing configuration. PUT `G/network` edits one endpoint with `net=net1`, or replaces the map with JSON in `interfaces`. `net0` is required; additional endpoints can be removed with `delete=1`. Running guests retain changes as pending until a full start or reboot. Stopped guests apply them immediately. PUT `revert=1` discards pending network changes.

Endpoints support IPv4 static/DHCP, IPv6 static/SLAAC/DHCPv6, VLAN tags, firewall, disconnect and rate limits. They publish application ports on the host; they are not additional NICs inside Docker services. Rootless IPv6 uses a host TCP/UDP adapter. VLAN trunks and automatic SDN DHCP/IPAM are unsupported. Dynamic address acquisition requires a trusted LAN.

```bash
curl --fail --silent --show-error -X PUT \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN" \
  --data-urlencode "digest=$DIGEST" \
  --data-urlencode net=net1 --data-urlencode bridge=vmbr0 \
  --data-urlencode cidr=192.0.2.151/24 --data-urlencode ports=web:8080/tcp \
  "$PVE_URL/api2/json$G/network"
```

PUT `G/settings` accepts `service`, `digest` and a JSON-encoded `settings` object. Resource options apply per service instance. Security fields include `user`, `read_only`, `cap_drop`, `no_new_privileges` and `apparmor`. Running services keep changes pending until stopped and recreated. Reconfiguration preserves writable files and volumes; tmpfs remains temporary. AppArmor requires rootful Docker and an enforcing host profile. Hardened rootful guests require `betterdocker-default`.

## Images, disks and recovery

Image updates use two steps. POST `G/images/preview` with `images={"web":"nginx:stable"}` and `digest`, wait for the task, then GET `G/images`. Stop the guest and resolve pending settings before POST `G/images` with the returned `token`, `digest` and a new snapshot name in `recovery`. The update leaves the guest stopped. Previews expire after one hour.

Start the guest and check application health after updating. Failed or interrupted updates require snapshot rollback. Rollback restores captured data too, discarding subsequent changes. Recovery snapshots remain until explicitly removed.

PUT `G/disks/mp1` accepts `service`, `target`, `storage`, `size_mib` and `digest`, with optional `read_only` and `backup`. The guest must be stopped and already have a project data disk. Disks can grow but cannot shrink. PUT `detach=1` retains the volume under unused disks.

GET `G/management` reports storage, capabilities and recovery hints. POST `G/recover` recovers interrupted local configuration operations and requires deployment configuration privileges. If `recovery.rollback` is present, use snapshot rollback instead.

## Migration and deletion

Running guests require `restart=1` for migration. Migration stops, transfers and restarts the services that were active; there is no live migration. Runtime modes must match, and destination storage must support the guest's disks and snapshot history. Resolve pending network changes first. HA uses native relocation.

Clone uses `newid` and a JSON-encoded destination `deployment`. Full clones are the default. `full=0` is available for LVM-thin templates and keeps the clone on source storage. Clone and restore use the destination endpoints; migration keeps the existing addresses and MACs.

DELETE `G` retains persistent data by default. `destroy-owned-data=1` permanently deletes exclusively owned data and managed disks. External or shared mounts remain. Protection, snapshots, HA, replication and unfinished transfers can block deletion.

DELETE `G/volumes/<vmid>?volume=<volid>` removes an unused disk. Attached disks cannot be deleted. Existing guests require `VM.Config.Disk` and `Datastore.AllocateSpace`; retained disks require node `Sys.Modify` and storage `Datastore.AllocateSpace`.

## Administrator policies

GET/PUT `G/limits` reads or changes aggregate ceilings. Fields are `cpus`, `memory` in MiB, `pids`, optional `services`, `replicas`, `exempt`, and `digest`. GET requires `VM.Audit`; changes require guest configuration privileges and node `Sys.Modify`. The guest must be stopped, outside HA and not a template. Zero removes an administrator ceiling. Delegated guests require positive ceilings unless explicitly exempted.

Docker enforces individual service caps. The extension sums caps across replicas and rejects configurations exceeding the aggregate budget. Capacity is not shared dynamically, and these budgets do not reserve resources across the node. New delegated guests default to Hardened policy, with 1 CPU, 512 MiB and 128 PIDs per container, and aggregate ceilings of 4 CPUs, 4096 MiB, 1024 PIDs, 8 services and 16 replicas.

Node policies are root-owned JSON files under `/etc/betterdocker/`, with mode `0600`:

| File | Default configuration |
| --- | --- |
| `resource-policy.json` | `{"container":{"cpus":1,"memory":512,"pids":128},"project":{"cpus":4,"memory":4096,"pids":1024,"services":8,"replicas":16}}` |
| `operation-policy.json` | `{"max_concurrent":2}`, integer 1 through 64 |
| `retention-policy.json` | `{"snapshots":16,"snapshot_bytes":34359738368,"preserved_image_bytes":8589934592,"log_file_bytes":10485760,"log_files":3,"transfer_bytes":34359738368,"node_transfer_bytes":68719476736}` |
| `space-policy.json` | `{"reserve_bytes":1073741824,"overhead_percent":10}` |
| `filesystem-policy.json` | `{"require_read_only_rootless":false}` |

Resource defaults affect future delegated guests. Retention checks reject operations exceeding budgets; they do not prune snapshots or delete data. Log rotation applies when containers are created or recreated. Space reservations coordinate extension operations, but other host processes can still consume capacity. These checks are not filesystem quotas for arbitrary application writes.

Enabling `require_read_only_rootless` requires read-only roots and bounded writable managed disks or explicitly sized tmpfs for Hardened rootless guests. Writable named/anonymous volumes are incompatible. Configure mounts before enabling this policy; existing guests are not automatically recreated. Rootless writable-layer quotas remain unsupported.

For private vulnerability reporting and the isolation model, see [SECURITY.md](SECURITY.md). For CLI help, use `dct help` or `dct help <command>`. Installed extension and guest data locations are listed in [CONTRIBUTING.md](CONTRIBUTING.md#installed-files).
