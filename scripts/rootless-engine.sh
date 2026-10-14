#!/usr/bin/env bash
# Runs only inside RootlessKit's mapped user and private mount namespace.
set -Eeuo pipefail
read -r inside outside count < /proc/self/uid_map
[[ $EUID -eq 0 && "$inside" == 0 && "$outside" -gt 0 && "$count" == 1 ]] || {
    echo 'Rootless Engine requires a mapped, non-host-root user namespace.' >&2
    exit 1
}
# Docker cannot load host AppArmor profiles as this account. Hide that host-only
# interface from the daemon; keep host AppArmor and container seccomp intact.
if [[ -d /sys/kernel/security/apparmor ]]; then
    mount -t tmpfs -o ro,nodev,nosuid,noexec tmpfs /sys/kernel/security
fi
exec /usr/bin/dockerd "$@"
