#!/usr/bin/env bash
# Install the host engine. Preserve existing Docker installations and daemon settings.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
MODE=${1:-rootful}
require_compose() {
    local installed
    installed=$(docker compose version --short)
    installed=${installed#v}
    if [[ ! $installed =~ ^[0-9]+\.[0-9]+\.[0-9]+([+][[:alnum:].-]+)?$ ]] || [[ $(printf '%s\n' 5.6.0 "$installed" | sort -V | head -n1) != 5.6.0 ]]; then
        echo 'Docker Compose 5.6.0 or newer is required. Upgrade the Compose plugin before installing BetterDockers; older parsers can retain stale .env values during deployment edits.' >&2
        exit 1
    fi
}

setup_repository() {
    if [[ ${BETTERDOCKER_PACKAGE_CONFIGURE:-0} == 1 ]]; then
        echo 'Package configuration cannot install Docker dependencies inside dpkg. Repair Docker and retry.' >&2
        exit 1
    fi
    local arch
    . /etc/os-release
    [[ "$ID" == debian && "$VERSION_ID" == 13 ]] || { echo 'Debian 13 required'; exit 1; }
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl --retry 5 -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    arch=$(dpkg --print-architecture)
    cat > /etc/apt/sources.list.d/docker.sources <<SOURCES
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: ${arch}
Signed-By: /etc/apt/keyrings/docker.asc
SOURCES
    apt-get update
}

install_rootless_packages() (
    masked=()
    installing=0
    cleanup() {
        local status=$?
        trap - EXIT
        set +e
        if (( installing )) && ! systemctl disable docker.service docker.socket; then
            echo 'Cannot disable the newly installed rootful Docker units; keeping them masked.' >&2
            exit 1
        fi
        if (( ${#masked[@]} )); then
            systemctl unmask "${masked[@]}" || status=1
        fi
        exit "$status"
    }
    if ! command -v docker >/dev/null; then
        if command -v dockerd >/dev/null || systemctl is-active --quiet docker.service docker.socket; then
            echo 'Existing Docker daemon has no client. Repair its binaries before installing rootless support.' >&2
            exit 1
        fi
        trap cleanup EXIT
        # Persistent masks also prevent a host-root daemon starting after a
        # power loss during apt. Remove only masks created by this invocation.
        for unit in docker.service docker.socket; do
            if [[ $(systemctl is-enabled "$unit" 2>/dev/null || true) != masked* ]]; then
                systemctl mask "$unit" || exit $?
                masked+=("$unit")
            fi
        done
        installing=1
    fi
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-compose-plugin || exit $?
)

if [[ "$MODE" == rootless ]]; then
    if ! command -v dockerd-rootless.sh >/dev/null || ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
        setup_repository
        if command -v docker >/dev/null && ! dpkg-query -W docker-ce >/dev/null 2>&1; then
            echo 'Existing Docker distribution lacks rootless support. Install compatible rootless binaries explicitly.' >&2
            exit 1
        fi
        install_rootless_packages
    fi
    require_compose
    python3 "$(dirname -- "${BASH_SOURCE[0]}")/dctlib/requirements.py" binaries
    echo 'Using rootless Docker binaries; the installer configures the dedicated user service.'
    exit 0
fi
[[ "$MODE" == rootful ]] || { echo 'Choose rootful or rootless' >&2; exit 2; }
if command -v docker >/dev/null && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    require_compose
    python3 "$(dirname -- "${BASH_SOURCE[0]}")/dctlib/requirements.py" binaries
    echo 'Using the existing Docker Engine and Compose.'
    exit 0
fi
setup_repository
if command -v docker >/dev/null; then
    # Do not replace an existing engine with a different package distribution.
    apt-get install -y docker-compose-plugin
else
    install -d -m 0755 /etc/docker
    python3 - <<'PY'
import json
from pathlib import Path
path = Path('/etc/docker/daemon.json')
config = json.loads(path.read_text()) if path.exists() else {}
# Do not let Docker change the default forwarding policy on the Proxmox router/bridge.
config.setdefault('ip-forward-no-drop', True)
path.write_text(json.dumps(config, indent=2) + '\n')
PY
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
docker info >/dev/null
docker compose version
require_compose
python3 "$(dirname -- "${BASH_SOURCE[0]}")/dctlib/requirements.py" binaries
