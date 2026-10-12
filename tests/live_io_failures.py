#!/usr/bin/env python3
"""Exercise actual full-filesystem and unavailable-SSH boundary failures."""
import errno
import io
from pathlib import Path
import subprocess
import sys
import tempfile
sys.path.insert(0, '/usr/local/lib/betterdocker')
from dctlib import capacity, transfer_io


def main():
    with tempfile.TemporaryDirectory(prefix='bd-full-disk-') as folder:
        folder = Path(folder)
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=80m', 'tmpfs', str(folder)], check=True)
        try:
            marker = folder / 'original'; marker.write_text('kept')
            with (folder / 'filler').open('wb') as stream:
                try:
                    while True:
                        stream.write(b'x' * 1048576)
                except OSError as error:
                    assert error.errno == errno.ENOSPC
            try:
                capacity.require_space(folder, 1)
            except ValueError as error:
                assert 'Insufficient free space' in str(error)
            else:
                raise AssertionError('Full filesystem was accepted')
            assert marker.read_text() == 'kept'
            print('PASS actual ENOSPC and capacity refusal preserve existing files', flush=True)
        finally:
            subprocess.run(['umount', str(folder)], check=True)
    try:
        transfer_io.send(['ssh', '-oBatchMode=yes', '-oConnectTimeout=2', '-p1', 'root@127.0.0.1', 'true'], io.BytesIO(b'fixture'), rate=4096, timeout=10)
    except (subprocess.CalledProcessError, BrokenPipeError):
        print('PASS unavailable SSH fails the transfer without a stuck stream', flush=True)
    else:
        raise AssertionError('Unavailable SSH was accepted')


if __name__ == '__main__':
    main()
