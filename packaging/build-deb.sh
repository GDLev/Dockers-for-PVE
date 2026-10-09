#!/usr/bin/env bash
# Build on Linux with dpkg-deb. Package triggers reapply the addon after PVE updates.
set -Eeuo pipefail
umask 022
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT_DIR=${1:-"$SOURCE_DIR/dist"}
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
chmod 0755 "$BUILD_DIR"
mkdir -p "$BUILD_DIR/DEBIAN" "$BUILD_DIR/opt/betterdocker" "$OUTPUT_DIR"
cp -a "$SOURCE_DIR/frontend" "$SOURCE_DIR/backend" "$SOURCE_DIR/scripts" "$SOURCE_DIR/systemd" "$SOURCE_DIR/start.sh" "$BUILD_DIR/opt/betterdocker/"
find "$BUILD_DIR/opt/betterdocker" -type d -name __pycache__ -prune -exec rm -rf {} +
cat > "$BUILD_DIR/DEBIAN/control" <<'CONTROL'
Package: proxmox-betterdockers
Version: 0.19.0
Architecture: all
Maintainer: BetterDockers contributors
Depends: pve-manager (>= 9.1), pve-manager (<< 9.3), python3, python3-yaml, e2fsprogs, util-linux, coreutils, curl, iputils-arping, isc-dhcp-client, pve-xtermjs, docker-ce | docker.io, docker-compose-plugin | docker-compose-v2
Description: Native Proxmox VE interface for host Docker Compose projects
CONTROL
cat > "$BUILD_DIR/DEBIAN/triggers" <<'TRIGGERS'
interest-noawait /usr/share/pve-manager
interest-noawait /usr/share/perl5/PVE/API2/Nodes.pm
interest-noawait /usr/share/perl5/PVE/API2/Cluster.pm
interest-noawait /usr/share/perl5/PVE/Cluster.pm
interest-noawait /usr/share/perl5/PVE/VZDump.pm
interest-noawait /usr/share/perl5/PVE/Replication.pm
interest-noawait /usr/share/perl5/PVE/API2/Replication.pm
interest-noawait /usr/share/perl5/PVE/HA/Env/PVE2.pm
interest-noawait /usr/share/perl5/PVE/API2/HA/Resources.pm
TRIGGERS
cat > "$BUILD_DIR/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -eu
case "$1" in
    configure|triggered) bash /opt/betterdocker/start.sh repair ;;
esac
POSTINST
cat > "$BUILD_DIR/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -eu
case "$1" in
    remove) bash /opt/betterdocker/start.sh uninstall ;;
esac
PRERM
chmod 0755 "$BUILD_DIR/DEBIAN/postinst" "$BUILD_DIR/DEBIAN/prerm"
dpkg-deb --root-owner-group --build "$BUILD_DIR" "$OUTPUT_DIR/proxmox-betterdockers_0.19.0_all.deb"
