"""Preview immutable image changes and update stopped guests with snapshot recovery."""
import json
import re
import time
import uuid


def state(directory):
    path = directory / 'image-update.json'
    return json.loads(path.read_text()) if path.exists() else {}


def overrides(config, directory):
    path = directory / 'images.json'
    for name, saved in (json.loads(path.read_text()) if path.exists() else {}).items():
        image = saved['id'] if isinstance(saved, dict) else saved
        if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise ValueError('Invalid saved image override')
        if name in config['services']:
            config['services'][name]['image'] = image
            config['services'][name]['pull_policy'] = 'never'


def saved_images(directory):
    config = {'services': {}}
    overrides(config, directory)
    path = directory / 'images.json'
    return [value['id'] if isinstance(value, dict) else value for value in (json.loads(path.read_text()) if path.exists() else {}).values()]


def inspect(image):
    from . import projects as w
    return json.loads(w.capture(['docker', 'image', 'inspect', image]))[0]


def preview(identifier, node, changes, digest):
    from . import projects as w
    with w.locked(identifier):
        w.detail(identifier, node)
        w.assert_configuration_digest(identifier, node, digest)
        directory = w.ROOT / identifier
        config = json.loads((directory / 'compose.runtime.json').read_text())
        if not isinstance(changes, dict) or not changes or set(changes) - config['services'].keys():
            raise ValueError('Select existing services to update')
        if state(directory).get('phase') == 'applying':
            raise ValueError('Roll back the recovery snapshot before preparing another update')
        rows = []
        containers = w.snapshot_containers(identifier, allow_running=True)
        for name, image in changes.items():
            if not isinstance(image, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,254}', image):
                raise ValueError('Invalid image reference')
            service = config['services'][name]
            old = sorted({c['Image'] for c in containers if c['Config']['Labels'].get('com.docker.compose.service') == name})
            if not old:
                old = [inspect(service['image'])['Id']]
            command = ['docker', 'pull']
            if service.get('platform'):
                command += ['--platform', service['platform']]
            w.run(command + [image], timeout=1800, capture_output=True)
            candidate = inspect(image)
            rows.append({'service': name, 'image': image, 'current': old, 'candidate': candidate['Id'],
                'digests': candidate.get('RepoDigests') or [], 'changed': old != [candidate['Id']]})
        plan = {'token': uuid.uuid4().hex, 'digest': w.detail(identifier, node)['digest'],
                'created': int(time.time()), 'phase': 'preview', 'changes': rows,
                'containers': sorted(c['Id'] for c in containers)}
        w.write_json(directory / 'image-update.json', plan)
        return plan


def apply(identifier, node, token, recovery, digest):
    from . import projects as w
    with w.locked(identifier):
        info = w.detail(identifier, node)
        w.assert_configuration_digest(identifier, node, digest)
        if info.get('template') or info.get('ha_enrolled'):
            raise ValueError('Clone templates and remove HA management before updating images')
        directory = w.ROOT / identifier
        plan = state(directory)
        if plan.get('phase') != 'preview' or plan.get('token') != token or plan.get('digest') != info['digest']:
            raise ValueError('Image preview is stale; prepare a new preview')
        if time.time() - plan['created'] > 3600:
            raise ValueError('Image preview expired; prepare a new preview')
        records = w.container_records(identifier)
        w.require_stopped(records)
        if sorted(c['id'] for c in records) != plan['containers']:
            raise ValueError('Containers changed since preview; prepare a new preview')
        if w.load_pending(directory) or w.load_pending_network(directory):
            raise ValueError('Apply or revert pending changes before updating images')
        changes = [row for row in plan['changes'] if row['changed']]
        if not changes:
            raise ValueError('Selected images are already current')
        for row in changes:
            if inspect(row['candidate'])['Id'] != row['candidate']:
                raise ValueError('Previewed image is no longer available')
        w.snapshot_name(recovery)
        w.project_snapshot(identifier, node, 'create', recovery, 'Image update recovery')
        plan.update(phase='applying', recovery=recovery)
        w.write_json(directory / 'image-update.json', plan)
        config = json.loads((directory / 'compose.runtime.json').read_text())
        path = directory / 'images.json'
        saved = json.loads(path.read_text()) if path.exists() else {}
        for row in changes:
            saved[row['service']] = {'id': row['candidate'], 'reference': row['image']}
        w.write_json(path, saved)
        overrides(config, directory)
        try:
            w.write_json(directory / 'compose.runtime.json', config)
            w.run(w.compose_base(directory, identifier) + ['up', '--no-start', '--no-deps', '--force-recreate',
                *[row['service'] for row in changes]], timeout=1800)
            (directory / 'snapshot-restored.json').unlink(missing_ok=True)
        except Exception:
            # Recovery is explicit, through the existing native snapshot rollback action.
            # Keep its name durable when the worker fails or is killed.
            raise ValueError('Image update failed; roll back snapshot ' + recovery) from None
        plan['phase'] = 'complete'
        w.write_json(directory / 'image-update.json', plan)
        w.refresh(node)
        print('Images updated. DCT remains stopped. Recovery snapshot: ' + recovery)
