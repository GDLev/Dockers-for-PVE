# Rootless Docker implementation plan

Status: approved and completed on 2026-10-10. Acceptance results are recorded in [todo.md](todo.md); supported restrictions are listed in [README.md](README.md).

## Outcome and scope

Choose rootful or rootless Docker when installing the extension on a node. Keep that choice through updates, repair and PVE package triggers. Do not add a mode switch to the GUI. Existing installations keep their current rootful runtime.

In rootless mode, the Docker daemon, containers and Docker commands run under one dedicated account per node. PVE still performs host administration as root. The runtime account receives no sudo access, root Docker socket, PVE credentials or membership in the root Docker group. This does not provide a separate runtime account for each DCT or VM-level isolation.

Keep the existing PVE task, permission and UX behavior. Check supported features against both runtime modes. Rootless must not silently ignore a limit, bypass the firewall or fall back to rootful Docker.

Acceptance checks passed on the test cluster. Rootless is the default for fresh installations. Both modes remain explicit installer choices, and existing installations preserve their runtime.

## Initial dependencies

Before this work, the implementation used rootful Docker service commands, `/var/run/docker.sock`, host Docker bridges and host numeric file owners. Changing the socket alone would have left networking, firewall, storage and restore behavior incorrect.

Docker rootless needs subordinate UID/GID ranges and `newuidmap`/`newgidmap`. Its supported service setup uses a systemd user service with lingering. Resource limits need cgroup v2 and the required systemd delegation. See the official [rootless documentation](https://docs.docker.com/engine/security/rootless/), [service and socket guidance](https://docs.docker.com/engine/security/rootless/tips/) and [resource-limit requirements](https://docs.docker.com/engine/security/rootless/troubleshoot/).

## R1 - Runtime configuration and privilege boundaries

- Add a root-owned, node-local runtime configuration. Proposed location: `/etc/betterdocker/runtime.json`. Record the mode, account, socket, data directory and UID/GID mappings. Do not infer these from a user's Docker context or environment.
- Centralize Docker CLI, Compose, Engine socket and service access. Proposed module: `scripts/runtime.py`. Cover provisioning, console, statistics, snapshots, backup, transfer, replication and HA callers.
- Run rootless workload operations after dropping privileges. Keep native PVE authorization, task creation, `/etc/pve` writes, storage credentials and host administration in the privileged coordinator.
- Limit privileged host operations to fixed, validated actions. Derive guest ownership and allowed volumes from root-owned state. Do not expose arbitrary commands, paths or a general sudo wrapper to the runtime account.
- Separate runtime-writable data from authoritative storage, transfer and ownership records. Protect against forged metadata, symlinks and path replacement. Preserve guest-operation locking across privileged and unprivileged workers.
- Keep administrator-selected native hook scripts under the existing PVE privilege model. Runtime-writable Compose files must not select or modify privileged hooks.

Acceptance: every Docker caller uses the selected runtime; a rootless worker cannot reach the root Docker socket, change privileged configuration or turn writable project metadata into a host operation.

## R2 - Installation, updates and removal

- Add an installer choice and an explicit noninteractive option. Proposed syntax: `--runtime=rootful|rootless`. Forward it through `install.sh` and `start.sh`. Read interactive input from the terminal so the curl-pipe installer cannot consume its own script.
- Provide an explicit first-install choice for DEB installations through package configuration. Package updates and repair triggers read the saved mode without prompting. Never initialize a second runtime during a trigger.
- Check dependencies, cgroup delegation, local runtime storage, account ownership and subordinate-ID collisions before changing services. Reuse a recorded account and mapping on retries; do not allocate a new range on each install.
- Create the dedicated account, systemd user service and lingering for rootless mode. Keep Docker runtime storage on a supported local filesystem.
- Reject an attempt to change an installed node's mode through install or repair. Direct the administrator to the migration procedure.
- Roll back changes made by a failed installation. Do not disable or convert an unrelated Docker daemon.
- Preserve the runtime account, mappings, service, workload data and required host maintenance on uninstall, matching the current policy of retaining Docker workloads. Document a separate explicit data-removal procedure.

Acceptance: fresh installation, repeated installation, update, repair, DEB triggers and uninstall preserve the selected mode and existing workloads. An unsupported setup fails before partial initialization.

## R3 - Guest operations and limits

- Route create, redeploy, start, shutdown, stop, reboot, remove, console and diagnostics through the runtime layer. Keep native task types and existing permission checks.
- Use scoped registry credentials without exposing root's Docker login state. Keep PVE secrets outside the runtime account's files and environment.
- Apply CPU, memory and PID limits through verified cgroup delegation. Reject unsupported requested limits instead of reporting success.
- Check boot ordering, daemon restart, node shutdown, onboot behavior and native hook phases with the user service.
- Report actual runtime mode and health in the existing diagnostics view. Keep missing measurements unavailable rather than turning them into zero.

Acceptance: process identities, container configuration and measured limits confirm rootless operation. Service restart and node reboot recover the expected guest state without starting rootful Docker as a fallback.

## R4 - Networking and firewall

- Adapt endpoint creation, port forwarding and firewall enforcement to the rootless network namespace. Retain the current DCT address and bridge behavior where supported.
- Support ports such as 80 and 443 through scoped privileged forwarding or an equally narrow mechanism. Do not silently lower the host-wide unprivileged-port threshold or grant broad capabilities.
- Enforce incoming, outgoing and cross-DCT rules. Check source-address handling, DNS, DHCP and supported SDN paths. Do not reuse host bridge assumptions when the bridge exists inside another namespace.
- Preserve pending network changes, rollback on failure and cleanup of owned endpoints. A guest with requested firewall protection must not start with incomplete filtering.
- Record network-driver restrictions discovered during implementation. Firewall and published-port behavior are release requirements, not optional follow-up work.

Acceptance: allowed traffic succeeds, blocked traffic fails, low ports work and failed network changes restore the previous endpoint. Verify these outcomes through the actual network, including source-address rules and cross-DCT access.

## R5 - Storage, snapshots and portable data

- Make managed data disks and project-owned mounts accessible through the recorded UID/GID mapping. Keep allocation and mounting under PVE control.
- Version the portable data format around container UID/GID ownership. Remap data on restore rather than copying host numeric owners between different mappings. Retain support for existing rootful archives.
- Adapt snapshots, rollback, clone, templates, backup and restore, including local storage and PBS. Preserve configuration, pending values and guest metadata.
- Advertise quotas only when the selected runtime and backing storage enforce them. Treat managed data-disk capacity separately from writable-layer quotas. Disable unsupported quota controls with a concise reason.
- Reject mounts or archive contents that escape the guest's allowed data. Never recursively change ownership of an unrelated host directory.

Acceptance: data hashes and container-visible ownership survive snapshot rollback, clone and local/PBS restore. Quotas shown as supported reach their real limit. Legacy backups still restore without changing their ownership semantics.

## R6 - Cluster operations

- Publish each node's runtime capabilities for transfer, replication and HA preflight checks. Validate mode, target storage and mappings before stopping or moving the source guest.
- Support ordinary migration, replication and HA between compatible rootless nodes under the existing DCT restrictions. Preserve VMIDs, pools, ACLs, task history and native placement behavior.
- Initially reject automatic transfer, replication or HA recovery across rootful/rootless modes. A mixed-mode cluster may contain independent guests; mode conversion remains an explicit migration operation.
- Fail clearly when the target lacks a compatible runtime. Do not start a guest against unreadable data, another daemon or an incompatible mapping.

Acceptance: transfer and replication preserve guest data and ownership across two rootless nodes. HA recovery uses the correct runtime. An incompatible target is rejected before the source workload changes.

## R7 - Explicit runtime migration

- Add a standalone, root-only migration script. Proposed path: `scripts/migrate-runtime.py`. No installer update or GUI action runs it automatically.
- Convert the node's DCT runtime during a maintenance window. Provide a dry run with the affected guests, compatibility blockers, backup destination and rollback requirements.
- Start with stopped, portable DCTs without HA registration, replication jobs or snapshot history. Reject these blockers before conversion; do not silently discard them. Shared external volumes and unrelated Docker workloads remain outside automatic conversion.
- Create and verify backups before cutover. Preserve VMIDs, PVE metadata, pools and ACLs. Convert file ownership using the recorded mappings rather than a blanket chown.
- Keep original runtime data until acceptance. Record each completed step in a root-owned journal so an interrupted run can resume or return to the original runtime. Store backups and journals with restricted access.
- Document both conversion directions and the supported subset. Restoring service after failure must not depend on a GUI mode switch.

Acceptance: a supported conversion preserves guest identity and data. An interrupted conversion can resume or roll back. A blocked conversion leaves the source runtime and data unchanged.

## R8 - Documentation, deployment and final verification

- Update README and API_DOCS after implementation with installation choices, file locations, read-only runtime information, retained files on uninstall and verified limitations. Do not add a mode-changing API.
- Add regression tests and a repeatable live acceptance runner. Use the current test framework and cover both modes, legacy configuration and archives, privilege boundaries, failure recovery and mode preservation.
- Implement the approved scope before evaluating final results. Perform prerequisite checks when needed to carry out an operation; run the acceptance suite after deployment.
- Deploy through SSH/SCP. Use the browser only at the end for installer-independent UX: diagnostics, available limits, ordinary saves, console and native task/error behavior.
- Fix failures, redeploy and repeat the affected checks until the acceptance criteria pass. Record versions, outcomes and remaining restrictions in `todo.md`. Keep unchecked work open.

## Test environment and deployment order

Use pve1, pve2 and pve3. Keep at least one node rootful for regression checks. Prepare a rootless pilot on a suitable node, then a second compatible rootless node for transfer, replication and HA checks. Do not assume pve3 is empty.

Before changing a node's runtime, inventory its Docker workloads and guest placement. Use disposable DCTs and an empty runtime where possible. If preparing two rootless nodes requires moving or stopping existing workloads, present the affected guests and the required maintenance window before that operation. Do not convert them as an incidental installer test.

Use the test PBS for backup and restore only. Install no extension code on PBS. Preserve existing HA, replication and storage configuration; use temporary test entries for acceptance and remove them afterward.

Final checks must include:

| Area | Required evidence |
| --- | --- |
| Installation | Both modes; shell and DEB install/update/repair/remove; mode-change rejection; interrupted-install recovery |
| Privileges | Daemon and worker UID; socket access; rejected forged metadata, unsafe mounts and path replacement; scoped API grants |
| Lifecycle | Running/stopped guests, pending edits, console, onboot, daemon restart and node reboot |
| Resources | Actual CPU, memory and PID enforcement; accurate unavailable statistics and quota capabilities |
| Network | Low ports, DNS, endpoint changes, permitted/blocked incoming and outgoing traffic, cross-DCT rules and source IP behavior |
| Data | Hashes and logical owners after snapshots, rollback, clone, legacy restore and local/PBS backup/restore |
| Cluster | Two-node rootless migration and replication; actual HA recovery; incompatible-target rejection before source changes |
| Conversion | Dry run, blockers, successful conversion, interruption/retry and rollback |
| PVE UX | Read-only mode display, native task feedback, working console and no new GUI mode switch |

Record the actual PVE, kernel, Docker and network-driver versions. Rootless remains opt-in if mandatory behavior has an unresolved failure. Do not mark the project fully integrated from a successful container start alone.
