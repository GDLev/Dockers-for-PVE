#!/usr/bin/env bash
# Run on each Proxmox VE node: bash start.sh install | repair | uninstall | check
set -Eeuo pipefail
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ACTION=${1:-install}
[[ $EUID -eq 0 ]] || { echo 'Uruchom jako root na hoście Proxmox VE.' >&2; exit 1; }
for command in pveversion pct pvesh python3 perl systemctl flock; do
    command -v "$command" >/dev/null || { echo "Brak: $command" >&2; exit 1; }
done
exec 9>/run/lock/betterdocker-install.lock
flock -n 9 || { echo 'Inny instalator BetterDocker już działa.' >&2; exit 1; }
VERSION=$(pveversion)
[[ "$VERSION" =~ pve-manager/(8|9)\. ]] || { echo "Nieobsługiwana wersja: $VERSION" >&2; exit 1; }
case "$ACTION" in
    check)
        python3 "$PROJECT_DIR/scripts/patch.py" check
        echo 'Struktura plików Proxmox jest zgodna. Test runtime wymaga instalacji.'
        exit 0 ;;
    uninstall)
        python3 "$PROJECT_DIR/scripts/patch.py" remove
        perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
        systemctl restart pvedaemon pveproxy
        rm -f /usr/share/perl5/PVE/API2/BetterDocker.pm /usr/share/pve-manager/js/betterdocker.js \
            /usr/share/pve-manager/css/betterdocker.css /usr/share/pve-manager/images/betterdocker.svg
        rm -rf /usr/local/lib/betterdocker
        echo 'Dodatek usunięty. Maszyny i dane pozostają dostępne jako LXC.'
        exit 0 ;;
    install|repair) ;;
    *) echo 'Użycie: bash start.sh [install|repair|uninstall|check]' >&2; exit 2 ;;
esac
python3 "$PROJECT_DIR/scripts/patch.py" check
BACKUP_DIR="/var/backups/betterdocker/$(date +%Y%m%d-%H%M%S)-$$"
install -d -m 0700 "$BACKUP_DIR"
cp -a /usr/share/perl5/PVE/API2/Nodes.pm "$BACKUP_DIR/Nodes.pm"
cp -a /usr/share/pve-manager/index.html.tpl "$BACKUP_DIR/index.html.tpl"
TARGETS=(
    /usr/local/lib/betterdocker/provision.py
    /usr/local/lib/betterdocker/docker-install.sh
    /usr/share/perl5/PVE/API2/BetterDocker.pm
    /usr/share/pve-manager/js/betterdocker.js
    /usr/share/pve-manager/css/betterdocker.css
    /usr/share/pve-manager/images/betterdocker.svg
)
for index in "${!TARGETS[@]}"; do
    if [[ -e "${TARGETS[$index]}" ]]; then
        cp -a "${TARGETS[$index]}" "$BACKUP_DIR/addon-$index"
    fi
done
rollback() {
    echo "Instalacja nie powiodła się; przywracam integrację. Kopie: $BACKUP_DIR" >&2
    cp -a "$BACKUP_DIR/Nodes.pm" /usr/share/perl5/PVE/API2/Nodes.pm
    cp -a "$BACKUP_DIR/index.html.tpl" /usr/share/pve-manager/index.html.tpl
    for index in "${!TARGETS[@]}"; do
        if [[ -e "$BACKUP_DIR/addon-$index" ]]; then
            cp -a "$BACKUP_DIR/addon-$index" "${TARGETS[$index]}"
        else
            rm -f "${TARGETS[$index]}"
        fi
    done
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
python3 "$PROJECT_DIR/scripts/patch.py" apply
perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
systemctl restart pvedaemon pveproxy
systemctl is-active --quiet pvedaemon pveproxy
NODE=$(hostname -s)
pvesh usage "/nodes/$NODE/docker" --verbose >/dev/null
trap - ERR
echo 'BetterDocker zainstalowany. Odśwież interfejs Ctrl+Shift+R i zaloguj się jako root@pam.'
echo "Kopie plików: $BACKUP_DIR"
