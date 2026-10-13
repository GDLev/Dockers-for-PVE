"""Bind queued API work to the guest identity authorized before the fork."""
import json
import os


def require_host_root(arguments):
    if os.geteuid() != 0 and arguments not in (['--help'], ['-h']):
        raise ValueError('This PVE helper requires host root privileges')


def check_context(identifier, info):
    from . import projects as w
    selected = w.node_runtime.load().mode
    marker = w.ROOT / identifier / 'runtime-owner.json'
    saved = json.loads(marker.read_text()) if marker.exists() else {}
    if saved and (saved.get('id') != identifier or saved.get('mode') != selected):
        raise ValueError('Retained DCT data belongs to another runtime')
    if info.get('runtime_mode') and info['runtime_mode'] != selected:
        raise ValueError('DCT runtime changed; refuse to use another Docker daemon')
    raw = os.environ.get('BETTERDOCKER_REQUEST_CONTEXT')
    if not raw:
        return  # Root-only node services and native backup helpers have no API caller.
    context = json.loads(raw)
    if context['id'] != identifier:
        if context.get('destination_id') != identifier:
            raise ValueError('Request was authorized for another DCT')
        owner = json.loads((w.METADATA / 'ids' / (str(context['destination_vmid']) + '.json')).read_text())
        if owner.get('id') != identifier or owner.get('node') != w.current_node():
            raise ValueError('Clone destination ownership changed')
        return
    if context['node'] != w.current_node() or not info:
        raise ValueError('DCT ownership changed after authorization')
    if any(context.get(key) is not None and context[key] != info.get(key) for key in ('node', 'vmid', 'runtime_mode')):
        raise ValueError('DCT ownership or runtime changed after authorization')
    if context.get('vmid') is not None:
        owner = json.loads((w.METADATA / 'ids' / (str(context['vmid']) + '.json')).read_text())
        if owner.get('id') != identifier or owner.get('node') != context['node']:
            raise ValueError('DCT guest ID ownership changed after authorization')


def remember_owner(identifier):
    from . import projects as w
    directory = w.ROOT / w.project_id(identifier)
    if not directory.is_dir():
        raise ValueError('Create the DCT project directory before recording its runtime')
    w.write_json(directory / 'runtime-owner.json', {'id': identifier, 'mode': w.node_runtime.load().mode})

