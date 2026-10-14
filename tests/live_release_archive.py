#!/usr/bin/env python3
"""Run the actual bootstrap archive reader without executing the installer."""
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def main():
    source = (Path(__file__).resolve().parents[1] / 'install.sh').read_text()
    reader = source.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    def extract(kind):
        with tempfile.TemporaryDirectory(prefix='bd-release-fixture-') as temporary:
            folder = Path(temporary); archive = folder / 'source.tar.gz'; out = folder / 'output'; out.mkdir(mode=0o700)
            with tarfile.open(archive, 'w:gz') as handle:
                entry = tarfile.TarInfo('release/start.sh'); entry.mode = 0o4755; entry.size = 2
                handle.addfile(entry, io.BytesIO(b'ok'))
                if kind != 'valid':
                    bad = tarfile.TarInfo({'traversal': 'release/../../outside', 'absolute': '/etc/bd-archive-unwanted', 'link': 'release/link', 'duplicate': 'release/./start.sh', 'special': 'release/device'}[kind])
                    if kind == 'link': bad.type = tarfile.SYMTYPE; bad.linkname = '/etc/shadow'
                    if kind == 'special': bad.type = tarfile.CHRTYPE
                    handle.addfile(bad)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            result = subprocess.run([sys.executable, '-', str(archive), digest, str(out)], input=reader, capture_output=True, text=True, timeout=70)
            if kind == 'valid':
                assert result.returncode == 0, result.stderr
                path = out / 'start.sh'
                assert path.read_text() == 'ok' and path.stat().st_uid == 0 and path.stat().st_mode & 0o7777 == 0o755
            else: assert result.returncode != 0, (kind, result.stdout, result.stderr)
    for kind in ('valid', 'traversal', 'absolute', 'link', 'duplicate', 'special'): extract(kind)
    print('PASS bootstrap extraction, ownership, stripped setuid and malicious release headers', flush=True)


if __name__ == '__main__': main()
