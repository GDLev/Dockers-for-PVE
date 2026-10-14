#!/usr/bin/env python3
"""Swap application paths after validation while real copy/extraction uses pinned FDs."""
import live_environment as env
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
sys.path.insert(0, '/usr/local/lib/betterdocker')
import runtime
from dctlib import projects as w, archives
from live_firewall import remote


def main():
    code = '''import os,sys,tempfile,tarfile,io,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'/usr/local/lib/betterdocker')
import runtime,guest_state
from dctlib import projects as w, archives
with tempfile.TemporaryDirectory(prefix='bd-pinned-race-',dir='/var/lib/betterdocker') as temporary:
 base=Path(temporary);source=base/'source';source.mkdir();(source/'marker').write_text('retained')
 target=base/'target';target.mkdir();selected=runtime.load();os.chown(target,selected.uid,selected.gid);outside=base/'outside';outside.mkdir();(outside/'marker').write_text('unrelated')
 swapped=False
 def run(args,**kw):
  global swapped
  if not swapped:
   source.rename(base/'saved-source');source.symlink_to(outside,target_is_directory=True)
   target.rename(base/'saved-target');target.symlink_to(outside,target_is_directory=True)
   swapped=True
  return subprocess.run(args,**kw)
 import subprocess
 runtime.restore_data(source,target,runner=run)
 assert (outside/'marker').read_text()=='unrelated'
 assert (base/'saved-target'/'marker').read_text()=='retained'
 # A validated archive must also remain bound to its original input inode.
 tar=base/'archive.tar';folder=base/'restore'
 with tarfile.open(tar,'w') as handle:
  entry=tarfile.TarInfo('manifest.json');entry.size=2;handle.addfile(entry,io.BytesIO(b'{}'))
 with tarfile.open(base/'replacement.tar','w') as handle:
  entry=tarfile.TarInfo('../outside/marker');entry.size=8;handle.addfile(entry,io.BytesIO(b'corrupt!'))
 real_members=archives.members
 def replace_after_scan(fd,capacity):
  result=real_members(fd,capacity);tar.rename(base/'saved.tar');(base/'replacement.tar').rename(tar);return result
 with patch.object(archives,'members',replace_after_scan),patch.object(guest_state,'check_state',return_value={}):
  guest_state.unpack_state(tar,folder)
 assert (folder/'manifest.json').read_text()=='{}'
 assert (outside/'marker').read_text()=='unrelated'
'''
    for node in (env.node('primary'), env.node('rootless')):
        result = remote(node, ['python3', '-c', code])
        assert result.returncode == 0, result.stderr
        print('PASS ' + node + ' path replacements cannot redirect copying or archive extraction', flush=True)


if __name__ == '__main__': main()
