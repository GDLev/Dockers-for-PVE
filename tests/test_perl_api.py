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
our $user = 'root@pam';
sub get_user { $user }
sub check { $main::grants{$_[2] . ':' . $_[3]->[0]} // 0 }
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
use JSON::PP ();
sub decode_json { JSON::PP::decode_json($_[0]); }
1;
''')
            subprocess.run(['perl', '-T', '-I' + folder, '-I' + str(ROOT / 'backend'), '-MPVE::API2::BetterDocker', '-e', r'''
# PVE's router cannot mix fixed and wildcard segments at the same depth.
my %segments;
for my $route (@main::registered) {
    my @path = split('/', $route->{path});
    for my $depth (0 .. $#path) {
        my $parent = join('/', @path[0 .. $depth - 1]);
        my $kind = $path[$depth] =~ /^\{/ ? 'wildcard' : 'fixed';
        my $key = $depth . ':' . $parent;
        die 'router conflict' if $segments{$key} && $segments{$key} ne $kind;
        $segments{$key} = $kind;
    }
}
my ($create) = grep { $_->{name} eq 'create' } @main::registered;
die 'LXC template still required' if $create->{parameters}->{properties}->{ostemplate};
die 'deployment missing' unless $create->{parameters}->{properties}->{deployment};
for my $route (@main::registered) {
    die 'missing runtime permission wrapper' unless $route->{permissions}->{user} eq 'all';
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
$PVE::RPCEnvironment::user = 'audit@pve';
my %param = (id => 'a' x 32, node => 'pve', action => 'start');
for my $route (@main::registered) {
    next if $route->{name} eq 'list';
    eval { $route->{code}->(\%param); };
    die "route lacks authorization: $route->{name}" unless $@ =~ /Permission denied/;
}
$main::grants{'/vms/101:VM.Audit'} = 1;
*PVE::API2::BetterDocker::output = sub { '{"deployment":{"environment":"secret"},"services":[],"containers":[]}' };
my ($management) = grep { $_->{name} eq 'management' } @main::registered;
my $public = $management->{code}->(\%param);
die 'auditor can read source secrets' if exists($public->{deployment});
die 'auditor can configure Docker' if $public->{capabilities}->{configure};
my ($logs) = grep { $_->{name} eq 'logs' } @main::registered;
eval { $logs->{code}->(\%param); };
die 'auditor can read logs' unless $@ =~ /Permission denied/;
PVE::API2::BetterDocker::register_docker_method({ name => 'action', code => sub { 'allowed' } });
my $power = $main::registered[-1];
eval { $power->{code}->(\%param); };
die 'audit implies power grant' unless $@ =~ /Permission denied/;
$main::grants{'/vms/101:VM.PowerMgmt'} = 1;
die 'power permission rejected' unless $power->{code}->(\%param) eq 'allowed';
$param{action} = 'delete';
eval { $power->{code}->(\%param); };
die 'power grants destructive configuration' unless $@ =~ /Permission denied/;
PVE::API2::BetterDocker::register_docker_method({ name => 'snapshot', code => sub { 'allowed' } });
my $snapshot = $main::registered[-1];
$param{action} = 'create';
eval { $snapshot->{code}->(\%param); };
die 'power grants snapshot' unless $@ =~ /Permission denied/;
$main::grants{'/vms/101:VM.Snapshot.Rollback'} = 1;
eval { $snapshot->{code}->(\%param); };
die 'rollback grants snapshot creation' unless $@ =~ /Permission denied/;
$param{action} = 'restore';
die 'native rollback grant rejected' unless $snapshot->{code}->(\%param) eq 'allowed';
$main::grants{'/vms/101:VM.Snapshot'} = 1;
$param{action} = 'create';
die 'native snapshot grant rejected' unless $snapshot->{code}->(\%param) eq 'allowed';
$param{action} = 'delete';
$main::grants{'/nodes/pve:Sys.Console'} = 1;
die 'host administrator rejected' unless $power->{code}->(\%param) eq 'allowed';

''' , '101', 'a' * 32], check=True)
