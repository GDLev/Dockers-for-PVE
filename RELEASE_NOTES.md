# Release notes

## 0.20.0 development

This is an unreleased development build. No production signing key has been established. Signed clean-install acceptance, the full security matrix, external CI on the final release commit, and independent review remain open.

Recent security work adds mandatory Hardened policy for delegated deployments, aggregate resource budgets, bounded logs, storage admission and retention controls, authenticated recovery state, and signed release verification.

Acceptance found and fixed rootful Shutdown with Debian's confined `runc`, the missing IPv6 `ndisc6` dependency, rootless node-local published connections, and direct private traffic between rootless DCTs. The fixes have regression checks and bounded live HTTP verification on the test cluster.

Rootful traffic was checked on PVE 9.1, and rootless traffic on PVE 9.2. The latest required suite passed 266 checks per node without skips. The local unit suite passed 683 tests with two macOS platform skips. These results refer to the recorded working tree, not a published release commit. Native guest ACCEPT/DROP traffic under both firewall backends, VLAN/SDN traffic, the remaining version/mode/storage combinations, and real host-outage scenarios are not fully verified.

Docker stays on the PVE host. Rootful retains host-root daemon authority. Rootless shares one runtime account per node and has no container AppArmor enforcement in the tested setup. Neither mode provides VM isolation. Managed data-disk capacity does not cover all Docker or snapshot storage.

Use only for small, non-critical installations. See [security guidance](SECURITY.md), [installation and removal](README.md#installation), and [HTTP API usage](API_DOCS.md). Release publication remains blocked until the outstanding acceptance requirements are met.
