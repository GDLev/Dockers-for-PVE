#!/usr/bin/env bash
# No arguments open the terminal installer. Explicit actions retain the package worker interface.
set -Eeuo pipefail
PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ $# -eq 0 ]]; then
    exec python3 "$PROJECT_DIR/scripts/installer.py"
fi
for option in "$@"; do
    case "$option" in
        finish|--development-source|--allow-downgrade|--nodes|--nodes=*|--runtime|--yes|--no-restart|--interactive|--help|-h)
            exec python3 "$PROJECT_DIR/scripts/installer.py" "$@" ;;
    esac
done
ACTION=install
DEFER_RESTART=0
OPERATION=
stage() {
    if [[ ${BETTERDOCKER_INSTALL_PROGRESS:-0} == 1 ]]; then
        printf '[betterdocker-stage] %s\n' "$1"
    fi
}
cache_and_mark() {
    python3 "$PROJECT_DIR/scripts/installer.py" --cache-source "$PROJECT_DIR"
    python3 "$PROJECT_DIR/scripts/installer.py" --mark-restart "$ACTION"
}
finish_services() {
    if [[ $DEFER_RESTART == 0 ]]; then
        python3 "$PROJECT_DIR/scripts/installer.py" --finish-restart
    fi
}
if [[ ${1:-} != --* && $# -gt 0 ]]; then ACTION=$1; shift; fi
RUNTIME_ARGUMENTS=()
for option in "$@"; do
    case "$option" in
        --runtime=rootful|--runtime=rootless) RUNTIME_ARGUMENTS+=("$option") ;;
        --defer-restart) DEFER_RESTART=1 ;;
        --operation=install|--operation=repair|--operation=uninstall|--operation=check) OPERATION=${option#--operation=} ;;
        *) echo "Unknown option: $option" >&2; exit 2 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo 'Run as root on a Proxmox VE host.' >&2; exit 1; }
for command in pveversion pvesh perl systemctl flock; do
    command -v "$command" >/dev/null || { echo "Missing: $command" >&2; exit 1; }
done
if [[ ${BETTERDOCKER_DEVELOPMENT_SOURCE:-0} == 1 && ! -e "$PROJECT_DIR/.release.json" ]]; then
    echo 'WARNING: explicitly using unsigned development source.' >&2
else
    python3 "$PROJECT_DIR/scripts/release.py" source "$PROJECT_DIR"
fi
exec 9>/run/lock/betterdocker-install.lock
flock -n 9 || { echo 'Another BetterDocker installer is running.' >&2; exit 1; }
VERSION=$(pveversion)
[[ "$ACTION" == uninstall || "$ACTION" == finish-restart || ${OPERATION:-} == uninstall || "$VERSION" =~ pve-manager/9\.(1|2)\. ]] || {
    echo "Unsupported version: $VERSION. Supported PVE releases: 9.1 and 9.2." >&2; exit 1;
}
check_uninstall() {
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
}
check_repair_dependencies() {
    for command in python3 curl arping dhclient nft iptables-translate; do
        command -v "$command" >/dev/null || { echo "Missing $command; run start.sh install." >&2; return 1; }
    done
    python3 -c 'import yaml' >/dev/null 2>&1 || { echo 'Missing python3-yaml; run start.sh install.' >&2; return 1; }
}
case "$ACTION" in
    finish-restart)
        stage restart
        python3 "$PROJECT_DIR/scripts/installer.py" --finish-restart
        exit 0 ;;
    preflight)
        case "$OPERATION" in
            uninstall) check_uninstall ;;
            install|repair)
                PREFLIGHT_MODE=$(python3 "$PROJECT_DIR/scripts/runtime_install.py" select "${RUNTIME_ARGUMENTS[@]}")
                if [[ "$OPERATION" == repair ]]; then
                    check_repair_dependencies
                    command -v docker >/dev/null || { echo 'Docker is missing; choose Install / Update.' >&2; exit 1; }
                    docker compose version >/dev/null
                    python3 "$PROJECT_DIR/scripts/runtime_install.py" preflight --runtime="$PREFLIGHT_MODE"
                fi
                python3 "$PROJECT_DIR/scripts/patch.py" check ;;
            check) python3 "$PROJECT_DIR/scripts/patch.py" check ;;
            *) echo 'Preflight requires --operation=install|repair|uninstall|check.' >&2; exit 2 ;;
        esac
        exit 0 ;;
    check)
        command -v python3 >/dev/null || { echo 'Python 3 is missing. Run installation first.' >&2; exit 1; }
        python3 "$PROJECT_DIR/scripts/patch.py" check
        echo 'Proxmox file structure is compatible. Runtime testing requires installation.'
        exit 0 ;;
    uninstall)
        command -v python3 >/dev/null || { echo 'Python 3 is required for uninstall.' >&2; exit 1; }
        check_uninstall
        cache_and_mark
        stage remove
        systemctl disable --now betterdocker-integration.timer 2>/dev/null || true
        if [[ -f /etc/systemd/system/betterdocker-lifecycle.service ]]; then
            systemctl disable --now betterdocker-lifecycle.service
            /usr/local/lib/betterdocker/provision.py daemon-unmanage
            rm -f /etc/systemd/system/betterdocker-lifecycle.service /etc/systemd/system/pve-guests.service.d/betterdocker.conf
            systemctl daemon-reload
        fi
        python3 "$PROJECT_DIR/scripts/patch.py" remove
        perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
        rm -f /usr/sbin/dct /usr/share/perl5/PVE/CLI/dct.pm /usr/local/lib/betterdocker/dct.py /usr/share/perl5/PVE/API2/BetterDocker.pm /usr/share/pve-manager/js/betterdocker.js \
            /usr/share/pve-manager/css/betterdocker.css /usr/share/pve-manager/images/betterdocker.svg \
            /usr/share/pve-manager/js/betterdocker-locales/en.js /usr/share/pve-manager/js/betterdocker-locales/pl.js \
            /usr/share/perl5/PVE/BetterDocker/Backup.pm /usr/share/perl5/PVE/BetterDocker/Firewall.pm /usr/share/perl5/PVE/BetterDocker/Replication.pm /usr/share/perl5/PVE/VZDump/Docker.pm /usr/share/perl5/PVE/HA/Resources/Docker.pm
        rm -rf /usr/share/perl5/PVE/API2/BetterDocker
        finish_services
        echo 'UI integration removed. Docker projects, volumes, network/lease and firewall maintenance are retained.'
        exit 0 ;;
    install|repair) ;;
    *) echo 'Usage: bash start.sh [install|repair|uninstall|check]' >&2; exit 2 ;;
esac
RUNTIME_MODE=$(python3 "$PROJECT_DIR/scripts/runtime_install.py" select "${RUNTIME_ARGUMENTS[@]}")
if [[ "$ACTION" == install ]]; then
    stage dependencies
    echo 'Updating the package index and installing BetterDocker dependencies.'
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-yaml apparmor ca-certificates curl iputils-arping isc-dhcp-client ndisc6 pve-xtermjs nftables iptables rsync
    if [[ "$RUNTIME_MODE" == rootless ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y uidmap dbus-user-session
    fi
else
    check_repair_dependencies
fi
python3 "$PROJECT_DIR/scripts/patch.py" check
stage backup
BACKUP_DIR="/var/backups/betterdocker/$(date +%Y%m%d-%H%M%S)-$$"
install -d -m 0700 "$BACKUP_DIR"
python3 "$PROJECT_DIR/scripts/runtime_install.py" snapshot-support --backup-dir="$BACKUP_DIR"
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
    /usr/local/lib/betterdocker/firewall.py
    /usr/local/lib/betterdocker/firewall.pl
    /usr/share/perl5/PVE/BetterDocker/Firewall.pm
    /etc/systemd/system/betterdocker-firewall.service
    /etc/systemd/system/betterdocker-firewall-restore.service
    /etc/systemd/system/docker.service.d/betterdocker-firewall.conf
    /usr/sbin/dct
    /usr/local/lib/betterdocker/runtime.py
    /usr/local/lib/betterdocker/runtime_install.py
    /usr/local/lib/betterdocker/host.py
    /usr/local/lib/betterdocker/migrate-runtime.py
    /etc/betterdocker/runtime.json
    /usr/local/lib/betterdocker/rootless-engine.sh
)
TARGETS+=(/etc/systemd/system/betterdocker-dhcp6@.service)
for source in "$PROJECT_DIR"/scripts/dctlib/*.py; do
    TARGETS+=("/usr/local/lib/betterdocker/dctlib/$(basename "$source")")
done
for source in "$PROJECT_DIR"/backend/PVE/API2/BetterDocker/*.pm; do
    TARGETS+=("/usr/share/perl5/PVE/API2/BetterDocker/$(basename "$source")")
done
TARGETS+=(/usr/local/lib/betterdocker/service_security.py /usr/local/lib/betterdocker/release.py /var/lib/betterdocker/installer/release.json)
TARGETS+=(/usr/local/lib/betterdocker/parse_config.py /usr/local/lib/betterdocker/archive_scan.py /usr/local/lib/betterdocker/security_install.py /etc/apparmor.d/betterdocker /etc/betterdocker/recovery.key)
TARGETS+=(/usr/share/perl5/PVE/CLI/dct.pm /usr/local/lib/betterdocker/dct.py /usr/local/lib/betterdocker/rrd.pl)
for index in "${!TARGETS[@]}"; do
    if [[ -e "${TARGETS[$index]}" ]]; then
        cp -a "${TARGETS[$index]}" "$BACKUP_DIR/addon-$index"
    fi
done
rollback() {
    echo "Installation failed; restoring integration. Backups: $BACKUP_DIR" >&2
    if [[ ! -e "$BACKUP_DIR/addon-42" && -e /etc/betterdocker/runtime.json ]]; then
        if [[ $(python3 /usr/local/lib/betterdocker/runtime.py identity 2>/dev/null) == *'"rootless"'* ]]; then
            python3 /usr/local/lib/betterdocker/runtime.py service stop || true
            systemctl disable --now betterdocker-rootless.service || true
            python3 /usr/local/lib/betterdocker/host.py remove || true
        fi
    fi
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
    python3 "$PROJECT_DIR/scripts/runtime_install.py" restore-support --backup-dir="$BACKUP_DIR" || true
    if [[ ! -e "$BACKUP_DIR/addon-34" ]]; then systemctl disable --now betterdocker-firewall.service || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-8" ]]; then systemctl disable --now betterdocker-refresh.timer || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-13" ]]; then systemctl disable --now betterdocker-integration.timer || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-6" ]]; then systemctl disable --now betterdocker-addresses.service || true; fi
    if [[ ! -e "$BACKUP_DIR/addon-16" ]]; then systemctl disable --now betterdocker-lifecycle.service || true; fi
    if [[ -f /etc/apparmor.d/usr.sbin.dhclient ]] && command -v apparmor_parser >/dev/null; then
        apparmor_parser -r /etc/apparmor.d/usr.sbin.dhclient || true
    fi
    systemctl daemon-reload || true
    finish_services || true
}
cache_and_mark
trap rollback ERR
stage integration
install -d -m 0755 /usr/local/lib/betterdocker /usr/local/lib/betterdocker/dctlib
install -d -m 0755 /usr/share/pve-manager/js/betterdocker-locales
install -d -m 0755 /usr/share/perl5/PVE/BetterDocker /usr/share/perl5/PVE/VZDump /usr/share/perl5/PVE/API2/BetterDocker
for source in "$PROJECT_DIR"/scripts/dctlib/*.py; do
    install -m 0644 "$source" "/usr/local/lib/betterdocker/dctlib/$(basename "$source")"
done
for source in "$PROJECT_DIR"/backend/PVE/API2/BetterDocker/*.pm; do
    install -m 0644 "$source" "/usr/share/perl5/PVE/API2/BetterDocker/$(basename "$source")"
done
install -m 0755 "$PROJECT_DIR/scripts/archive_scan.py" /usr/local/lib/betterdocker/archive_scan.py
install -m 0755 "$PROJECT_DIR/scripts/parse_config.py" /usr/local/lib/betterdocker/parse_config.py
install -m 0755 "$PROJECT_DIR/scripts/security_install.py" /usr/local/lib/betterdocker/security_install.py
install -m 0755 "$PROJECT_DIR/scripts/release.py" /usr/local/lib/betterdocker/release.py
install -m 0644 "$PROJECT_DIR/scripts/service_security.py" /usr/local/lib/betterdocker/service_security.py
install -m 0755 "$PROJECT_DIR/scripts/hooks.pl" "${TARGETS[30]}"
install -d -m 0755 /usr/share/perl5/PVE/CLI
install -m 0644 "$PROJECT_DIR/backend/PVE/CLI/dct.pm" /usr/share/perl5/PVE/CLI/dct.pm
install -m 0755 "$PROJECT_DIR/scripts/dct.py" /usr/local/lib/betterdocker/dct.py
install -m 0755 "$PROJECT_DIR/scripts/dct" "${TARGETS[37]}"
install -m 0755 "$PROJECT_DIR/scripts/runtime.py" "${TARGETS[38]}"
install -m 0755 "$PROJECT_DIR/scripts/runtime_install.py" "${TARGETS[39]}"
install -m 0755 "$PROJECT_DIR/scripts/host.py" "${TARGETS[40]}"
install -m 0755 "$PROJECT_DIR/scripts/migrate-runtime.py" "${TARGETS[41]}"
install -m 0755 "$PROJECT_DIR/scripts/rootless-engine.sh" "${TARGETS[43]}"
install -m 0755 "$PROJECT_DIR/scripts/firewall.py" "${TARGETS[31]}"
install -m 0755 "$PROJECT_DIR/scripts/firewall.pl" "${TARGETS[32]}"
install -m 0644 "$PROJECT_DIR/backend/PVE/BetterDocker/Firewall.pm" "${TARGETS[33]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-firewall.service" "${TARGETS[34]}"
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-firewall-restore.service" "${TARGETS[35]}"
install -d -m 0755 /etc/systemd/system/docker.service.d
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-firewall-docker.conf" "${TARGETS[36]}"
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
install -m 0755 "$PROJECT_DIR/scripts/rrd.pl" /usr/local/lib/betterdocker/rrd.pl
install -m 0755 "$PROJECT_DIR/scripts/provision.py" "${TARGETS[0]}"
install -m 0755 "$PROJECT_DIR/scripts/docker-install.sh" "${TARGETS[1]}"
install -m 0644 "$PROJECT_DIR/backend/PVE/API2/BetterDocker.pm" "${TARGETS[2]}"
python3 "$PROJECT_DIR/scripts/build_frontend.py" --output "${TARGETS[3]}"
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
stage runtime
bash /usr/local/lib/betterdocker/docker-install.sh "$RUNTIME_MODE"
python3 /usr/local/lib/betterdocker/runtime_install.py initialize --runtime="$RUNTIME_MODE"
python3 /usr/local/lib/betterdocker/security_install.py "$RUNTIME_MODE" "$PROJECT_DIR/assets/betterdocker.apparmor"
# Refresh existing mount definitions too; repair must fix their boot ordering.
python3 - <<'PY'
import sys
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import projects as worker
with worker.locked():
    for info in worker.summaries():
        if info['node'] == worker.current_node():
            storage = worker.storage_config(info['id'])
            if storage:
                worker.storage_unit(info['id'], storage)
PY
install -m 0644 "$PROJECT_DIR/systemd/betterdocker-dhcp6@.service" /etc/systemd/system/betterdocker-dhcp6@.service
stage services
systemctl daemon-reload
systemctl enable --now betterdocker-firewall.service
systemctl start betterdocker-firewall-restore.service
systemctl restart betterdocker-firewall.service
systemctl enable --now betterdocker-addresses.service betterdocker-refresh.timer betterdocker-integration.timer betterdocker-lifecycle.service
systemctl restart betterdocker-addresses.service
python3 "$PROJECT_DIR/scripts/patch.py" apply
perl -I/usr/share/perl5 -MPVE::API2::Cluster -e 'PVE::API2::BetterDocker::migrate_ids()'
perl -I/usr/share/perl5 -MPVE::API2::Nodes -e 'PVE::API2::Nodes->validate_method_schemas(1)'
perl -I/usr/share/perl5 -MPVE::CLI::dct -e 'PVE::CLI::dct->validate_method_schemas(1)'
finish_services
systemctl is-active --quiet pvedaemon pveproxy
NODE=$(hostname -s)
pvesh usage "/nodes/$NODE/docker" --verbose >/dev/null
pvesh get /cluster/resources --output-format json >/dev/null
if [[ -f "$PROJECT_DIR/.release.json" ]]; then
    python3 "$PROJECT_DIR/scripts/release.py" record "$PROJECT_DIR"
fi
trap - ERR
if [[ $DEFER_RESTART == 1 ]]; then
    echo 'BetterDocker installed. Panel/API reload is pending.'
else
    echo 'BetterDocker installed. Refresh with Ctrl+Shift+R and sign in as root@pam.'
fi
echo "File backups: $BACKUP_DIR"
