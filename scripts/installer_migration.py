"""Runtime conversion from the installer, using each node's installed migrator."""
import json
import re
from pathlib import Path

from installer_ui import Cancelled, Choice

MIGRATOR = '/usr/local/lib/betterdocker/migrate-runtime.py'


def migrate(runner, nodes, target=None, interactive=True):
    ui = runner.ui
    modes = {node['mode'] for node in nodes}
    if len(modes) != 1 or not modes <= {'rootful', 'rootless'}:
        raise ValueError('Select nodes with the same configured runtime mode.')
    if any(not node['installed'] or node['pending'] for node in nodes):
        raise ValueError('Migration requires an installed extension with no pending panel/API reload. Finish installation first.')
    source = next(iter(modes))
    if target is None and interactive:
        target = ui.choose('Choose the target runtime', [
            Choice(mode, mode + (' (current)' if mode == source else ''), ui.t(mode + '_description'), mode != source)
            for mode in ('rootful', 'rootless')], ['rootless' if source == 'rootful' else 'rootful'])[0]
    if target not in ('rootful', 'rootless') or target == source:
        raise ValueError('Choose a different target with --runtime=rootful or --runtime=rootless.')
    ui.output('  Migrate: ' + source + ' -> ' + target)
    ui.output('  Back up all DCTs first. All guests must be stopped and remain stopped after migration.')
    ui.output('  Snapshot history, HA, replication, unsupported mounts and networks block conversion; nothing is removed automatically.')
    blocked = False
    for node in nodes:
        if runner.interrupted:
            raise Cancelled()
        result = runner.process(node['node'], [MIGRATOR, '--runtime=' + target], 'preflight')
        ui.output('  ' + node['node'] + '  Log: ' + str(result['log']))
        if result['code']:
            blocked = True
            continue
        report = json.loads(Path(result['log']).read_text())
        if report.get('source') != source or report.get('target') != target:
            raise ValueError(node['node'] + ': runtime changed during preflight. Start again.')
        ui.output('  ' + node['node'] + ': ' + str(len(report['guests'])) + ' DCT(s)')
        for reason in report['blockers']:
            ui.output('    Blocked: ' + reason)
            blocked = True
    if blocked:
        raise ValueError('Migration checks failed. No runtime changes applied. Resolve the blockers and try again.')
    if runner.interrupted:
        raise Cancelled()
    if interactive:
        ui.confirm('Press Enter to migrate the selected nodes. DCTs will remain stopped. Esc cancels without changes.')
    if runner.interrupted:
        raise Cancelled()
    ui.output('  Cancellation finishes the current node and skips the remaining nodes. Retained source data must not be deleted.')
    for node in nodes:
        if runner.interrupted:
            break
        name = node['node']
        result = runner.process(name, [MIGRATOR, '--runtime=' + target, '--apply'], 'apply')
        runner.results.append(result)
        ui.output('  ' + name + ': ' + ui.t('failed' if result['code'] else 'done'))
        ui.output('    Log: ' + str(result['log']))
        journal = None
        for line in Path(result['log']).read_text(errors='replace').splitlines():
            try:
                receipt = json.loads(line)
            except ValueError:
                continue
            token = receipt.get('journal') if isinstance(receipt, dict) else None
            if isinstance(token, str) and re.fullmatch(r'[a-f0-9]{32}', token):
                journal = token
        if journal:
            ui.output('    Recovery journal on ' + name + ': /var/lib/betterdocker/runtime-migrations/' + journal)
            if result['code']:
                ui.output('    Resume on ' + name + ': ' + MIGRATOR + ' --runtime=' + target + ' --resume ' + journal + ' --apply')
            ui.output('    Rollback on ' + name + ': ' + MIGRATOR + ' --rollback ' + journal + ' --apply')
            ui.output('    Rollback discards changes made in the target runtime after conversion. Back up those changes first.')
        elif result['code']:
            ui.output('    Check /var/lib/betterdocker/runtime-migrations on ' + name + ' for the recovery journal before retrying.')
        if result['code']:
            ui.output('  Remaining nodes were not migrated. Fix or recover this node before continuing.')
            break
    return 130 if runner.interrupted else int(any(item['code'] for item in runner.results))
