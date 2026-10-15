#!/usr/bin/env bash
# Build on Linux with dpkg-deb. Package triggers reapply the addon after PVE updates.
set -Eeuo pipefail
umask 022
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT_DIR=${1:-"$SOURCE_DIR/dist"}
RELEASE_VERSION=$(python3 "$SOURCE_DIR/scripts/dctlib/version.py")
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
chmod 0755 "$BUILD_DIR"
mkdir -p "$BUILD_DIR/DEBIAN" "$BUILD_DIR/opt/betterdocker" "$OUTPUT_DIR"
cp -a "$SOURCE_DIR/assets" "$SOURCE_DIR/frontend" "$SOURCE_DIR/backend" "$SOURCE_DIR/scripts" "$SOURCE_DIR/systemd" "$SOURCE_DIR/start.sh" "$SOURCE_DIR/install.sh" "$BUILD_DIR/opt/betterdocker/"
mkdir -p "$BUILD_DIR/usr/share/doc/proxmox-betterdockers"
for document in README.md API_DOCS.md CONTRIBUTING.md SECURITY.md RELEASE_NOTES.md; do
    cp "$SOURCE_DIR/$document" "$BUILD_DIR/usr/share/doc/proxmox-betterdockers/"
done
cat > "$BUILD_DIR/usr/share/doc/proxmox-betterdockers/copyright" <<'COPYRIGHT'
Proxmox BetterDockers
Copyright 2026 GDLev and contributors
Licensed under the GNU Affero General Public License, version 3 or later.

COPYRIGHT
cat "$SOURCE_DIR/LICENSE" >> "$BUILD_DIR/usr/share/doc/proxmox-betterdockers/copyright"
find "$BUILD_DIR/opt/betterdocker" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$BUILD_DIR/opt/betterdocker" -name .DS_Store -delete
find "$BUILD_DIR/opt/betterdocker" -name '*.pyc' -delete
cat > "$BUILD_DIR/DEBIAN/control" <<'CONTROL'
Package: proxmox-betterdockers
Version: @VERSION@
Architecture: all
Maintainer: BetterDockers contributors
Depends: pve-manager (>= 9.1), pve-manager (<< 9.3), python3, python3-yaml, openssl, apparmor, e2fsprogs, util-linux, rsync, coreutils, nftables, iptables, curl, iputils-arping, isc-dhcp-client, ndisc6, pve-xtermjs, docker-ce | docker.io, docker-compose-plugin (>= 5.6.0) | docker-compose-v2 (>= 5.6.0), uidmap, dbus-user-session, docker-ce-rootless-extras | rootlesskit, debconf
Recommends: docker-ce-rootless-extras
Description: Native Proxmox VE interface for host Docker Compose projects
CONTROL
sed -i "s/^Version: .*/Version: $RELEASE_VERSION/" "$BUILD_DIR/DEBIAN/control"
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
cat > "$BUILD_DIR/DEBIAN/preinst" <<'PREINST'
#!/bin/sh
set -eu
case "$1" in
    install|upgrade)
        auth=/var/lib/betterdocker/installer/deb-authorization
        test -f "$auth/.release.json" && test -f "$auth/.release.sig" || {
            echo 'Install through release.py deb with signed metadata.' >&2
            exit 1
        }
        openssl dgst -sha256 -verify /etc/betterdocker/release-key.pem -signature "$auth/.release.sig" "$auth/.release.json" >/dev/null
        python3 - "$auth" <<'VERIFY'
import hashlib, json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
for path in (root, *root.parents, pathlib.Path('/etc/betterdocker/release-key.pem'), pathlib.Path('/etc/betterdocker/release-key.sha256')):
    st = path.lstat()
    if path.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022: raise SystemExit('Unprotected release authorization')
key = subprocess.check_output(['openssl','pkey','-pubin','-in','/etc/betterdocker/release-key.pem','-outform','DER'])
if hashlib.sha256(key).hexdigest() != pathlib.Path('/etc/betterdocker/release-key.sha256').read_text().strip(): raise SystemExit('Release key fingerprint mismatch')
m = json.loads((root / '.release.json').read_text())
name = 'proxmox-betterdockers_' + m['version'] + '_all.deb'
with (root / name).open('rb') as f:
    if hashlib.file_digest(f, 'sha256').hexdigest() != m['artifacts'][name]: raise SystemExit('DEB checksum mismatch')
VERIFY
        ;;
esac
PREINST
cat > "$BUILD_DIR/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -eu
case "$1" in
    configure)
        auth=/var/lib/betterdocker/installer/deb-authorization
        if [ -f "$auth/.release.json" ]; then
            install -m 0644 "$auth/.release.json" /opt/betterdocker/.release.json
            install -m 0644 "$auth/.release.sig" /opt/betterdocker/.release.sig
        fi
        python3 /opt/betterdocker/scripts/release.py source /opt/betterdocker
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
    remove)
        bash /opt/betterdocker/start.sh uninstall
        find /opt/betterdocker -type d -name __pycache__ -prune -exec rm -rf {} + ;;
esac
PRERM
chmod 0755 "$BUILD_DIR/DEBIAN/preinst" "$BUILD_DIR/DEBIAN/postinst" "$BUILD_DIR/DEBIAN/prerm"
# Runner checkout modes are not installation permissions.
find "$BUILD_DIR" -type d -exec chmod 0755 {} +
find "$BUILD_DIR" -type f -perm /111 -exec chmod 0755 {} +
find "$BUILD_DIR" -type f ! -perm /111 -exec chmod 0644 {} +
dpkg-deb --root-owner-group --build "$BUILD_DIR" "$OUTPUT_DIR/proxmox-betterdockers_${RELEASE_VERSION}_all.deb"
