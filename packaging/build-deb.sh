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
cp -a "$SOURCE_DIR/frontend" "$SOURCE_DIR/backend" "$SOURCE_DIR/scripts" "$SOURCE_DIR/systemd" "$SOURCE_DIR/start.sh" "$SOURCE_DIR/install.sh" "$BUILD_DIR/opt/betterdocker/"
find "$BUILD_DIR/opt/betterdocker" -type d -name __pycache__ -prune -exec rm -rf {} +
cat > "$BUILD_DIR/DEBIAN/control" <<'CONTROL'
Package: proxmox-betterdockers
Version: 0.19.0
Architecture: all
Maintainer: BetterDockers contributors
Depends: pve-manager (>= 9.1), pve-manager (<< 9.3), python3, python3-yaml, e2fsprogs, util-linux, coreutils, nftables, iptables, curl, iputils-arping, isc-dhcp-client, pve-xtermjs, docker-ce | docker.io, docker-compose-plugin | docker-compose-v2, uidmap, dbus-user-session, docker-ce-rootless-extras | rootlesskit, debconf
Recommends: docker-ce-rootless-extras
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
    configure)
        export BETTERDOCKER_PACKAGE_CONFIGURE=1
        if [ -e /etc/betterdocker/runtime.json ] || python3 /opt/betterdocker/scripts/runtime_install.py select >/dev/null 2>&1; then
            bash /opt/betterdocker/start.sh repair
        else
            . /usr/share/debconf/confmodule
            db_input medium proxmox-betterdockers/runtime || true
            db_go
            db_get proxmox-betterdockers/runtime
            bash /opt/betterdocker/start.sh repair --runtime="$RET"
        fi ;;
    triggered) BETTERDOCKER_PACKAGE_CONFIGURE=1 bash /opt/betterdocker/start.sh repair ;;
esac
POSTINST
cat > "$BUILD_DIR/DEBIAN/templates" <<'TEMPLATES'
Template: proxmox-betterdockers/runtime
Type: select
Choices: rootful, rootless
Default: rootless
Description: Docker runtime for this node:
 Choose once at installation. Updates and repair preserve this mode.
 Rootless needs compatible Docker rootless binaries. Later conversion is an
 explicit offline maintenance operation, not a GUI setting.
TEMPLATES
cat > "$BUILD_DIR/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -eu
case "$1" in
    remove) bash /opt/betterdocker/start.sh uninstall ;;
esac
PRERM
chmod 0755 "$BUILD_DIR/DEBIAN/postinst" "$BUILD_DIR/DEBIAN/prerm"
dpkg-deb --root-owner-group --build "$BUILD_DIR" "$OUTPUT_DIR/proxmox-betterdockers_0.19.0_all.deb"
