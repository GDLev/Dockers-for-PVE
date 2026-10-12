#!/usr/bin/env bash
# Remote entry point for the corresponding immutable release.
# Define the entire entry point before executing it when read from a pipe.
set -Eeuo pipefail

main() {
    local project_dir
    if [[ ${1:-} == --help || ${1:-} == -h ]]; then
        echo 'Usage: bash install.sh [install|repair|uninstall|finish|check] [--nodes=node1,node2] [--runtime=rootful|rootless] [--yes] [--no-restart] [--interactive]'
        return 0
    fi
    [[ $EUID -eq 0 ]] || { echo 'Run as root on a Proxmox VE host.' >&2; return 1; }
    for command in pveversion python3 curl tar mktemp; do
        command -v "$command" >/dev/null || { echo "Missing: $command" >&2; return 1; }
    done

    project_dir=$(mktemp -d /tmp/betterdocker-install.XXXXXXXX)
    # Expand the path now: the local variable is gone when the EXIT trap runs.
    trap "rm -rf -- '$project_dir'" EXIT
    local release_version=0.20.0
    local archive_name="betterdockers-$release_version.tar.gz"
    local release_url="https://github.com/GDLev/Proxmox-BetterDockers/-/releases/v$release_version/downloads"
    local expected_sha=${BETTERDOCKER_SOURCE_SHA256:-}
    if [[ -n ${BETTERDOCKER_SOURCE_URL:-} && -z $expected_sha ]]; then
        echo 'A custom source URL requires BETTERDOCKER_SOURCE_SHA256.' >&2
        return 1
    fi
    if [[ -z $expected_sha ]]; then
        curl --fail --silent --show-error --location --retry 3 --max-filesize 65536 \
            "$release_url/SOURCE_SHA256SUMS" --output "$project_dir/SOURCE_SHA256SUMS" </dev/null
        expected_sha=$(awk -v name="$archive_name" '$2 == name { print $1 }' "$project_dir/SOURCE_SHA256SUMS")
    fi
    [[ $expected_sha =~ ^[0-9a-fA-F]{64}$ ]] || { echo 'Invalid release checksum.' >&2; return 1; }
    echo "Downloading Proxmox BetterDockers $release_version."
    curl --fail --silent --show-error --location --retry 3 \
        "${BETTERDOCKER_SOURCE_URL:-$release_url/$archive_name}" --max-filesize 134217728 \
        --output "$project_dir/project.tar.gz" </dev/null
    python3 - "$project_dir/project.tar.gz" "$expected_sha" "$project_dir" <<'PY'
import hashlib, pathlib, sys, tarfile
archive, expected, directory = sys.argv[1:]
with open(archive, 'rb') as handle:
    if hashlib.file_digest(handle, 'sha256').hexdigest() != expected.lower():
        raise SystemExit('Release checksum mismatch. Installation stopped.')
with tarfile.open(archive) as handle:
    members = handle.getmembers()
    prefixes = {pathlib.PurePosixPath(member.name).parts[0] for member in members}
    if len(prefixes) != 1 or any(not (member.isfile() or member.isdir()) or pathlib.PurePosixPath(member.name).is_absolute()
            or '..' in pathlib.PurePosixPath(member.name).parts for member in members):
        raise SystemExit('Unsafe release archive. Installation stopped.')
    total = sum(member.size for member in members)
    if total > 256 * 1024 * 1024 or len(members) > 10000:
        raise SystemExit('Release archive is too large.')
    for member in members:
        member.name = str(pathlib.PurePosixPath(*pathlib.PurePosixPath(member.name).parts[1:]))
    handle.extractall(directory, members=members, filter='data')
PY
    [[ -f "$project_dir/start.sh" && -f "$project_dir/scripts/installer.py" ]] || {
        echo 'Downloaded archive does not contain the BetterDocker installer.' >&2
        return 1
    }
    # Package managers must not consume the incoming installation script.
    python3 "$project_dir/scripts/installer.py" "$@" </dev/null
}

main "$@"
