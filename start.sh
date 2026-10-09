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
[[ "$ACTION" == uninstall || "$VERSION" =~ pve-manager/9\.(1|2)\. ]] || {
    echo "Unsupported version: $VERSION. Supported PVE releases: 9.1 and 9.2." >&2; exit 1;
}
case "$ACTION" in
    check)
        command -v python3 >/dev/null || { echo 'Python 3 is missing. Run installation first.' >&2; exit 1; }
        python3 "$PROJECT_DIR/scripts/patch.py" check
        echo 'Proxmox file structure is compatible. Runtime testing requires installation.'
        exit 0 ;;
    uninstall)
        command -v python3 >/dev/null || { echo 'Python 3 is required for uninstall.' >&2; exit 1; }
        # The native parser skips unknown resource types when the provider is
        # not loaded. Check the on-disk sections before removing that provider.
        perl -e '
            my $path = "/etc/pve/ha/resources.cfg";
            if (-f $path) {
                open(my $fh, "<", $path) or die "$path: $!\n";
                local $/; my $config = <$fh>;
                die qq(Remove DCT HA resources before uninstalling BetterDocker.\n)
                    if $config =~ /^docker:[ \t]*[1-9][0-9]*[ \t]*$/m;
            }
        '
        perl -MPVE::ReplicationConfig -e '
            my $config = PVE::ReplicationConfig->new();
            for my $job (values %{$config->{ids}}) {
                die "Remove DCT replication jobs before uninstalling BetterDocker.\n"
                    if -f "/etc/pve/betterdocker/ids/$job->{guest}.json";
            }
        '
        systemctl disable --now betterdocker-integration.timer 2>/dev/null || true
        if [[ -f /etc/systemd/system/betterdocker-lifecycle.service ]]; then
            systemctl disable --now betterdocker-lifecycle.service
            /usr/local/lib/betterdocker/provision.py daemon-unmanage
            rm -f /etc/systemd/system/betterdocker-lifecycle.service /etc/systemd/system/pve-guests.service.d/betterdocker.conf
            systemctl daemon-reload
        fi
        python3 "$PROJECT_DIR/scripts/patch.py" remove
        perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
        systemctl restart pvedaemon pveproxy pvescheduler pve-ha-crm pve-ha-lrm
        rm -f /usr/share/perl5/PVE/API2/BetterDocker.pm /usr/share/pve-manager/js/betterdocker.js \
            /usr/share/pve-manager/css/betterdocker.css /usr/share/pve-manager/images/betterdocker.svg \
            /usr/share/pve-manager/js/betterdocker-locales/en.js /usr/share/pve-manager/js/betterdocker-locales/pl.js \
            /usr/share/perl5/PVE/BetterDocker/Backup.pm /usr/share/perl5/PVE/BetterDocker/Replication.pm /usr/share/perl5/PVE/VZDump/Docker.pm /usr/share/perl5/PVE/HA/Resources/Docker.pm
        echo 'UI integration removed. Docker projects, volumes and network/lease maintenance are retained.'
        exit 0 ;;
    install|repair) ;;
    *) echo 'Usage: bash start.sh [install|repair|uninstall|check]' >&2; exit 2 ;;
esac
if [[ "$ACTION" == install ]]; then
    echo 'Updating the package index and installing BetterDocker dependencies.'
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-yaml ca-certificates curl iputils-arping isc-dhcp-client pve-xtermjs
else
    for command in python3 curl arping dhclient; do
        command -v "$command" >/dev/null || { echo "Missing $command; run start.sh install." >&2; exit 1; }
    done
    python3 -c 'import yaml' >/dev/null 2>&1 || { echo 'Missing python3-yaml; run start.sh install.' >&2; exit 1; }
fi
python3 "$PROJECT_DIR/scripts/patch.py" check
BACKUP_DIR="/var/backups/betterdocker/$(date +%Y%m%d-%H%M%S)-$$"
install -d -m 0700 "$BACKUP_DIR"
cp -a /usr/share/perl5/PVE/API2/Nodes.pm "$BACKUP_DIR/Nodes.pm"
cp -a /usr/share/pve-manager/index.html.tpl "$BACKUP_DIR/index.html.tpl"
cp -a /usr/share/perl5/PVE/API2/Cluster.pm "$BACKUP_DIR/Cluster.pm"
cp -a /usr/share/perl5/PVE/Cluster.pm "$BACKUP_DIR/Cluster-lib.pm"
cp -a /usr/share/perl5/PVE/VZDump.pm "$BACKUP_DIR/VZDump.pm"
cp -a /usr/share/perl5/PVE/HA/Env/PVE2.pm "$BACKUP_DIR/HA-Env.pm"
cp -a /usr/share/perl5/PVE/API2/HA/Resources.pm "$BACKUP_DIR/HA-API.pm"
cp -a /usr/share/perl5/PVE/Replication.pm "$BACKUP_DIR/Replication.pm"
cp -a /usr/share/perl5/PVE/API2/Replication.pm "$BACKUP_DIR/Replication-API.pm"
cp -a /usr/share/pve-manager/js/pvemanagerlib.js "$BACKUP_DIR/pvemanagerlib.js"
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
    /usr/local/lib/betterdocker/patch.py
    /etc/systemd/system/betterdocker-integration.service
    /etc/systemd/system/betterdocker-integration.timer
    /usr/share/pve-manager/js/betterdocker-locales/en.js
    /usr/share/pve-manager/js/betterdocker-locales/pl.js
    /etc/systemd/system/betterdocker-lifecycle.service
    /etc/systemd/system/pve-guests.service.d/betterdocker.conf
    /etc/apparmor.d/local/betterdocker-dhclient
    /etc/apparmor.d/local/usr.sbin.dhclient
    /usr/local/lib/betterdocker/storage.pl
    /usr/local/lib/betterdocker/guest_state.py
    /usr/local/lib/betterdocker/backup.pl
    /usr/share/perl5/PVE/BetterDocker/Backup.pm
    /usr/share/perl5/PVE/VZDump/Docker.pm
    /usr/local/lib/betterdocker/transfer.py
    /usr/local/lib/betterdocker/replica.py
    /usr/share/perl5/PVE/BetterDocker/Replication.pm
    /usr/local/lib/betterdocker/ha.py
    /usr/share/perl5/PVE/HA/Resources/Docker.pm
    /usr/local/lib/betterdocker/hooks.pl
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
    cp -a "$BACKUP_DIR/VZDump.pm" /usr/share/perl5/PVE/VZDump.pm
    cp -a "$BACKUP_DIR/HA-Env.pm" /usr/share/perl5/PVE/HA/Env/PVE2.pm
    cp -a "$BACKUP_DIR/HA-API.pm" /usr/share/perl5/PVE/API2/HA/Resources.pm
    cp -a "$BACKUP_DIR/Replication.pm" /usr/share/perl5/PVE/Replication.pm
    cp -a "$BACKUP_DIR/Replication-API.pm" /usr/share/perl5/PVE/API2/Replication.pm
    cp -a "$BACKUP_DIR/pvemanagerlib.js" /usr/share/pve-manager/js/pvemanagerlib.js
    for index in "${!TARGETS[@]}"; do
        if [[ -e "$BACKUP_DIR/addon-$index" ]]; then
            cp -a "$BACKUP_DIR/addon-$index" "${TARGETS[$index]}"
        else
            rm -f "${TARGETS[$index]}"
        fi
    done
    if [[ ! -e "$BACKUP_DIR/addon-8" ]]; then systemctl disable --now betterdocker-refresh.timer || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-13" ]]; then systemctl disable --now betterdocker-integration.timer || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-6" ]]; then systemctl disable --now betterdocker-addresses.service || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-16" ]]; then systemctl disable --now betterdocker-lifecycle.service || true; fi
    if [[ -f /etc/apparmor.d/usr.sbin.dhclient ]] && command -v apparmor_parser >/dev/null; then
        apparmor_parser -r /etc/apparmor.d/usr.sbin.dhclient || true
    fi
    systemctl daemon-reload || true
    systemctl restart pvedaemon pveproxy pvescheduler pve-ha-crm pve-ha-lrm || true
}
trap rollback ERR
install -d -m 0755 /usr/local/lib/betterdocker
install -d -m 0755 /usr/share/pve-manager/js/betterdocker-locales
install -d -m 0755 /usr/share/perl5/PVE/BetterDocker /usr/share/perl5/PVE/VZDump
install -m 0755 "$PROJECT_DIR/scripts/hooks.pl" "${TARGETS[30]}"
install -m 0755 "$PROJECT_DIR/scripts/ha.py" "${TARGETS[28]}"
install -m 0644 "$PROJECT_DIR/backend/PVE/HA/Resources/Docker.pm" "${TARGETS[29]}"
install -m 0755 "$PROJECT_DIR/scripts/replica.py" "${TARGETS[26]}"
install -m 0644 "$PROJECT_DIR/backend/PVE/BetterDocker/Replication.pm" "${TARGETS[27]}"
install -m 0755 "$PROJECT_DIR/scripts/transfer.py" "${TARGETS[25]}"
install -m 0755 "$PROJECT_DIR/scripts/guest_state.py" "${TARGETS[21]}"
install -m 0755 "$PROJECT_DIR/scripts/backup.pl" "${TARGETS[22]}"
install -m 0644 "$PROJECT_DIR/backend/PVE/BetterDocker/Backup.pm" "${TARGETS[23]}"
install -m 0644 "$PROJECT_DIR/backend/PVE/VZDump/Docker.pm" "${TARGETS[24]}"
install -m 0755 "$PROJECT_DIR/scripts/storage.pl" "${TARGETS[20]}"
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
install -m 0755 "$PROJECT_DIR/scripts/patch.py" "${TARGETS[11]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-integration.service" "${TARGETS[12]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-integration.timer" "${TARGETS[13]}"
install -m 0644 "$PROJECT_DIR/frontend/locales/en.js" "${TARGETS[14]}"
install -m 0644 "$PROJECT_DIR/frontend/locales/pl.js" "${TARGETS[15]}"
install -d -m 0755 /etc/systemd/system/pve-guests.service.d
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-lifecycle.service" "${TARGETS[16]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-pve-guests.conf" "${TARGETS[17]}"
# Extend the distribution profile instead of disabling AppArmor for DHCP.
if [[ -f /etc/apparmor.d/usr.sbin.dhclient ]] && command -v apparmor_parser >/dev/null; then
    install -d -m 0755 /etc/apparmor.d/local
    install -m 0644 "$PROJECT_DIR/systemd/betterdocker-dhclient.apparmor" "${TARGETS[18]}"
    touch "${TARGETS[19]}"
    if ! grep -Fxq '#include if exists <local/betterdocker-dhclient>' "${TARGETS[19]}"; then
        printf '\n%s\n' '#include if exists <local/betterdocker-dhclient>' >> "${TARGETS[19]}"
    fi
    apparmor_parser -r /etc/apparmor.d/usr.sbin.dhclient
fi
bash /usr/local/lib/betterdocker/docker-install.sh
systemctl daemon-reload
systemctl enable --now betterdocker-addresses.service betterdocker-refresh.timer betterdocker-integration.timer betterdocker-lifecycle.service
python3 "$PROJECT_DIR/scripts/patch.py" apply
perl -I/usr/share/perl5 -MPVE::API2::Cluster -e 'PVE::API2::BetterDocker::migrate_ids()'
perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
systemctl restart pvedaemon pveproxy pvescheduler pve-ha-crm pve-ha-lrm
systemctl is-active --quiet pvedaemon pveproxy
NODE=$(hostname -s)
pvesh usage "/nodes/$NODE/docker" --verbose >/dev/null
pvesh get /cluster/resources --output-format json >/dev/null
trap - ERR
echo 'BetterDocker installed. Refresh with Ctrl+Shift+R and sign in as root@pam.'
echo "File backups: $BACKUP_DIR"
