#!/usr/bin/env python3
"""Print hashes of the source and test files used by acceptance checks."""
import json
from pathlib import Path
from acceptance_report import hashes
ROOT=Path(__file__).resolve().parents[1]
files={}
for name in ('assets','frontend','backend','scripts','systemd','packaging','tests','.github'):
    files.update({name+'/'+path:sha for path,sha in hashes(ROOT/name).items()})
import hashlib
for name in ('install.sh','start.sh','.gitlab-ci.yml','requirements-test.txt'):
    files[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
print(json.dumps(files,sort_keys=True))
