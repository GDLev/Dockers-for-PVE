#!/usr/bin/env python3
"""Small, reversible marked integrations; never restore stale package backups."""
import argparse
import os
import re
import tempfile
from pathlib import Path

NODE_BLOCK = '''# BEGIN BETTERDOCKER
use PVE::API2::BetterDocker;
__PACKAGE__->register_method({ subclass => 'PVE::API2::BetterDocker', path => 'docker' });
# END BETTERDOCKER
'''
CLUSTER_BLOCK = '''# BEGIN BETTERDOCKER
use PVE::API2::BetterDocker;
PVE::API2::BetterDocker::hook_resources();
# END BETTERDOCKER
'''
VMID_BLOCK = '''# BEGIN BETTERDOCKER
    if (-e "/etc/pve/betterdocker/ids/$vmid.json") {
        return undef if $noerr;
        die "DCT $vmid already exists\\n";
    }
# END BETTERDOCKER
'''
UI_BLOCK = '''<!-- BEGIN BETTERDOCKER -->
<link rel="stylesheet" href="/pve2/css/betterdocker.css?v=19.14" />
<link rel="stylesheet" href="/xtermjs/xterm.css" />
<script src="/xtermjs/xterm.js"></script>
<script src="/xtermjs/addon-fit.js"></script>
<script src="/pve2/js/betterdocker-locales/en.js?v=19.14"></script>
<script src="/pve2/js/betterdocker-locales/pl.js?v=19.14"></script>
<script src="/pve2/js/betterdocker.js?v=19.14"></script>
<!-- END BETTERDOCKER -->'''


def transform(text, kind, remove=False):
    block = NODE_BLOCK if kind == 'node' else CLUSTER_BLOCK if kind == 'cluster' else VMID_BLOCK if kind == 'vmid' else UI_BLOCK
    if not remove and block in text:
        return text
    pattern = (r'# BEGIN BETTERDOCKER\n.*?# END BETTERDOCKER\n' if kind in ('node', 'cluster', 'vmid')
               else r'<!-- BEGIN BETTERDOCKER -->\n.*?<!-- END BETTERDOCKER -->\n?')
    text = re.sub(pattern, '', text, flags=re.S)
    if remove:
        return text
    if kind == 'vmid':
        anchor = re.search(r'sub check_vmid_unused \{\s*my \(\$vmid, \$noerr\) = @_;\n', text)
        if not anchor:
            raise RuntimeError('Unsupported Cluster library; refusing ID patch')
        return text[:anchor.end()] + block + text[anchor.end():]
    if kind == 'cluster':
        if "name => 'resources'" not in text or not re.search(r'\n1;\s*$', text):
            raise RuntimeError('Unsupported Cluster.pm; refusing patch')
        return re.sub(r'\n1;\s*$', '\n' + CLUSTER_BLOCK + '\n1;\n', text)
    if kind == 'node':
        if 'use PVE::API2::LXC;' not in text or "path => 'lxc'" not in text:
            raise RuntimeError('Unsupported Nodes.pm; refusing patch')
        anchor = '__PACKAGE__->register_method({'
        if anchor not in text:
            raise RuntimeError('API registration anchor missing')
        return text.replace(anchor, NODE_BLOCK + '\n' + anchor, 1)
    anchor = re.search(r'<script\b[^>]*src=[\'"]/pve2/js/pvemanagerlib\.js[^\'"]*[\'"][^>]*></script>', text)
    if not anchor:
        raise RuntimeError('Unsupported index.html.tpl; refusing patch')
    return text[:anchor.end()] + '\n' + UI_BLOCK + '\n' + text[anchor.end():]


def atomic_write(path, text):
    stat = path.stat()
    fd, tmp = tempfile.mkstemp(prefix='.betterdocker-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(text)
        os.chmod(tmp, stat.st_mode)
        os.chown(tmp, stat.st_uid, stat.st_gid)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check', 'status', 'apply', 'remove'])
    parser.add_argument('--root', default='/')
    args = parser.parse_args()
    root = Path(args.root)
    files = [(root / 'usr/share/perl5/PVE/API2/Nodes.pm', 'node'),
             (root / 'usr/share/pve-manager/index.html.tpl', 'ui'),
             (root / 'usr/share/perl5/PVE/API2/Cluster.pm', 'cluster'),
             (root / 'usr/share/perl5/PVE/Cluster.pm', 'vmid')]
    # Preflight both targets before the first mutation.
    changes = [(path, transform(path.read_text(), kind, args.action == 'remove')) for path, kind in files]
    if args.action == 'status':
        if any(path.read_text() != text for path, text in changes):
            raise RuntimeError('BetterDocker integration requires repair: run start.sh repair')
        print('BetterDocker integration is current')
        return
    if args.action != 'check':
        for path, text in changes:
            atomic_write(path, text)


if __name__ == '__main__':
    main()
