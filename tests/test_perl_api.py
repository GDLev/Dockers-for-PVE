"""Check host-only routes and cluster resource integration without requiring PVE."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PerlAPITests(unittest.TestCase):
    def test_host_api_has_no_lxc_schema_and_resources_do_not_expose_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'PVE').mkdir()
            (base / 'PVE/RESTHandler.pm').write_text('''
package PVE::RESTHandler;
sub register_method { push @main::registered, $_[1]; }
1;
''')
            (base / 'PVE/RPCEnvironment.pm').write_text('''
package PVE::RPCEnvironment;
sub get { bless {}, __PACKAGE__ }
sub get_user { 'root@pam' }
1;
''')
            (base / 'PVE/AccessControl.pm').write_text('package PVE::AccessControl; 1;')
            (base / 'PVE/Cluster.pm').write_text('package PVE::Cluster; sub check_vmid_unused { 1 } sub cfs_lock_file { $_[2]->(); } 1;')
            (base / 'PVE/Tools.pm').write_text('package PVE::Tools; sub run_command { my ($cmd, %opts) = @_; $opts{outfunc}->($ARGV[0]); } 1;')
            # Avoid relying on a locally installed Perl JSON distribution.
            (base / 'JSON.pm').write_text('''
package JSON;
use Exporter 'import';
our @EXPORT_OK = qw(decode_json);
sub decode_json { die 'not used in this test'; }
1;
''')
            subprocess.run(['perl', '-T', '-I' + folder, '-I' + str(ROOT / 'backend'), '-MPVE::API2::BetterDocker', '-e', '''
my ($create) = grep { $_->{name} eq 'create' } @main::registered;
die 'LXC template still required' if $create->{parameters}->{properties}->{ostemplate};
die 'deployment missing' unless $create->{parameters}->{properties}->{deployment};
for my $route (@main::registered) {
    die 'non-root grant' if $route->{permissions}->{user} || $route->{permissions}->{check};
    die 'unprotected' unless $route->{protected} || $route->{name} eq 'vncwebsocket';
}
package PVE::API2::Cluster;
our $info = { code => sub { [] }, returns => { items => { properties => { type => { enum => ['lxc', 'qemu'] } } } } };
sub map_method_by_name { $_[1] eq 'resources' ? $info : undef }
package main;
no warnings 'redefine';
*PVE::API2::BetterDocker::summaries = sub { [{ id => 'a' x 32, vmid => $ARGV[0], node => 'pve', name => 'web', status => 'running', address => '192.0.2.50' }] };
PVE::API2::BetterDocker::hook_resources();
my $result = $PVE::API2::Cluster::info->{code}->({});
die 'numeric ID missing' unless $result->[0]->{vmid} == 101;
use Scalar::Util qw(tainted);
use IO::File;
die 'test input is not tainted' unless tainted($ARGV[0]);
my $worker = PVE::API2::BetterDocker::task_id('a' x 32, 'pve');
die 'numeric task ID missing' unless $worker == 101;
die 'metadata ID still tainted' if tainted($worker);
my $reserved = PVE::API2::BetterDocker::reserve_guest_id('a' x 32, 'pve', 101);
die 'subprocess ID still tainted' if tainted($reserved);
my $legacy = PVE::API2::BetterDocker::worker_id($ARGV[1]);
die 'legacy UUID still tainted' if tainted($legacy);
# Exercise the same sysopen sink as the worker task log, with perl -T enabled.
eval { IO::File->new('/dev/null/' . $ARGV[0], 'w'); };
die 'taint regression control did not fail' unless $@ =~ /Insecure dependency/;
for my $value ($worker, $reserved, $legacy) {
    # ENOTDIR is expected; an insecure dependency exception is not.
    eval { IO::File->new('/dev/null/' . $value, 'w'); };
    die $@ if $@;
}
for my $invalid ('../101', "101\n", '99', '1000000000', '101;id', 'A' x 32) {
    eval { PVE::API2::BetterDocker::worker_id($invalid); };
    die 'unsafe worker ID accepted' unless $@;
}
die 'missing Docker resource' unless $result->[0]->{type} eq 'docker';
die 'config exposed' if $result->[0]->{deployment} || $result->[0]->{compose};
die 'schema missing Docker' unless grep { $_ eq 'docker' } @{$PVE::API2::Cluster::info->{returns}->{items}->{properties}->{type}->{enum}};
''' , '101', 'a' * 32], check=True)
