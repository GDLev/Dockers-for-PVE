#!/usr/bin/env python3
"""Bootstrap, numbered forms, late rollback and real uninstall/reinstall.

Test cluster only. Uses live_terminal_installer's journaled HA maintenance.
Run after its ordinary cluster acceptance, using the same deployed source.
"""
import live_environment as env
import argparse
from pathlib import Path
import shutil
import subprocess
import tarfile
import uuid

from live_terminal_installer import Session, panels, workloads, remote, lifecycle, NODES, PENDING


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    args = parser.parse_args()
    source = Path(args.source)
    folder = Path('/root/betterdocker-installer-edges-' + uuid.uuid4().hex[:8])
    folder.mkdir(mode=0o700)
    baseline = panels()
    original = workloads()
    archive = folder / 'bootstrap.tar.gz'
    with tarfile.open(archive, 'w:gz') as package:
        package.add(source, arcname='project', filter=lambda item: None if '__pycache__' in Path(item.name).parts else item)
    command = 'export BETTERDOCKER_SOURCE_URL=' + str(archive.as_uri()) + '; cat ' + str(source / 'install.sh') + ' | bash'
    ui = Session(['bash', '-c', command], folder / 'bootstrap.tty')
    ui.wait('Esc cancel'); ui.send(b'\x1b'); ui.finish(130)
    assert panels() == baseline
    print('PASS curl/file archive bootstrap reads forms from /dev/tty with piped script stdin', flush=True)

    ui = Session(['bash', str(source / 'start.sh')], folder / 'numbered.tty', term='dumb')
    ui.wait('Option number'); ui.send(b'2\n')
    ui.wait('Enter to continue'); ui.send(b'\n')
    ui.wait('Node numbers'); ui.send(b'3\n')
    ui.wait('Press Enter to start'); ui.send(b'q\n'); ui.finish(130)
    assert panels() == baseline and b'\x1b[' not in ui.output
    print('PASS plain numbered operation/node menus and cancellation before apply', flush=True)

    failed = folder / 'failed-source'
    shutil.copytree(source, failed)
    patch = failed / 'scripts/patch.py'
    text = patch.read_text()
    assert 'args = parser.parse_args()' in text
    patch.write_text(text.replace('args = parser.parse_args()', "args = parser.parse_args()\n    if args.action == 'apply': raise RuntimeError('Injected installer acceptance failure')"))
    result = subprocess.run(['bash', str(failed / 'start.sh'), 'repair', f"--nodes={env.node('rootless')}", '--yes', '--no-restart'], text=True, capture_output=True, timeout=300)
    (folder / 'failed-apply.log').write_text(result.stdout + result.stderr)
    assert result.returncode != 0 and 'Injected installer acceptance failure' in result.stdout
    assert 'restoring integration' in result.stdout
    assert panels() == baseline and workloads() == original
    assert remote(env.node('rootless'), ['test', '-f', PENDING]).returncode == 0
    result = subprocess.run(['bash', str(source / 'start.sh'), 'finish', f"--nodes={env.node('rootless')}", '--yes'], text=True, capture_output=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    assert workloads() == original
    print('PASS late apply failure restores integration, keeps workloads and defers panel reload for recovery', flush=True)

    lifecycle(source, folder, folder / 'lifecycle.json')
    assert workloads() == original
    assert all(remote(node, ['test', '-f', PENDING]).returncode == 1 for node in NODES)
    shutil.rmtree(failed)
    archive.unlink()
    print('PASS edge acceptance and cleanup; transcripts/journal retained privately: ' + str(folder), flush=True)


if __name__ == '__main__':
    main()
