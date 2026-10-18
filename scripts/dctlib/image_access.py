"""Keep private writable-layer images inside their authorized guest or archive."""
import json
import os
import re


def reference(image, identifier):
    from . import projects as w
    if not isinstance(image, str) or not image:
        raise ValueError('Invalid image reference')
    if w.trusted_management():
        return
    result = w.run(['docker', 'image', 'inspect', image], check=False, capture_output=True)
    if result.returncode:
        if 'no such image' in (result.stderr or '').lower():
            return
        raise ValueError('Cannot verify private image ownership')
    value = json.loads(result.stdout)[0]
    owner = (value.get('Config', {}).get('Labels') or {}).get('betterdocker.id')
    if not owner or owner == identifier:
        return
    context = json.loads(os.environ.get('BETTERDOCKER_REQUEST_CONTEXT', '{}'))
    if context.get('id') == owner and context.get('destination_id') == identifier:
        from .authorization import check_context
        check_context(owner, w.detail(owner, context['node']))
        return
    rows = w.container_records(identifier)
    if rows:
        actual = json.loads(w.capture(['docker', 'inspect', *[r['id'] for r in rows]]))
        if any(r['Image'] == value['Id'] and (r['Config'].get('Labels') or {}).get('betterdocker.id') == identifier for r in actual):
            return
    raise ValueError('Private images require access through their guest clone or backup')


def loaded(result, containers):
    from . import projects as w
    tags = {match.group(1) for line in result.stdout.splitlines()
            if (match := re.fullmatch(r'Loaded image(?: ID)?: (\S+)', line))}
    for container in containers:
        tag = container['SnapshotTag']
        if tag not in tags:
            raise ValueError('Backup references a private image not provided by its archive')
        identity = w.capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag]).strip()
        if identity != container['SnapshotImage']:
            raise ValueError('Backup image identity does not match its archive')
