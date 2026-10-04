#!/usr/bin/env bash
# Remote entry point: curl -fsSL .../-/raw/HEAD/install.sh | bash
# Define the entire entry point before executing it when read from a pipe.
set -Eeuo pipefail

main() {
    local action="${1:-install}"
    local project_dir
    [[ $EUID -eq 0 ]] || { echo 'Uruchom jako root na hoście Proxmox VE.' >&2; return 1; }
    case "$action" in
        install|repair) ;;
        *) echo 'Użycie: bash install.sh [install|repair]' >&2; return 2 ;;
    esac
    for command in pveversion curl tar mktemp; do
        command -v "$command" >/dev/null || { echo "Brak: $command" >&2; return 1; }
    done
    local version
    version=$(pveversion)
    [[ "$version" =~ pve-manager/(8|9)\. ]] || { echo "Nieobsługiwana wersja: $version" >&2; return 1; }

    project_dir=$(mktemp -d /tmp/betterdocker-install.XXXXXXXX)
    # Expand the path now: the local variable is gone when the EXIT trap runs.
    trap "rm -rf -- '$project_dir'" EXIT
    echo 'Pobieram Proxmox BetterDockers.'
    curl --fail --silent --show-error --location --retry 3 \
        'https://github.com/GDLev/Proxmox-BetterDockers/-/archive/HEAD/Proxmox-BetterDockers-HEAD.tar.gz' \
        --output "$project_dir/project.tar.gz" </dev/null
    tar -xzf "$project_dir/project.tar.gz" --strip-components=1 -C "$project_dir"
    [[ -f "$project_dir/start.sh" && -f "$project_dir/scripts/patch.py" ]] || {
        echo 'Pobrane archiwum nie zawiera instalatora BetterDocker.' >&2
        return 1
    }
    # Package managers must not consume the incoming installation script.
    bash "$project_dir/start.sh" "$action" </dev/null
}

main "$@"
