#!/usr/bin/env bash
# Executed INSIDE the new Debian guest, never on the Proxmox host.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
[[ "$ID" == debian && "$VERSION_ID" =~ ^(12|13)$ ]] || { echo 'Debian 12/13 required'; exit 1; }
for attempt in {1..12}; do
    if apt-get update; then break; fi
    [[ "$attempt" != 12 ]] || exit 1
    sleep 5
done
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
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
for attempt in {1..15}; do
    if docker info >/dev/null 2>&1; then break; fi
    [[ "$attempt" != 15 ]] || exit 1
    sleep 2
done
docker compose version
