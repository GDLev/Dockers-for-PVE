"""Authenticated PVE requests execute helper arguments under Perl taint mode."""
import re
import os
import json
import tempfile
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PerlHelperTests(unittest.TestCase):
    def test_ha_and_replication_helper_arguments_are_validated_and_untainted(self):
        for filename, call in (
            ('backend/PVE/HA/Resources/Docker.pm', "helper({node=>$ARGV[1]},'enroll',$ARGV[0],$ARGV[1]);"),
            ('backend/PVE/BetterDocker/Replication.pm', "helper($ARGV[1],'prepare',$ARGV[0],$ARGV[1],'--lock-fd',3);"),
        ):
            source = (ROOT / filename).read_text()
            helper = re.search(r'^sub helper \{.*?^\}', source, re.M | re.S).group()
            program = r'''
use strict; use warnings; use Scalar::Util;
package PVE::INotify; sub nodename { 'pve' }
package PVE::Tools;
sub run_command {
 die 'tainted helper command' if grep { Scalar::Util::tainted($_) } @{$_[0]};
 $main::executed++;
}
package main;
our $executed=0; my $helper='/usr/local/lib/betterdocker/replica.py';
''' + helper + call + "die 'helper not executed' unless $executed;"
            with self.subTest(filename=filename):
                subprocess.run(['perl', '-T', '-e', program, 'a' * 32, 'pve'], check=True)
                failed = subprocess.run(['perl', '-T', '-e', program, 'unsafe; argument', 'pve'], capture_output=True, text=True)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('Invalid DCT helper argument', failed.stderr)

    def test_guest_lookup_uses_canonical_metadata_instead_of_inventory(self):
        source = (ROOT / 'backend/PVE/API2/BetterDocker.pm').read_text()
        functions = []
        for name in ('worker_id', 'read_guest', 'task_id', 'request_context'):
            match = re.search(r'^sub ' + name + r' \{.*?^\}', source, re.M | re.S)
            if match:
                functions.append(match.group().replace('/etc/pve/betterdocker/', '$directory/'))
            elif name != 'read_guest':
                self.fail('Missing native helper: ' + name)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ('a' * 32 + '.json')
            path.write_text(json.dumps({'id': 'a' * 32, 'node': 'pve', 'vmid': 123, 'runtime_mode': 'rootless'}))
            program = r"""
use strict;use warnings;use JSON::PP qw(decode_json);use Scalar::Util;use Errno qw(ENOENT);
package JSON;sub new { JSON::PP->new }
package main;
my $directory=shift @ARGV;$directory =~ /\A([A-Za-z0-9_\/.-]+)\z/ or die; $directory=$1;
sub PVE::Cluster::get_config {
 $_[0] =~ /\Abetterdocker\/([0-9a-f]{32}\.json)\z/ or die 'Invalid IPC path';
 open(my $fh,'<',"$directory/$1") or do { return undef if $! == ENOENT; die $!; };
 local $/;return <$fh>;
}
our $inventory_reads=0;sub summaries { $inventory_reads++; [] }
FUNCTIONS
my $id=$ARGV[0];my $vmid=task_id($id,'pve');
die 'Wrong guest ID' unless $vmid==123;
die 'Tainted worker ID' if Scalar::Util::tainted($vmid);
my $context=decode_json(request_context($id,'pve'));
die 'Wrong ownership context' unless $context->{id} eq $id && $context->{node} eq 'pve' && $context->{vmid}==123 && $context->{runtime_mode} eq 'rootless';
die 'Global inventory used for a known guest' if $inventory_reads;
print 'PASS';
""".replace('FUNCTIONS', '\n'.join(functions))
            result = subprocess.run(['perl', '-T', '-e', program, folder, 'a' * 32], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'PASS')
            for metadata in (
                {'id': 'b' * 32, 'node': 'pve', 'vmid': 123},
                {'id': 'a' * 32, 'node': 'other', 'vmid': 123},
                [],
            ):
                with self.subTest(metadata=metadata):
                    path.write_text(json.dumps(metadata))
                    rejected = subprocess.run(['perl', '-T', '-e', program, folder, 'a' * 32], capture_output=True, text=True)
                    self.assertNotEqual(rejected.returncode, 0)
            path.unlink()
            missing = subprocess.run(['perl', '-T', '-e', program, folder, 'a' * 32], capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('Docker project does not exist', missing.stderr)

    def test_metadata_read_errors_are_not_reported_as_missing_ownership(self):
        source = (ROOT / 'backend/PVE/API2/BetterDocker.pm').read_text()
        function = re.search(r'^sub read_guest \{.*?^\}', source, re.M | re.S).group()
        function = function.replace('/etc/pve/betterdocker/', '$directory/')
        with tempfile.TemporaryDirectory() as folder:
            os.chmod(folder, 0o755)
            path = Path(folder) / ('a' * 32 + '.json')
            path.write_text(json.dumps({'id': 'a' * 32, 'node': 'pve', 'vmid': 123}))
            path.chmod(0)
            program = r"""
use strict;use warnings;use JSON::PP qw(decode_json);use Errno qw(ENOENT);
my $directory=shift @ARGV;
$directory =~ /\A([A-Za-z0-9_\/.-]+)\z/ or die; $directory=$1;
sub PVE::Cluster::get_config {
 $_[0] =~ /\Abetterdocker\/([0-9a-f]{32}\.json)\z/ or die 'Invalid IPC path';
 open(my $fh,'<',"$directory/$1") or die $!;
 local $/;return <$fh>;
}
FUNCTION
print defined(read_guest($ARGV[0],'pve')) ? 'FOUND' : 'MISSING';
""".replace('FUNCTION', function)
            identity = dict(user=65534, group=65534, extra_groups=[]) if os.geteuid() == 0 else {}
            try:
                result = subprocess.run(['perl', '-T', '-e', program, folder, 'a' * 32],
                                        capture_output=True, text=True, **identity)
                self.assertNotEqual(result.returncode, 0, 'Unreadable metadata was treated as absent')
                self.assertIn('Cannot read DCT metadata', result.stderr)
            finally:
                path.chmod(0o600)

    def test_cluster_ipc_preserves_ownership_checks_and_real_absence(self):
        source = (ROOT / 'backend/PVE/API2/BetterDocker.pm').read_text()
        function = re.search(r'^sub read_guest \{.*?^\}', source, re.M | re.S).group()
        function = function.replace('/etc/pve/betterdocker/', '$directory/')
        with tempfile.TemporaryDirectory() as folder:
            program = r'''
use strict;use warnings;use JSON::PP qw(decode_json);use Errno qw(ENOENT);
package PVE::Cluster;
sub get_config {
 die 'Unexpected IPC path' unless $_[0] eq 'betterdocker/'.('a' x 32).'.json';
 return $main::content;
}
package main;
my $directory=shift @ARGV;
$directory =~ /\A([A-Za-z0-9_\/.-]+)\z/ or die; $directory=$1;
our $content='{"id":"'.('a' x 32).'","node":"pve","vmid":123}';
FUNCTION
die 'Existing metadata missing' unless read_guest('a' x 32,'pve')->{vmid}==123;
die 'Wrong node authorized' if defined(read_guest('a' x 32,'other'));
$content='{"id":"'.('b' x 32).'","node":"pve","vmid":123}';
die 'Wrong guest authorized' if defined(read_guest('a' x 32,'pve'));
$content=undef;
die 'Missing metadata authorized' if defined(read_guest('a' x 32,'pve'));
print 'PASS';
'''.replace('FUNCTION', function)
            result = subprocess.run(['perl', '-T', '-e', program, folder], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'PASS')
