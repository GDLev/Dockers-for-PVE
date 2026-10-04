#!/usr/bin/env bash
# Run on each Proxmox VE node: bash start.sh install | repair | uninstall | check
set -Eeuo pipefail
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ACTION=${1:-install}
[[ $EUID -eq 0 ]] || { echo 'Run as root on a Proxmox VE host.' >&2; exit 1; }
for command in pveversion pvesh perl systemctl flock; do
    command -v "$command" >/dev/null || { echo "Missing: $command" >&2; exit 1; }
done
exec 9>/run/lock/betterdocker-install.lock
flock -n 9 || { echo 'Another BetterDocker installer is running.' >&2; exit 1; }
VERSION=$(pveversion)
[[ "$VERSION" =~ pve-manager/(8|9)\. ]] || { echo "Unsupported version: $VERSION" >&2; exit 1; }
case "$ACTION" in
    check)
        command -v python3 >/dev/null || { echo 'Python 3 is missing. Run installation first.' >&2; exit 1; }
        python3 "$PROJECT_DIR/scripts/patch.py" check
        echo 'Proxmox file structure is compatible. Runtime testing requires installation.'
        exit 0 ;;
    uninstall)
        command -v python3 >/dev/null || { echo 'Python 3 is required for uninstall.' >&2; exit 1; }
        python3 "$PROJECT_DIR/scripts/patch.py" remove
        perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
        systemctl restart pvedaemon pveproxy
        rm -f /usr/share/perl5/PVE/API2/BetterDocker.pm /usr/share/pve-manager/js/betterdocker.js \
            /usr/share/pve-manager/css/betterdocker.css /usr/share/pve-manager/images/betterdocker.svg
        echo 'UI integration removed. Docker projects, volumes and network/lease maintenance are retained.'
        exit 0 ;;
    install|repair) ;;
    *) echo 'Usage: bash start.sh [install|repair|uninstall|check]' >&2; exit 2 ;;
esac
echo 'Updating the package index and installing BetterDocker dependencies.'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 ca-certificates curl iputils-arping isc-dhcp-client pve-xtermjs
python3 "$PROJECT_DIR/scripts/patch.py" check
BACKUP_DIR="/var/backups/betterdocker/$(date +%Y%m%d-%H%M%S)-$$"
install -d -m 0700 "$BACKUP_DIR"
cp -a /usr/share/perl5/PVE/API2/Nodes.pm "$BACKUP_DIR/Nodes.pm"
cp -a /usr/share/pve-manager/index.html.tpl "$BACKUP_DIR/index.html.tpl"
cp -a /usr/share/perl5/PVE/API2/Cluster.pm "$BACKUP_DIR/Cluster.pm"
cp -a /usr/share/perl5/PVE/Cluster.pm "$BACKUP_DIR/Cluster-lib.pm"
TARGETS=(
    /usr/local/lib/betterdocker/provision.py
    /usr/local/lib/betterdocker/docker-install.sh
    /usr/share/perl5/PVE/API2/BetterDocker.pm
    /usr/share/pve-manager/js/betterdocker.js
    /usr/share/pve-manager/css/betterdocker.css
    /usr/share/pve-manager/images/betterdocker.svg
    /etc/systemd/system/betterdocker-addresses.service
    /etc/systemd/system/betterdocker-refresh.service
    /etc/systemd/system/betterdocker-refresh.timer
    /usr/local/lib/betterdocker/dhcp-hook.py
    /etc/systemd/system/betterdocker-dhcp@.service
)
for index in "${!TARGETS[@]}"; do
    if [[ -e "${TARGETS[$index]}" ]]; then
        cp -a "${TARGETS[$index]}" "$BACKUP_DIR/addon-$index"
    fi
done
rollback() {
    echo "Installation failed; restoring integration. Backups: $BACKUP_DIR" >&2
    cp -a "$BACKUP_DIR/Nodes.pm" /usr/share/perl5/PVE/API2/Nodes.pm
    cp -a "$BACKUP_DIR/index.html.tpl" /usr/share/pve-manager/index.html.tpl
    cp -a "$BACKUP_DIR/Cluster.pm" /usr/share/perl5/PVE/API2/Cluster.pm
    cp -a "$BACKUP_DIR/Cluster-lib.pm" /usr/share/perl5/PVE/Cluster.pm
    for index in "${!TARGETS[@]}"; do
        if [[ -e "$BACKUP_DIR/addon-$index" ]]; then
            cp -a "$BACKUP_DIR/addon-$index" "${TARGETS[$index]}"
        else
            rm -f "${TARGETS[$index]}"
        fi
    done
    if [[ ! -e "$BACKUP_DIR/addon-8" ]]; then systemctl disable --now betterdocker-refresh.timer || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-6" ]]; then systemctl disable --now betterdocker-addresses.service || true; fi
    systemctl daemon-reload || true
    systemctl restart pvedaemon pveproxy || true
}
trap rollback ERR
install -d -m 0755 /usr/local/lib/betterdocker
install -m 0755 "$PROJECT_DIR/scripts/provision.py" "${TARGETS[0]}"
install -m 0755 "$PROJECT_DIR/scripts/docker-install.sh" "${TARGETS[1]}"
install -m 0644 "$PROJECT_DIR/backend/PVE/API2/BetterDocker.pm" "${TARGETS[2]}"
install -m 0644 "$PROJECT_DIR/frontend/betterdocker.js" "${TARGETS[3]}"
install -m 0644 "$PROJECT_DIR/frontend/betterdocker.css" "${TARGETS[4]}"
install -m 0644 "$PROJECT_DIR/frontend/docker.svg" "${TARGETS[5]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-addresses.service" "${TARGETS[6]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-refresh.service" "${TARGETS[7]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-refresh.timer" "${TARGETS[8]}"
install -m 0755 "$PROJECT_DIR/scripts/dhcp-hook.py" "${TARGETS[9]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-dhcp@.service" "${TARGETS[10]}"
bash /usr/local/lib/betterdocker/docker-install.sh
systemctl daemon-reload
systemctl enable --now betterdocker-addresses.service betterdocker-refresh.timer
python3 "$PROJECT_DIR/scripts/patch.py" apply
perl -I/usr/share/perl5 -MPVE::API2::Cluster -e 'PVE::API2::BetterDocker::migrate_ids()'
perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
systemctl restart pvedaemon pveproxy
systemctl is-active --quiet pvedaemon pveproxy
NODE=$(hostname -s)
pvesh usage "/nodes/$NODE/docker" --verbose >/dev/null
pvesh get /cluster/resources --output-format json >/dev/null
trap - ERR
echo 'BetterDocker installed. Refresh with Ctrl+Shift+R and sign in as root@pam.'
echo "File backups: $BACKUP_DIR"
