#!/usr/bin/env bash
# Remote entry point: curl -fsSL .../-/raw/HEAD/install.sh | bash
# Define the entire entry point before executing it when read from a pipe.
set -Eeuo pipefail

main() {
    local project_dir
    if [[ ${1:-} == --help || ${1:-} == -h ]]; then
        echo 'Usage: bash install.sh [install|repair|uninstall|finish|check] [--nodes=node1,node2] [--runtime=rootful|rootless] [--yes] [--no-restart] [--interactive]'
        return 0
    fi
    [[ $EUID -eq 0 ]] || { echo 'Run as root on a Proxmox VE host.' >&2; return 1; }
    for command in pveversion python3 curl tar mktemp; do
        command -v "$command" >/dev/null || { echo "Missing: $command" >&2; return 1; }
    done

    project_dir=$(mktemp -d /tmp/betterdocker-install.XXXXXXXX)
    # Expand the path now: the local variable is gone when the EXIT trap runs.
    trap "rm -rf -- '$project_dir'" EXIT
    echo 'Downloading Proxmox BetterDockers.'
    curl --fail --silent --show-error --location --retry 3 \
        "${BETTERDOCKER_SOURCE_URL:-https://github.com/GDLev/Proxmox-BetterDockers/-/archive/HEAD/Proxmox-BetterDockers-HEAD.tar.gz}" \
        --output "$project_dir/project.tar.gz" </dev/null
    tar -xzf "$project_dir/project.tar.gz" --strip-components=1 -C "$project_dir"
    [[ -f "$project_dir/start.sh" && -f "$project_dir/scripts/installer.py" ]] || {
        echo 'Downloaded archive does not contain the BetterDocker installer.' >&2
        return 1
    }
    # Package managers must not consume the incoming installation script.
    python3 "$project_dir/scripts/installer.py" "$@" </dev/null
}

main "$@"
