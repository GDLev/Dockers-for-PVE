"""Adding HA inventory consumers must not hide DCTs from replication consumers."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InventoryTests(unittest.TestCase):
    def test_scoped_inventory_consumers_share_one_dispatcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for module in ('Cluster', 'Storage', 'APIClient/LWP', 'AccessControl', 'Storage/PBSPlugin', 'API2/Storage/PruneBackups'):
                path = root / ('PVE/' + module + '.pm'); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('package PVE::' + module.replace('/', '::') + '; 1;')
            path = root / 'JSON.pm'
            path.write_text("package JSON; use Exporter 'import'; our @EXPORT_OK=qw(decode_json); sub decode_json {} 1;")
            subprocess.run(['perl', '-I' + temporary, '-I' + str(ROOT / 'backend'), '-e', r'''
package PVE::Cluster;
sub get_vmlist { return { ids => {103 => { type => 'lxc' }} }; }
package main;
require PVE::BetterDocker::Backup;
{ no warnings 'redefine'; *PVE::BetterDocker::Backup::inventory = sub {
    my ($native)=@_; return {ids=>{%{$native->{ids}},115=>{type=>'docker'}}};
}; }
PVE::BetterDocker::Backup::hook_inventory('AllowedReplication');
package AllowedReplication;
sub list { PVE::Cluster::get_vmlist() }
package AllowedHA;
sub list { PVE::Cluster::get_vmlist() }
package main;
die 'replication hidden' unless AllowedReplication::list()->{ids}->{115};
die 'HA visible before opt-in' if AllowedHA::list()->{ids}->{115};
PVE::BetterDocker::Backup::hook_inventory('AllowedHA');
PVE::BetterDocker::Backup::hook_inventory('AllowedReplication');
die 'replication lost after HA registration' unless AllowedReplication::list()->{ids}->{115};
die 'HA not registered' unless AllowedHA::list()->{ids}->{115};
die 'unrelated caller changed' if PVE::Cluster::get_vmlist()->{ids}->{115};
die 'native guest lost' unless AllowedHA::list()->{ids}->{103}->{type} eq 'lxc';
'''], check=True)
