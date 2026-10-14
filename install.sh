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
import ctypes, hashlib, pathlib, sys, tarfile, os, pwd, resource
archive, expected, directory = sys.argv[1:]
source = open(archive, 'rb')
if hashlib.file_digest(source, 'sha256').hexdigest() != expected.lower():
    raise SystemExit('Release checksum mismatch. Installation stopped.')
source.seek(0)
account = pwd.getpwnam('nobody')
output = pathlib.Path(directory) / 'verified-source'
output.mkdir(mode=0o700)
os.chown(output, account.pw_uid, account.pw_gid)
pid = os.fork()
if pid:
    _, status = os.waitpid(pid, 0)
    if status:
        raise SystemExit('Release archive inspection or extraction failed.')
    for base, dirs, files in os.walk(output, followlinks=False):
        os.chown(base, 0, 0)
        for name in dirs + files:
            os.chown(os.path.join(base, name), 0, 0, follow_symlinks=False)
    for item in output.iterdir():
        item.rename(pathlib.Path(directory) / item.name)
    output.rmdir()
    raise SystemExit(0)
output_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
os.setgroups([]); os.setgid(account.pw_gid); os.setuid(account.pw_uid)
if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) or ctypes.CDLL(None).prctl(38, 1, 0, 0, 0):
    raise SystemExit('Cannot isolate release archive reader.')
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
with tarfile.open(fileobj=source) as handle:
    members = []; names = set(); prefix = None; total = 0
    for member in handle:
        path = pathlib.PurePosixPath(member.name)
        if not path.parts or not (member.isfile() or member.isdir()) or path.is_absolute() or '..' in path.parts or '\x00' in member.name:
            raise SystemExit('Unsafe release archive. Installation stopped.')
        if prefix is None:
            prefix = path.parts[0]
        if path.parts[0] != prefix or len(path.parts) == 1 and not member.isdir():
            raise SystemExit('Release archive must contain one project directory.')
        if str(path) in names or len(member.name.encode()) > 4096:
            raise SystemExit('Duplicate or oversized release path.')
        total += member.size
        if member.size < 0 or total > 256 * 1024 * 1024 or len(members) >= 10000:
            raise SystemExit('Release archive is too large.')
        names.add(str(path))
        member.name = str(pathlib.PurePosixPath(*path.parts[1:]))
        member.uid = member.gid = 0
        member.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
        member.pax_headers = {}
        members.append(member)
    if not members:
        raise SystemExit('Release archive is empty.')
    # Members now contain only sanitized regular files and directories.
    # Keep the root-only parent inaccessible to other nobody processes.
    # The data filter resolves that private parent and cannot use this FD.
    handle.extractall('/proc/self/fd/' + str(output_fd), members=members, filter='fully_trusted')
PY
    [[ -f "$project_dir/start.sh" && -f "$project_dir/scripts/installer.py" ]] || {
        echo 'Downloaded archive does not contain the BetterDocker installer.' >&2
        return 1
    }
    # Package managers must not consume the incoming installation script.
    python3 "$project_dir/scripts/installer.py" "$@" </dev/null
}

main "$@"
