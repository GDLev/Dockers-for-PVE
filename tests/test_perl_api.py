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
            (base / 'PVE/GuestHelpers.pm').write_text('package PVE::GuestHelpers; sub assert_tag_permissions { 1 } 1;')
            (base / 'PVE/JSONSchema.pm').write_text('package PVE::JSONSchema; 1;')
            (base / 'PVE/AccessControl.pm').write_text('package PVE::AccessControl; 1;')
            (base / 'PVE/Cluster.pm').write_text('package PVE::Cluster; sub check_vmid_unused { 1 } sub cfs_lock_file { $_[2]->(); } 1;')
            (base / 'PVE/Tools.pm').write_text('package PVE::Tools; sub run_command { my ($cmd, %opts) = @_; $opts{outfunc}->($ARGV[0]); } sub assert_if_modified { die "detected modified configuration" if defined($_[0]) && $_[0] ne $_[1]; } 1;')
            # Avoid relying on a locally installed Perl JSON distribution.
            (base / 'JSON.pm').write_text('''
package JSON;
use Exporter 'import';
our @EXPORT_OK = qw(decode_json encode_json);
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
*PVE::API2::BetterDocker::output = sub { '{"deployment":{"environment":"secret"},"digest":"private-fingerprint","services":[],"containers":[]}' };
my ($management) = grep { $_->{name} eq 'management' } @main::registered;
my $public = $management->{code}->(\%param);
die 'auditor can read source secrets' if exists($public->{deployment});
die 'auditor can configure Docker' if $public->{capabilities}->{configure};
die 'auditor can read config digest' if exists($public->{digest});
my ($logs) = grep { $_->{name} eq 'logs' } @main::registered;
eval { $logs->{code}->(\%param); };
die 'auditor can read logs' unless $@ =~ /Permission denied/;
PVE::API2::BetterDocker::register_docker_method({ name => 'action', code => sub { 'allowed' } });
my $power = $main::registered[-1];
eval { $power->{code}->(\%param); };
die 'audit implies power grant' unless $@ =~ /Permission denied/;
$main::grants{'/vms/101:VM.PowerMgmt'} = 1;
die 'power permission rejected' unless $power->{code}->(\%param) eq 'allowed';
for my $command ('shutdown', 'reboot', 'force-stop') {
 $param{action} = $command;
 die 'native power command rejected' unless $power->{code}->(\%param) eq 'allowed';
}
# Worker identity is derived from RPC state, not from inherited environment values.
local $ENV{BETTERDOCKER_TASK_USER} = 'root@pam';
PVE::API2::BetterDocker::register_docker_method({ name => 'action', code => sub {
 die 'forged task owner' unless $ENV{BETTERDOCKER_TASK_USER} eq 'audit@pve';
 die 'forged Sys.Modify grant' unless $ENV{BETTERDOCKER_TASK_SYS_MODIFY} eq '0';
 return 'allowed';
} });
die 'identity wrapper rejected' unless $main::registered[-1]->{code}->(\%param) eq 'allowed';
*PVE::API2::BetterDocker::output = sub { '{"operation":{"active":true,"action":"shutdown","user":"other@pve"},"services":[],"containers":[]}' };
die 'other owner can overrule' if $management->{code}->(\%param)->{capabilities}->{overrule_shutdown};
*PVE::API2::BetterDocker::output = sub { '{"operation":{"active":true,"action":"shutdown","user":"audit@pve!job"},"services":[],"containers":[]}' };
die 'own token cannot overrule' unless $management->{code}->(\%param)->{capabilities}->{overrule_shutdown};
$param{action} = 'delete';
eval { $power->{code}->(\%param); };
die 'power grants destructive configuration' unless $@ =~ /Permission denied/;
PVE::API2::BetterDocker::register_docker_method({ name => 'destroy', code => sub { 'allowed' } });
my $destroy = $main::registered[-1];
eval { $destroy->{code}->(\%param); };
die 'power grants DELETE access' unless $@ =~ /Permission denied/;
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
# Grant only CPU configuration. It must not imply host access or other fields.
$main::grants{'/vms/101:VM.Config.CPU'} = 1;
my $caps = PVE::API2::BetterDocker::capabilities('a' x 32, 'pve');
die 'CPU configuration missing' unless $caps->{cpu} && $caps->{settings};
die 'CPU grant implies host or memory' if $caps->{configure} || $caps->{console} || $caps->{memory} || $caps->{network} || $caps->{options};
PVE::API2::BetterDocker::register_docker_method({ name => 'settings', code => sub {
 die 'wrong worker field grant' unless $ENV{BETTERDOCKER_CONFIG_KEYS} eq 'cpus,cpuunits'; return 'allowed';
} });
local $ENV{BETTERDOCKER_CONFIG_KEYS} = '*';
die 'CPU settings rejected' unless $main::registered[-1]->{code}->(\%param) eq 'allowed';
for my $name ('update', 'destroy', 'termproxy', 'logs', 'storage_limit', 'reset_settings', 'preview') {
 my ($route) = grep { $_->{name} eq $name } @main::registered;
 eval { $route->{code}->(\%param); };
 die "CPU grants host route: $name" unless $@ =~ /Permission denied/;
}
*PVE::API2::BetterDocker::output = sub { '{"deployment":{"environment":"secret"},"digest":"fingerprint","services":[],"containers":[]}' };
my $delegated = $management->{code}->(\%param);
die 'delegate reads deployment' if exists($delegated->{deployment});
die 'delegate lacks edit digest' unless $delegated->{digest} eq 'fingerprint';
$param{action} = 'delete';
$main::grants{'/nodes/pve:Sys.Console'} = 1;
die 'host administrator rejected' unless $power->{code}->(\%param) eq 'allowed';
for my $name ('update', 'settings', 'reset_settings', 'storage_limit', 'revert_settings') {
 my ($route) = grep { $_->{name} eq $name } @main::registered;
 die 'missing optional digest' unless $route->{parameters}->{properties}->{digest}->{optional};
 *PVE::API2::BetterDocker::output = sub { die 'unexpected helper before digest' unless $_[0]->[0] eq 'detail'; return '{"digest":"changed"}'; };
 eval { $route->{code}->({ id => 'a' x 32, node => 'pve', digest => '0' x 64 }); };
 die "missing API preflight: $name" unless $@ =~ /detected modified configuration/;
}


''' , '101', 'a' * 32], check=True)
