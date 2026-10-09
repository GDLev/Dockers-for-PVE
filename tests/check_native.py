#!/usr/bin/env python3
"""Check patch compatibility against a saved PVE filesystem: check_native.py ROOT."""
import importlib.util
from pathlib import Path
import sys
spec=importlib.util.spec_from_file_location('patcher',Path(__file__).resolve().parents[1]/'scripts/patch.py')
patcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(patcher)
root=Path(sys.argv[1])
files={
 'usr/share/perl5/PVE/API2/Nodes.pm':'node',
 'usr/share/perl5/PVE/API2/Cluster.pm':'cluster',
 'usr/share/perl5/PVE/Cluster.pm':'vmid',
 'usr/share/perl5/PVE/VZDump.pm':'vzdump',
 'usr/share/perl5/PVE/Replication.pm':'replication-core',
 'usr/share/perl5/PVE/API2/Replication.pm':'replication-api',
 'usr/share/perl5/PVE/HA/Env/PVE2.pm':'ha-env',
 'usr/share/perl5/PVE/API2/HA/Resources.pm':'ha-api',
 'usr/share/pve-manager/index.html.tpl':'ui',
 'usr/share/pve-manager/js/pvemanagerlib.js':'manager',
}
for path,kind in files.items():
 text=(root/path).read_text()
 clean=patcher.transform(text,kind,remove=True)
 applied=patcher.transform(clean,kind)
 assert patcher.transform(applied,kind)==applied,(kind,'not idempotent')
 assert patcher.transform(applied,kind,remove=True)==clean,(kind,'not reversible')
 print('PASS',kind)
