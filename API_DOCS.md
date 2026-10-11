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
| POST | `/nodes/<node>/docker` | Create a DCT; send `deployment` as a JSON-encoded string and optional `pool` and `start`. Omitted `start` keeps the legacy API default of starting immediately. |
| GET | `G/management` | Status, services, capabilities and configuration digest. |
| GET / PUT | `G/config` | Notes, tags, protection, startup and hook configuration. |
| GET / PUT | `G/network` | Endpoint settings, pending changes and firewall flag. PUT `revert=1` with `digest` to discard pending network changes. |
| GET / PUT | `G/pending` | Service effective/pending values. PUT `service`, comma-separated `revert` keys and `digest` to discard selected changes. |
| PUT | `G/settings` | Service settings; send `service`, JSON-encoded `settings` and `digest`. |
| POST | `G/action` | `action`: `start`, `shutdown`, `force-stop` or `reboot`. Optional `timeout` in seconds. |
| POST | `G/migrate` | Move to `target`; running DCTs require `restart=1`. Optional `storage`, `timeout` and `digest`. HA uses native relocation. |
| POST | `G/clone` | Clone with `newid`, JSON-encoded destination `deployment`, optional `pool`, `storage` and `digest`. |
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

`GET /nodes/{node}/docker/{id}/limits` returns `cpus`, `memory` in MiB, `pids` and a configuration `digest`. Reading requires `VM.Audit`.

`PUT` to the same path sets all three ceilings. Send `cpus`, `memory`, `pids` and the last `digest`. Zero removes that ceiling. Changing a budget requires the guest configuration privileges and `Sys.Modify` on the node. The DCT must be stopped, outside HA and not a template.

For example, with the same authentication headers used above:

```http
PUT /api2/json/nodes/pve1/docker/{id}/limits
Content-Type: application/x-www-form-urlencoded

cpus=2&memory=1024&pids=512&digest={digest}
```

Every service must have a finite limit for each active ceiling. The extension sums those limits across all declared replicas and rejects configurations exceeding the budget. It checks pending edits, deployment changes, saved containers and snapshot rollback. Docker enforces the individual container caps; unused capacity is not shared dynamically between services. CPU and memory reservations are not hard limits.

Budgets are preserved in backups, clones and migration. Snapshot rollback retains the current administrator budget. Changes are synchronous and return the saved ceilings, without a task dialog.

From the node CLI:

```sh
dct limits 100
dct limits 100 --cpus 2 --memory 1024 --pids 512
```

## Removing data

`DELETE G?destroy-owned-data=1` removes project files, managed disks and local Docker volumes owned exclusively by that DCT. External bind mounts and external volumes remain. Shared volumes, protection, snapshots, HA, replication and unfinished transfers prevent removal. Keep the default if you need recovery data.

The Docker Volumes storage tab can remove individual unused or retained PVE disks. Attached disks cannot be deleted there. For an existing DCT, removal requires `VM.Config.Disk` and `Datastore.AllocateSpace`. Retained disks require node `Sys.Modify` and storage `Datastore.AllocateSpace`, because the deleted guest no longer has ACLs.

The CLI accepts `dct destroy 150 --destroy-owned-data 1`. Deletion is permanent; take a backup first.

Charts use PVE's native guest RRD archives and consolidation. Long ranges fill as new measurements arrive; the previous JSON history is not imported.

## Migration and network interfaces

`POST /nodes/<node>/docker/<id>/migrate` accepts `target`, optional `storage`, `restart=1` and `timeout` in seconds. Running guests require restart mode: shutdown, transfer, then start the instances that were previously active. Stopped guests remain stopped. Snapshot history travels with the guest. Native disk snapshots require snapshot-capable destination storage. Runtime modes must match.

`GET /nodes/<node>/docker/<id>/network` returns an `interfaces` object keyed by `net0` through `net15`, current `digest` and pending changes. `PUT` edits one interface using `net=net1`, or replaces the whole map using JSON in `interfaces`. Supply the latest digest. `delete=1` removes the selected interface; `net0` is required. `revert=1` discards pending network changes. Running guests apply changes at their next full start or reboot.

Each interface accepts `bridge`, `hwaddr`, `mtu`, `firewall`, `tag`, `link_down`, `rate`, `ipv4mode`, `cidr`, `gw`, `ipv6mode`, `cidr6`, `gw6` and `ports`. IPv4 supports static addresses and DHCP; additional interfaces can disable IPv4. IPv6 supports static addresses. VLAN tags require a VLAN-aware bridge. Rate limits use MiB/s in both directions. Unassigned published ports use net0; `ports=web:8080/tcp` assigns that service's published port to the selected endpoint.

These are published endpoints on the host, not extra NICs inside each Docker service. Gateways provide source routing for endpoint traffic without changing the node's management routes. Docker's private service networks and their default route remain separate. Published IPv6 ports pass DCT firewall policy before bypassing the separate node INPUT policy. Rootless IPv6 uses a host TCP/UDP adapter; it does not enable IPv6 inside the private Docker networks. Disconnect blocks endpoint traffic while keeping its address reserved.

```bash
curl --fail --silent --show-error -X PUT \
  -H "Authorization: PVEAPIToken=$PVE_TOKEN" \
  --data-urlencode "digest=$DIGEST" \
  --data-urlencode 'net=net1' --data-urlencode 'bridge=vmbr0' \
  --data-urlencode 'cidr=192.0.2.151/24' --data-urlencode 'ports=web:8080/tcp' \
  "$PVE_URL/api2/json$G/network"
```

Use the returned task ID to follow completion. To disconnect the interface, PUT `net=net1`, `link_down=1` and a fresh digest. To remove it, send `delete=1` instead.
