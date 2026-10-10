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
PVE::API2::BetterDocker::hook_native_node_operations();
# lifecycle integration 2
# END BETTERDOCKER
'''
CLUSTER_BLOCK = '''# BEGIN BETTERDOCKER
use PVE::API2::BetterDocker;
PVE::API2::BetterDocker::hook_resources();
PVE::API2::BetterDocker::hook_pools();
PVE::API2::BetterDocker::hook_native_cluster_operations();
# END BETTERDOCKER
'''
VMID_BLOCK = '''# BEGIN BETTERDOCKER
    if (-e "/etc/pve/betterdocker/ids/$vmid.json") {
        return undef if $noerr;
        die "DCT $vmid already exists\\n";
    }
# END BETTERDOCKER
'''
VZDUMP_BLOCK = """# BEGIN BETTERDOCKER
require PVE::BetterDocker::Backup;
PVE::BetterDocker::Backup::hook_storage();
push @pve_vzdump_classes, 'PVE::VZDump::Docker';
# END BETTERDOCKER
"""
VZDUMP_INVENTORY = """# BEGIN BETTERDOCKER
    $vmlist = PVE::BetterDocker::Backup::inventory($vmlist);
# END BETTERDOCKER
"""
VZDUMP_PBS = """# BEGIN BETTERDOCKER
            } elsif ($vmtype eq 'docker') {
                $pbs_group_name = "host/dct-$vmid";
# END BETTERDOCKER
"""
UI_BLOCK = '''<!-- BEGIN BETTERDOCKER -->
<link rel="stylesheet" href="/pve2/css/betterdocker.css?v=19.78" />
<link rel="stylesheet" href="/xtermjs/xterm.css" />
<script src="/xtermjs/xterm.js"></script>
<script src="/xtermjs/addon-fit.js"></script>
<script src="/pve2/js/betterdocker-locales/en.js?v=19.78"></script>
<script src="/pve2/js/betterdocker-locales/pl.js?v=19.78"></script>
<script src="/pve2/js/betterdocker.js?v=19.78"></script>
<!-- END BETTERDOCKER -->'''


NODE_LIFECYCLE = [
    ("    return $res;\n}\n\n# return all VMs", "    PVE::API2::BetterDocker::append_migration_list($res, $nodename, $vmfilter) if $templates;\n"),
    ('    } else {\n        die "unknown VM type \'$type\'\\n";\n    }\n\n    my $task = PVE::Tools::upid_decode($upid);', "    } elsif ($type eq 'docker') {\n        $upid = PVE::API2::BetterDocker::node_migrate($vmid, $nodename, $target);\n"),
    ("    return $resList;\n}", "    PVE::API2::BetterDocker::append_lifecycle_list($resList, $nodename, $autostart, $vmfilter);\n"),
    ("                        } else {\n                            die \"unknown VM type '$d->{type}'\\n\";", "                        } elsif ($d->{type} eq 'docker') {\n                            print STDERR \"Starting DCT $vmid\\n\";\n                            $upid = PVE::API2::BetterDocker::node_start($vmid, $nodename);\n                            next if !$upid;\n"),
    ('    } else {\n        die "unknown VM type \'$type\'\\n";\n    }\n};\n\n__PACKAGE__->register_method({\n    name => \'stopall\',', "    } elsif ($type eq 'docker') {\n        return PVE::API2::BetterDocker::node_stop($vmid, $nodename, $timeout, $force_stop);\n"),
]


# Extend the existing backup screens, preserving native controls and layout.
MANAGER_RULES = [
    ("        } else if (me.guestType === 'qemu') {\n            type = 'VM';\n        }",
     "        } else if (me.guestType === 'docker') {\n            type = 'DCT';\n", 'guestid'),
    ('        } else {\n            throw "unsupported VM type \'" + vmtype + "\'";\n',
     "        } else if (vmtype === 'docker') {\n            vmtypeFilter = item => PVE.Utils.volume_is_dct_backup(item.data);\n", False),
    ("            handler: function (b, e, rec) {\n                let win = Ext.create('PVE.window.Restore', {",
     "                if (vmtype === 'docker') {\n                    const win = Ext.create('PVE.betterdocker.StateRestore', { nodename, archive: rec.data.volid, guestname: vmname });\n                    win.on('destroy', reload);\n                    win.show();\n                    return;\n                }\n", 'handler'),
    ("                handler: function (b, e, rec) {\n                    let vmtype;\n                    if (PVE.Utils.volume_is_qemu_backup(rec.data)) {",
     "                    if (PVE.Utils.volume_is_dct_backup(rec.data)) {\n                        Ext.create('PVE.betterdocker.StateRestore', { autoShow: true, nodename, archive: rec.data.volid,\n                            listeners: { destroy: () => me.store.load() } });\n                        return;\n                    }\n", 'handler'),
    ("                    if (name.startsWith('vzdump-lxc-') || format === 'pbs-ct') {",
     "                    if (PVE.Utils.volume_is_dct_backup(backup)) {\n                        this.setText(gettext('Prune group') + ` docker/${vmid}`);\n                        this.vmid = vmid;\n                        this.vmtype = 'docker';\n                        this.setDisabled(false);\n                        return;\n                    }\n", False),
    ("        let guestTypeStr = me.vmtype === 'lxc' ? 'CT' : 'VM';\n",
     "        if (me.vmtype === 'docker') { guestTypeStr = 'DCT'; }\n", True),
]


def manager_transform(text, remove=False):
    text = re.sub(r'// BEGIN BETTERDOCKER\n.*?// END BETTERDOCKER\n', '', text, flags=re.S)
    text = re.sub(r' /\* BEGIN BETTERDOCKER \*/.*?/\* END BETTERDOCKER \*/', '', text, flags=re.S)
    if remove:
        return text
    for anchor, addition, position in MANAGER_RULES:
        if text.count(anchor) != 1:
            raise RuntimeError('Unsupported native backup UI; refusing patch')
        marked = '// BEGIN BETTERDOCKER\n' + addition + '// END BETTERDOCKER\n'
        if position == 'guestid':
            prefix, closing = anchor.rsplit('\n', 1)
            text = text.replace(anchor, prefix + '\n' + marked + closing, 1)
        elif position == 'handler':
            first, remainder = anchor.split('\n', 1)
            text = text.replace(anchor, first + '\n' + marked + remainder, 1)
        elif position:
            text = text.replace(anchor, anchor + marked, 1)
        else:
            text = text.replace(anchor, marked + anchor, 1)
    prune = "        if (me.backup_type !== 'qemu' && me.backup_type !== 'lxc'"
    if text.count(prune) != 1:
        raise RuntimeError('Unsupported native prune dialog; refusing patch')
    text = text.replace(prune, prune + " /* BEGIN BETTERDOCKER */ && me.backup_type !== 'docker' /* END BETTERDOCKER */", 1)
    anchor = "                        if (me.vmtype === 'lxc'"
    if text.count(anchor) != 1:
        raise RuntimeError('Unsupported native PBS backup controls; refusing patch')
    text = text.replace(anchor, anchor + " /* BEGIN BETTERDOCKER */ || me.vmtype === 'docker' /* END BETTERDOCKER */", 1)
    anchor = "res[1] !== 'vm' && res[1] !== 'ct'"
    if text.count(anchor) != 3:
        raise RuntimeError('Unsupported native HA editor; refusing patch')
    text = text.replace(anchor, anchor + " /* BEGIN BETTERDOCKER */ && res[1] !== 'docker' /* END BETTERDOCKER */")
    for anchor in ("(data.type === 'lxc' || data.type === 'qemu') && data.pool !== me.pool;",
                   "rec.data.type === 'openvz'\n                )"):
        if text.count(anchor) != 1:
            raise RuntimeError('Unsupported native pool member controls; refusing patch')
        addition = " /* BEGIN BETTERDOCKER */ || data.type === 'docker' /* END BETTERDOCKER */" if anchor.startswith('(') else " /* BEGIN BETTERDOCKER */ || rec.data.type === 'docker' /* END BETTERDOCKER */"
        if anchor.startswith('('):
            replacement = anchor.replace("'qemu')", "'qemu'" + addition + ")")
        else:
            replacement = anchor.replace("'openvz'", "'openvz'" + addition)
        text = text.replace(anchor, replacement, 1)
    start = text.index("Ext.define('PVE.ha.VMResourceInputPanel'")
    end = text.index("Ext.define('PVE.ha.VMResourceEdit'", start)
    panel = text[start:end]
    anchor = 'value: /lxc|qemu/,'
    if panel.count(anchor) != 1:
        raise RuntimeError('Unsupported native HA guest selector; refusing patch')
    panel = panel.replace(anchor, "value:  /* BEGIN BETTERDOCKER */ new RegExp( /* END BETTERDOCKER *//lxc|qemu/ /* BEGIN BETTERDOCKER */ .source + '|docker') /* END BETTERDOCKER */,", 1)
    anchor = "fieldLabel: me.vmid && me.guestType === 'ct' ? 'CT' : 'VM',"
    if panel.count(anchor) != 1:
        raise RuntimeError('Unsupported native HA guest field; refusing patch')
    panel = panel.replace(anchor, "fieldLabel:  /* BEGIN BETTERDOCKER */ me.guestType === 'docker' ? 'DCT' : /* END BETTERDOCKER */me.vmid && me.guestType === 'ct' ? 'CT' : 'VM',", 1)
    return text[:start] + panel + text[end:]



def transform(text, kind, remove=False):
    if kind == 'manager':
        return manager_transform(text, remove)
    if kind in ('ha-env', 'ha-api'):
        text = re.sub(r'# BEGIN BETTERDOCKER\n.*?# END BETTERDOCKER\n', '', text, flags=re.S)
        if remove:
            return text
        if kind == 'ha-env':
            anchor = 'PVE::HA::Resources->init();'
            if text.count(anchor) != 1 or not re.search(r'\n1;\s*$', text):
                raise RuntimeError('Unsupported HA environment; refusing patch')
            register = '# BEGIN BETTERDOCKER\nrequire PVE::HA::Resources::Docker;\nPVE::HA::Resources::Docker->register();\n# END BETTERDOCKER\n'
            text = text.replace(anchor, register + anchor, 1)
            hook = '# BEGIN BETTERDOCKER\nPVE::HA::Resources::Docker::hook();\n# END BETTERDOCKER\n'
            return re.sub(r'\n1;\s*$', lambda _: '\n' + hook + '1;\n', text)
        anchor = 'my $resource_type_enum = PVE::HA::Resources->lookup_types();'
        if text.count(anchor) != 1:
            raise RuntimeError('Unsupported HA API; refusing patch')
        return text.replace(anchor, '# BEGIN BETTERDOCKER\nuse PVE::HA::Env::PVE2;\n# END BETTERDOCKER\n' + anchor, 1)
    block = {'node': NODE_BLOCK, 'cluster': CLUSTER_BLOCK, 'vmid': VMID_BLOCK,
             'vzdump': VZDUMP_BLOCK, 'ui': UI_BLOCK,
             'replication-core': '# BEGIN BETTERDOCKER\nrequire PVE::BetterDocker::Replication;\nPVE::BetterDocker::Replication::hook_core();\n# END BETTERDOCKER\n',
             'replication-api': '# BEGIN BETTERDOCKER\nrequire PVE::BetterDocker::Replication;\nPVE::BetterDocker::Replication::hook_api();\n# END BETTERDOCKER\n'}[kind]
    if not remove and block in text and (kind != 'node' or all(addition in text for _, addition in NODE_LIFECYCLE)) and (kind != 'vzdump' or (text.count(VZDUMP_INVENTORY) == 2 and VZDUMP_PBS in text)):
        return text
    pattern = (r'# BEGIN BETTERDOCKER\n.*?# END BETTERDOCKER\n' if kind != 'ui'
               else r'\n?<!-- BEGIN BETTERDOCKER -->\n.*?<!-- END BETTERDOCKER -->\n?')
    # Earlier versions added a blank line outside these marked blocks. Remove
    # it together with the block; new insertions own no extra whitespace.
    if kind == 'node':
        text = re.sub(r'# BEGIN BETTERDOCKER\nuse PVE::API2::BetterDocker;\n.*?# END BETTERDOCKER\n\n(?=__PACKAGE__->register_method\(\{)', '', text, flags=re.S)
    if kind in ('cluster', 'replication-core', 'replication-api'):
        text = re.sub(r'# BEGIN BETTERDOCKER\n.*?# END BETTERDOCKER\n\n(?=1;)', '', text, flags=re.S)
    text = re.sub(pattern, '', text, flags=re.S)
    if remove:
        return text
    if kind in ('replication-core', 'replication-api'):
        expected = 'sub replicate {' if kind == 'replication-core' else 'our $lookup_guest_class = sub {'
        if expected not in text or not re.search(r'\n1;\s*$', text):
            raise RuntimeError('Unsupported replication library; refusing patch')
        return re.sub(r'\n(?=1;\s*$)', lambda _: '\n' + block, text)
    if kind == 'vzdump':
        plugins = re.search(r'my @pve_vzdump_classes = qw\([^;]+\);\n', text)
        inventory = '    my $vmlist = PVE::Cluster::get_vmlist();\n'
        pbs = '            } else {\n                die "pbs backup not implemented for plugin type'
        if not plugins or text.count(inventory) != 2 or text.count(pbs) != 1:
            raise RuntimeError('Unsupported VZDump library; refusing backup patch')
        text = text[:plugins.end()] + block + text[plugins.end():]
        text = text.replace(inventory, inventory + VZDUMP_INVENTORY)
        return text.replace(pbs, VZDUMP_PBS + pbs, 1)
    if kind == 'vmid':
        anchor = re.search(r'sub check_vmid_unused \{\s*my \(\$vmid, \$noerr\) = @_;\n', text)
        if not anchor:
            raise RuntimeError('Unsupported Cluster library; refusing ID patch')
        return text[:anchor.end()] + block + text[anchor.end():]
    if kind == 'cluster':
        if "name => 'resources'" not in text or not re.search(r'\n1;\s*$', text):
            raise RuntimeError('Unsupported Cluster.pm; refusing patch')
        return re.sub(r'\n(?=1;\s*$)', lambda _: '\n' + CLUSTER_BLOCK, text)
    if kind == 'node':
        if 'use PVE::API2::LXC;' not in text or "path => 'lxc'" not in text:
            raise RuntimeError('Unsupported Nodes.pm; refusing patch')
        anchor = '__PACKAGE__->register_method({'
        if anchor not in text:
            raise RuntimeError('API registration anchor missing')
        text = text.replace(anchor, NODE_BLOCK + anchor, 1)
        for target, addition in NODE_LIFECYCLE:
            if text.count(target) != 1:
                raise RuntimeError('Unsupported Nodes.pm lifecycle; refusing patch')
            text = text.replace(target, '# BEGIN BETTERDOCKER\n' + addition + '# END BETTERDOCKER\n' + target, 1)
        return text
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
             (root / 'usr/share/perl5/PVE/Cluster.pm', 'vmid'),
             (root / 'usr/share/perl5/PVE/VZDump.pm', 'vzdump'),
             (root / 'usr/share/perl5/PVE/Replication.pm', 'replication-core'),
             (root / 'usr/share/perl5/PVE/API2/Replication.pm', 'replication-api'),
             (root / 'usr/share/perl5/PVE/HA/Env/PVE2.pm', 'ha-env'),
             (root / 'usr/share/perl5/PVE/API2/HA/Resources.pm', 'ha-api'),
             (root / 'usr/share/pve-manager/js/pvemanagerlib.js', 'manager')]
    # Preflight all targets before the first mutation.
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
