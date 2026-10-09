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
            (base / 'PVE/Exception.pm').write_text("package PVE::Exception; use Exporter 'import'; our @EXPORT_OK = qw(raise_perm_exc); sub raise_perm_exc { die 'Permission denied' } 1;")
            (base / 'PVE/GuestHelpers.pm').write_text('package PVE::GuestHelpers; sub assert_tag_permissions { 1 } 1;')
            (base / 'PVE/JSONSchema.pm').write_text('package PVE::JSONSchema; 1;')
            (base / 'PVE/AccessControl.pm').write_text('package PVE::AccessControl; 1;')
            (base / 'PVE/Cluster.pm').write_text('package PVE::Cluster; sub check_vmid_unused { 1 } sub cfs_lock_file { $_[2]->(); } 1;')
            (base / 'PVE/Tools.pm').write_text('package PVE::Tools; sub run_command { my ($cmd, %opts) = @_; $opts{outfunc}->($ARGV[0]); } sub assert_if_modified { die "detected modified configuration" if defined($_[0]) && $_[0] ne $_[1]; } 1;')
            (base / 'PVE/API2/Storage').mkdir(parents=True)
            (base / 'PVE/API2/Storage/Content.pm').write_text('''
package PVE::API2::Storage::Content;
our $last;
our $index = { parameters => { properties => {} }, returns => { items => { properties => {} } },
 code => sub { $last = $_[0]; return [] if $_[0]->{storage} eq 'dir'; return [
 {vmid=>103,volid=>'local-lvm:vm-103-disk-0',content=>'rootdir'},
 {vmid=>105,volid=>'local-lvm:vm-105-disk-0',content=>'images'},
 {vmid=>105,volid=>'local:backup/example',content=>'backup'} ]; } };
our $delete = { code => sub { PVE::Storage::vdisk_free({}, $_[0]->{volume}); return 'native-delete'; } };
sub map_method_by_name { $_[1] eq 'index' ? $index : $delete }
1;
''')
            (base / 'PVE/Storage.pm').write_text('''
package PVE::Storage;
sub config { {} }
sub vdisk_list { return {$_[1] => ($_[1] eq 'dir' ? [{vmid=>105, volid=>'dir:105/vm-105-disk-0.raw', size=>1024, format=>'raw'}] : [])}; }
sub check_volume_access { 1 }
sub parse_volname { return ('backup', '', 105) if $_[1] =~ /backup/; return ('images', '', $_[1] =~ /vm-105/ ? 105 : 103); }
sub vdisk_free { $main::freed = $_[1]; }
1;
''')
            # Storage/PBS contracts are exercised on PVE; this harness isolates route authorization.
            (base / 'PVE/BetterDocker').mkdir()
            (base / 'PVE/BetterDocker/Backup.pm').write_text('package PVE::BetterDocker::Backup; sub hook_storage { 1 } 1;')
            (base / 'PVE/BetterDocker/Firewall.pm').write_text('package PVE::BetterDocker::Firewall; sub register_api { 1 } 1;')
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
    die 'console incorrectly proxied between nodes' if $route->{name} =~ /^(termproxy|vncwebsocket)$/ && $route->{proxyto};
}
package PVE::API2::Cluster;
our $info = { code => sub { [] }, returns => { items => { properties => { type => { enum => ['lxc', 'qemu'] } } } } };
sub map_method_by_name { $_[1] eq 'resources' ? $info : undef }
package main;
no warnings 'redefine';
*PVE::API2::BetterDocker::ha_managed = sub { 0 };
*PVE::HA::Config::get_service_status = sub { return { managed => 0 }; };
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
# Guest-scoped console and state operations must not require node console access.
*PVE::API2::BetterDocker::output = sub { return '{"safe":true}' if $_[0]->[0] eq 'delegated-check'; return '{"services":[],"containers":[]}'; };
$main::grants{'/vms/101:VM.Console'} = 1;
$main::grants{'/vms/101:VM.Clone'} = 1;
$main::grants{'/vms/101:VM.Migrate'} = 1;
$main::grants{'/vms/101:VM.Config.Disk'} = 1;
my $guest = PVE::API2::BetterDocker::capabilities('a' x 32, 'pve');
die 'guest delegation rejected' unless $guest->{console} && $guest->{clone} && $guest->{migrate} && $guest->{disk};
die 'guest delegation grants host trust' if $guest->{trusted} || $guest->{configure};
*PVE::API2::BetterDocker::output = sub { '{"safe":false}' };
$guest = PVE::API2::BetterDocker::capabilities('a' x 32, 'pve');
die 'unsafe deployment delegated' if $guest->{console} || $guest->{clone} || $guest->{migrate} || $guest->{disk};
delete @main::grants{map { '/vms/101:' . $_ } qw(VM.Console VM.Clone VM.Migrate VM.Config.Disk)};

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
my $host_only = PVE::API2::BetterDocker::capabilities('a' x 32, 'pve');
die 'node console grants guest configuration' if $host_only->{configure} || $host_only->{console} || $host_only->{remove};
$main::grants{'/vms/101:' . $_} = 1 for qw(VM.Allocate VM.Config.Memory VM.Config.Network VM.Config.Options VM.Config.Disk);
die 'host administrator rejected' unless $power->{code}->(\%param) eq 'allowed';
for my $name ('update', 'settings', 'reset_settings', 'storage_limit', 'revert_settings') {
 my ($route) = grep { $_->{name} eq $name } @main::registered;
 die 'missing optional digest' unless $route->{parameters}->{properties}->{digest}->{optional};
 *PVE::API2::BetterDocker::output = sub { die 'unexpected helper before digest' unless $_[0]->[0] eq 'detail'; return '{"digest":"changed"}'; };
 eval { $route->{code}->({ id => 'a' x 32, node => 'pve', digest => '0' x 64 }); };
 die "missing API preflight: $name" unless $@ =~ /detected modified configuration/;
}


# Native storage content remains complete unless the new filter is explicitly requested.
$PVE::RPCEnvironment::user = 'root@pam';
*PVE::API2::BetterDocker::storage_guest_owner = sub {
    return $_[0] == 105 ? {id => 'b' x 32, vmid => 105, node => 'pve', name => 'archived'} : undef;
};
my $content = $PVE::API2::Storage::Content::index;
my $all = $content->{code}->({node=>'pve',storage=>'local-lvm'});
die 'native full list changed' unless @$all == 3;
my $docker = $content->{code}->({node=>'pve',storage=>'local-lvm',content=>'rootdir',dct=>1});
die 'Docker disk classification missing' unless @$docker == 1 && $docker->[0]->{vmid} == 105;
die 'retained owner missing' unless $docker->[0]->{dct_name} eq 'archived' && $docker->[0]->{dct_status} eq 'retained';
die 'raw Docker disk hidden by content type' if defined($PVE::API2::Storage::Content::last->{content});
my $dir = $content->{code}->({node=>'pve',storage=>'dir',dct=>1});
die 'rootdir-only directory disk missing' unless @$dir == 1 && $dir->[0]->{volid} eq 'dir:105/vm-105-disk-0.raw';
{
 local %main::grants;
 $PVE::RPCEnvironment::user = 'storage-audit@pve';
 my $restricted = $content->{code}->({node=>'pve',storage=>'local-lvm',dct=>1});
 die 'storage-only viewer reads private guest name' unless $restricted->[0]->{dct_name} eq '';
}
$PVE::RPCEnvironment::user = 'root@pam';
my $native = $content->{code}->({node=>'pve',storage=>'local-lvm',content=>'rootdir',dct=>0});
die 'LXC or backups changed' unless @$native == 2;
eval { $PVE::API2::Storage::Content::delete->{code}->({volume=>'local-lvm:vm-105-disk-0'}); };
die 'DCT disk deletion accepted' unless $@ =~ /Cannot remove a DCT disk/;
die 'blocked disk was freed' if $main::freed;
$PVE::API2::Storage::Content::delete->{code}->({volume=>'local-lvm:vm-103-disk-0'});
die 'native deletion behavior changed' unless $main::freed eq 'local-lvm:vm-103-disk-0';
{
 my $calls = 0;
 local *PVE::API2::BetterDocker::storage_guest_owner = sub { return ++$calls == 1 ? undef : {id=>'b' x 32}; };
 eval { $PVE::API2::Storage::Content::delete->{code}->({volume=>'local-lvm:vm-105-disk-0'}); };
 die 'worker did not recheck DCT ownership' unless $@ =~ /Cannot remove a DCT disk/;
 die 'race freed DCT disk' unless $main::freed eq 'local-lvm:vm-103-disk-0';
}

# Unsupported native operations must reject DCT before entering a native handler,
# including mixed backup lists, clone targets and retained guest IDs.
my %native_methods;
for my $class (qw(PVE::API2::LXC PVE::API2::Qemu PVE::API2::Backup PVE::API2::ReplicationConfig PVE::API2::HA::Resources PVE::API2::Cluster::BulkAction::Guest)) {
    no strict 'refs';
    *{"${class}::map_method_by_name"} = sub {
        my ($class, $name) = @_;
        return $native_methods{"$class:$name"} //= {code => sub {
            $main::native_calls++;
            die 'native permission denied' if $_[0]->{denied};
            return $_[0];
        }};
    };
}
PVE::API2::BetterDocker::hook_native_node_operations();
{ no warnings 'redefine'; *PVE::HA::Config::parse_sid = sub { my $sid=$_[0]; $sid="ct:$sid" if $sid =~ /^\d+$/; my($type,$id)=split(':',$sid); return ($sid,$type,$id); }; }
PVE::API2::BetterDocker::hook_native_cluster_operations();
my $original_wrapper = PVE::API2::Backup->map_method_by_name('create_job')->{code};
PVE::API2::Backup->map_method_by_name('update_job');
PVE::API2::BetterDocker::hook_native_cluster_operations();
die 'native method wrapped twice' unless $original_wrapper == $native_methods{'PVE::API2::Backup:create_job'}->{code};
for my $case (
    (map { ['PVE::API2::HA::Resources',$_,{sid=>'ct:105'}] } qw(create update migrate relocate)),
    ['PVE::API2::HA::Resources','create',{sid=>'vm:105'}],
    (map { ['PVE::API2::LXC',$_,{vmid=>105}] } qw(create_vm clone_vm template migrate_vm remote_migrate_vm)),
    (map { ['PVE::API2::Qemu',$_,{vmid=>105}] } qw(create_vm clone_vm template migrate_vm remote_migrate_vm)),
    ['PVE::API2::LXC','clone_vm',{vmid=>103,newid=>105}],
    ['PVE::API2::Qemu','clone_vm',{vmid=>103,newid=>105}],
) {
    my ($class,$name,$param) = @$case;
    my $before = $main::native_calls // 0;
    eval { $native_methods{"$class:$name"}->{code}->($param); };
    die "native DCT route accepted: $class/$name" unless $@ =~ /DCT 105 does not support native/;
    die 'native mutation invoked for DCT' if ($main::native_calls // 0) != $before;
}
# Native HA registration and local policy changes share one per-guest lock.
{
 my ($locked, @actions, $existing);
 local *PVE::Tools::lock_file = sub {
  die 'unexpected HA lock' unless $_[0] eq '/run/lock/betterdocker-ha-105.lock';
  local $main::ha_transaction = 1;
  my $result=eval { $_[2]->(); };
  return $result;
 };
 local *PVE::HA::Config::read_resources_config = sub { {ids => $existing ? {'docker:105'=>{}} : {}} };
 local *PVE::HA::Resources::Docker::config = sub {
  die 'HA config outside transaction' unless $main::ha_transaction;
  return {id=>'a' x 32,node=>'pve1'};
 };
 local *PVE::HA::Resources::Docker::helper = sub {
  die 'HA policy outside transaction' unless $main::ha_transaction;
  push @actions, $_[1];
 };
 my $param={sid=>'docker:105'};
 die 'native HA return changed' unless $native_methods{'PVE::API2::HA::Resources:create'}->{code}->($param)==$param;
 die 'HA enrollment skipped' unless "@actions" eq 'enroll';
 @actions=();
 eval { $native_methods{'PVE::API2::HA::Resources:create'}->{code}->({sid=>'docker:105',denied=>1}); };
 die 'native HA error swallowed' unless $@ =~ /native permission denied/;
 die 'failed HA create retained policy override' unless "@actions" eq 'enroll release';
 @actions=(); $existing=1;
 $native_methods{'PVE::API2::HA::Resources:create'}->{code}->($param);
 die 'existing HA guest reenrolled' if @actions;
 $native_methods{'PVE::API2::HA::Resources:delete'}->{code}->($param);
 die 'HA removal skipped release' unless "@actions" eq 'release';
}

for my $list ('105', '103 105', '103,105', '103;105') {
    my $param = {vmid=>$list};
    die 'DCT scheduled backup parameters changed' unless
        $native_methods{'PVE::API2::Backup:create_job'}->{code}->($param) == $param;
}
for my $param ({vmid=>'103'}, {all=>1}, {pool=>'native'}, {exclude=>'105'}) {
    my $result = $native_methods{'PVE::API2::Backup:create_job'}->{code}->($param);
    die 'native non-DCT parameter changed' unless $result == $param;
}
eval { $native_methods{'PVE::API2::Backup:create_job'}->{code}->({vmid=>'103',denied=>1}); };
die 'native permission check bypassed' unless $@ =~ /native permission denied/;



''' , '101', 'a' * 32], check=True)

    def test_network_checkbox_serializes_as_number(self):
        source = (ROOT / 'backend/PVE/API2/BetterDocker.pm').read_text()
        start = source.index('my %values = map {', source.index("name => 'set_network'"))
        end = source.index('my ($fh, $path)', start)
        fragment = source[start:end]
        code = "use JSON::PP; for my $input ('0', '', '1', 0, 1) { my $param = {firewall=>$input}; " + fragment + " print encode_json(\\%values), qq(\\n); }"
        result = subprocess.run(['perl', '-e', code], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.splitlines(), ['{"firewall":0}', '{"firewall":0}', '{"firewall":1}', '{"firewall":0}', '{"firewall":1}'])
