#!/usr/bin/env bash
# Remote entry point for the corresponding immutable release.
# Define the entire entry point before executing it when read from a pipe.
set -Eeuo pipefail
umask 077

main() {
    for option in "$@"; do
        [[ "$option" != --allow-downgrade ]] || export BETTERDOCKER_ALLOW_DOWNGRADE=1
    done
    local project_dir
    if [[ ${1:-} == --help || ${1:-} == -h ]]; then
        echo 'Usage: bash install.sh [install|repair|uninstall|migrate|finish|check] [--nodes=node1,node2] [--runtime=rootful|rootless] [--yes] [--no-restart] [--interactive]'
        return 0
    fi
    [[ $EUID -eq 0 ]] || { echo 'Run as root on a Proxmox VE host.' >&2; return 1; }
    for command in pveversion python3 curl tar mktemp openssl; do
        command -v "$command" >/dev/null || { echo "Missing: $command" >&2; return 1; }
    done

    project_dir=$(mktemp -d /tmp/betterdocker-install.XXXXXXXX)
    # Expand the path now: the local variable is gone when the EXIT trap runs.
    trap "rm -rf -- '$project_dir'" EXIT
    local release_version=0.20.2
    local archive_name="betterdockers-$release_version.tar.gz"
    local release_url="https://github.com/GDLev/Dockers-for-PVE/releases/download/v$release_version"
    local trusted_key=/etc/betterdocker/release-key.pem
    local trusted_pin=/etc/betterdocker/release-key.sha256
    python3 - "$trusted_key" "$trusted_pin" <<'TRUST'
import fcntl, hashlib, os, pathlib, re, stat, subprocess, sys

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEApNYWKQTS26OjyZmBoLVl
ueTC3fbRQ9nLfkOAXkfG3bOfuUKUKkceMENcH4IBMaHvIFf0uTEXyVgjk7WC4CUZ
Hu8BMeojT77j9yzZwrgGdci9uXfxBgj4VXtVsgYpcIODDUBpv2JBPQ9JinVfz/Mi
7EHaCoMSfSqrHxSG86Hy9MwqlP7ZIzy4LmucUgYMNydAvn+sieeTjoTgGqDyaSo3
tLkRP2TwQT1VBk77i4iQyuU/MBkzjAlWp4nPOsDqrEbEq/jig8LbArQ0LEIuMgVN
6tLVXtdHutZqj8GlUeMdHsoVTrIaAzv50DmjWzOzcuZyQVKUIZ0Iy0TiWA48xf1E
l65fdzzSqKmZ09FlHsF2tnMZuDLtYZafG2LZ3jbAIKKbAXCnBI/NduMaNGUBZtiA
ST0KKr+s1qrsK5DnspoB3zmQqQelQ5+s5bUXsprqOaBotqA8gM9JmiOWPbCLbqDO
mKPl6BhZ0w967nLOlcepOzIRP7IAnb1AzXAHds007DtDAgMBAAE=
-----END PUBLIC KEY-----
"""
FINGERPRINT = '8f1983fa57cf5e003b0cd1e8885472c20a8f5fd7f3b376b6d154714a97fc77ff'
key, pin = map(pathlib.Path, sys.argv[1:])

def protected(path):
    for part in reversed([path, *path.parents]):
        try:
            value = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise SystemExit('Release trust files and directories must be root-owned, protected and free of symbolic links.')
        if part != path and not stat.S_ISDIR(value.st_mode):
            raise SystemExit('Release trust parent is not a directory.')

def verify(public, expected):
    result = subprocess.run(['openssl', 'pkey', '-pubin', '-outform', 'DER'], input=public,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode or not re.fullmatch(r'[0-9a-f]{64}', expected) or hashlib.sha256(result.stdout).hexdigest() != expected:
        raise SystemExit('Release key fingerprint mismatch. Existing trust files were not changed.')

for path in (key, pin):
    protected(path)
key.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
lock_path = key.parent / '.release-trust.lock'
protected(lock_path)
if lock_path.exists() and not lock_path.is_file():
    raise SystemExit('Invalid release trust lock file.')
fd = os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
with os.fdopen(fd, 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    for path in (key, pin):
        protected(path)
    present = [path.exists() for path in (key, pin)]
    if any(present):
        if not all(present):
            raise SystemExit('Release trust setup is incomplete. Restore both release-key.pem and release-key.sha256 in /etc/betterdocker before retrying. Existing files were not changed.')
        for path, maximum in ((key, 16384), (pin, 256)):
            if not path.is_file() or path.stat().st_size > maximum:
                raise SystemExit('Invalid release trust file: ' + str(path))
        try:
            verify(key.read_bytes(), pin.read_text().strip())
        except (OSError, UnicodeError):
            raise SystemExit('Cannot read release trust files. Existing files were not changed.')
    else:
        verify(PUBLIC_KEY.encode(), FINGERPRINT)
        for path, content in ((key, PUBLIC_KEY), (pin, FINGERPRINT + '\n')):
            with path.open('x') as output:
                os.fchmod(output.fileno(), 0o644)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        print('Release signing key configured. Future runs will reuse it.')
TRUST
    [[ -z ${BETTERDOCKER_SOURCE_SHA256:-} ]] || { echo 'Checksum overrides are no longer supported; use signed release metadata.' >&2; return 1; }
    local metadata_url=${BETTERDOCKER_RELEASE_URL:-$release_url}
    for file in RELEASE.json RELEASE.sig; do
        curl --proto '=https' --proto-redir '=https' --fail --silent --show-error --location --retry 3 --max-filesize 4194304 \
            "$metadata_url/$file" --output "$project_dir/$file" </dev/null
    done
    openssl dgst -sha256 -verify "$trusted_key" -signature "$project_dir/RELEASE.sig" "$project_dir/RELEASE.json" >/dev/null
    local installer_path="${BASH_SOURCE[0]:-}" streamed=0
    if [[ ! -f "$installer_path" ]]; then
        streamed=1
        installer_path="$project_dir/install.sh"
        curl --proto '=https' --proto-redir '=https' --fail --silent --show-error --location --retry 3 --max-filesize 4194304 \
            "$metadata_url/install.sh" --output "$installer_path" </dev/null
    fi
    local expected_sha
    expected_sha=$(python3 - "$project_dir/RELEASE.json" "$release_version" "$installer_path" <<'METADATA'
import hashlib, json, pathlib, re, sys
path, expected, installer = sys.argv[1:]
def pairs(items):
    result = {}
    for k, v in items:
        if k in result: raise SystemExit('Duplicate release metadata field.')
        result[k] = v
    return result
m = json.loads(pathlib.Path(path).read_text(), object_pairs_hook=pairs)
if m.get('format') != 1 or m.get('version') != expected:
    raise SystemExit('Release version mismatch.')
artifacts = m['artifacts']
if hashlib.sha256(pathlib.Path(installer).read_bytes()).hexdigest() != artifacts['install.sh']:
    raise SystemExit('Installer checksum mismatch. Download and verify install.sh before execution.')
sha = artifacts['betterdockers-' + expected + '.tar.gz']
if not re.fullmatch('[0-9a-f]{64}', sha): raise SystemExit('Invalid signed archive checksum.')
print(sha)
METADATA
)
    if (( streamed )); then
        bash "$installer_path" "$@" </dev/null
        return
    fi
    echo "Downloading Dockers for PVE $release_version."
    curl --proto '=https' --proto-redir '=https' --fail --silent --show-error --location --retry 3 \
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
    # Debian 12's original Python 3.11 lacks the extraction-filter API.
    options = {'filter': 'fully_trusted'} if hasattr(tarfile, 'fully_trusted_filter') else {}
    handle.extractall('/proc/self/fd/' + str(output_fd), members=members, **options)
PY
    [[ -f "$project_dir/start.sh" && -f "$project_dir/scripts/installer.py" ]] || {
        echo 'Downloaded archive does not contain the Dockers for PVE installer.' >&2
        return 1
    }
    cp "$project_dir/RELEASE.json" "$project_dir/.release.json"
    cp "$project_dir/RELEASE.sig" "$project_dir/.release.sig"
    python3 "$project_dir/scripts/release.py" source "$project_dir"
    # Package managers must not consume the incoming installation script.
    python3 "$project_dir/scripts/installer.py" "$@" </dev/null
}

main "$@"
