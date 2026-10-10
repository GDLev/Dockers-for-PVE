#!/usr/bin/env bash
# Install the host engine. Preserve existing Docker installations and daemon settings.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
MODE=${1:-rootful}
if [[ "$MODE" == rootless ]]; then
    if ! command -v dockerd-rootless.sh >/dev/null || ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
        if [[ ${BETTERDOCKER_PACKAGE_CONFIGURE:-0} == 1 ]]; then
            echo 'Install compatible Docker rootless binaries before selecting rootless. Package configuration cannot run apt inside dpkg.' >&2
            exit 1
        fi
        . /etc/os-release
        [[ "$ID" == debian && "$VERSION_ID" =~ ^(12|13)$ ]] || { echo 'Debian 12/13 required'; exit 1; }
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
        if command -v docker >/dev/null && ! dpkg-query -W docker-ce >/dev/null 2>&1; then
            echo 'Existing Docker distribution lacks rootless support. Install compatible rootless binaries explicitly.' >&2
            exit 1
        fi
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-compose-plugin
    fi
    echo 'Using rootless Docker binaries; the installer configures the dedicated user service.'
    exit 0
fi
[[ "$MODE" == rootful ]] || { echo 'Choose rootful or rootless' >&2; exit 2; }
if command -v docker >/dev/null && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo 'Using the existing Docker Engine and Compose.'
    exit 0
fi
. /etc/os-release
[[ "$ID" == debian && "$VERSION_ID" =~ ^(12|13)$ ]] || { echo 'Debian 12/13 (Proxmox VE 8/9) required'; exit 1; }
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
