"""Acceptance evidence and exact fixture ownership, separate from test execution."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid


def hashes(root):
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc' and p.name != '.DS_Store'}


class Report:
    def __init__(self, path, revision, source_hashes):
        self.path = Path(path)
        self.data = dict(format=1, run=uuid.uuid4().hex, revision=revision, source_hashes=source_hashes,
                         started=time.time(), checks=[], fixtures=[], environments={}, baseline={})
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def check(self, name, status, evidence):
        if status not in ('pass', 'fail', 'not_tested'):
            raise ValueError('Invalid acceptance result')
        self.data['checks'].append(dict(name=name, status=status, evidence=evidence))
        self.save()
        print(status.upper(), name, flush=True)

    def plan_guest(self, node, vmid, name):
        if any(item['vmid'] == vmid or item['name'] == name for item in self.data['fixtures']):
            raise ValueError('Fixture already registered')
        item = dict(node=node, vmid=vmid, name=name, id=None, removed=False)
        self.data['fixtures'].append(item); self.save()
        return item

    def confirm_guest(self, item, info):
        if any(info.get(key) != item[key] for key in ('node', 'vmid', 'name')):
            raise ValueError('Fixture identity differs from its registered allocation')
        identifier = info.get('id', '')
        if len(identifier) != 32 or any(char not in '0123456789abcdef' for char in identifier):
            raise ValueError('Invalid fixture identifier')
        if item['id'] is not None and item['id'] != identifier:
            raise ValueError('Fixture identifier changed')
        item['id'] = identifier; self.save()

    def finish(self):
        self.data['finished'] = time.time()
        failed = any(c['status'] == 'fail' for c in self.data['checks']) or any(not item['removed'] for item in self.data['fixtures'])
        incomplete = not self.data['checks'] or any(c['status'] == 'not_tested' for c in self.data['checks'])
        self.data['status'] = 'failed' if failed else 'partial' if incomplete else 'passed'
        self.save()
