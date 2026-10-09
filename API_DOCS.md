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
| POST | `/nodes/<node>/docker` | Create a DCT; send `deployment` as a JSON-encoded string and optional `pool` and `start`. Omitted `start` keeps the legacy API default of starting immediately. |
| GET | `G/management` | Status, services, capabilities and configuration digest. |
| GET / PUT | `G/config` | Notes, tags, protection, startup and hook configuration. |
| GET / PUT | `G/network` | Endpoint settings, pending changes and firewall flag. PUT `revert=1` with `digest` to discard pending net0 changes. |
| GET / PUT | `G/pending` | Service effective/pending values. PUT `service`, comma-separated `revert` keys and `digest` to discard selected changes. |
| PUT | `G/settings` | Service settings; send `service`, JSON-encoded `settings` and `digest`. |
| POST | `G/action` | `action`: `start`, `shutdown`, `force-stop` or `reboot`. Optional `timeout` in seconds. |
| POST | `G/migrate` | Move to `target`; requires a stopped DCT or HA relocation. Optional `storage` and `digest`. |
| POST | `G/clone` | Clone with `newid`, JSON-encoded destination `deployment`, optional `pool`, `storage` and `digest`. |
| GET | `G/snapshots` | List snapshots. |
| GET / PUT | `G/snapshot/<name>/config` | Snapshot metadata. PUT `description` with the digest returned by GET. |
| POST | `G/snapshot` | `action`: `create`, `restore` or `delete`; also send `snapname`. |
| GET / POST | `G/firewall/rules` | Read or add native PVE firewall rules. |
| PUT / DELETE | `G/firewall/rules/<position>` | Edit or remove a firewall rule. |
| GET / PUT | `G/firewall/options` | Read or edit firewall options. |
| DELETE | `G` | Remove the DCT; persistent data is retained. Protection and HA restrictions apply. |

Network changes on a running DCT are queued. GET returns the effective fields and a `pending` object; `/management` includes `pending_network`. A full DCT start, reboot or restart applies them after all containers stop. Restarting one service does not apply them. Failed application keeps the pending changes and restores the previous network. Stopped guests apply edits immediately. Apply or revert pending network changes before migration. Clone/restore use the destination endpoint and discard the source's pending endpoint. Restored DCTs allow firewall changes; endpoint changes require redeployment.

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

## Local CLI

Run `dct` as root on an extension-enabled PVE node. It resolves the numeric guest ID and calls the PVE API, including when the owner is another node.

```bash
dct list
dct status 150
dct create 150 --node pve1 --file deployment.json
dct start 150
dct shutdown 150 --timeout 60
dct migrate 150 pve2
dct --no-wait snapshot 150 before-update
dct task status 'UPID:...'
```

CLI creation leaves the DCT stopped unless `--start` is supplied. Commands wait for asynchronous tasks by default and return a nonzero exit status on failure. `--no-wait` prints the UPID. Interrupting the CLI stops waiting; use `dct task stop 'UPID:...'` to request cancellation. `dct destroy 150 --confirm 150` removes the guest while retaining persistent data. `pct` does not manage DCTs.
