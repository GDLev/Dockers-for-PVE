package PVE::HA::Resources::Docker;

use strict;
use warnings;
use JSON qw(decode_json encode_json);
use PVE::Cluster;
use PVE::Tools;
use PVE::HA::Tools;
use PVE::BetterDocker::Replication;
use base qw(PVE::HA::Resources);

sub type { return 'docker'; }
sub verify_name { die "Invalid DCT ID\n" unless $_[1] =~ /\A[1-9][0-9]{2,8}\z/; }
sub options { return { map { $_ => { optional => 1 } } qw(state group comment failback max_restart max_relocate) }; }
sub config {
    my ($class, $vmid) = @_;
    $class->verify_name($vmid);
    my $entry = PVE::BetterDocker::Backup::inventory({ ids => {} })->{ids}->{$vmid}
        or die "DCT $vmid does not exist\n";
    my $id = $entry->{id};
    die "Invalid DCT identifier\n" unless $id =~ /\A([0-9a-f]{32})\z/;
    return decode_json(PVE::Cluster::get_config("betterdocker/$1.json"));
}
sub config_file { my $info = $_[0]->config($_[1]); return "/etc/pve/betterdocker/$info->{id}.json"; }
sub exists {
    my ($class, $id, $noerr) = @_;
    my $info = eval { $class->config($id); };
    die $@ if $@ && !$noerr;
    return $info ? 1 : undef;
}
sub helper {
    my ($info, @arguments) = @_;
    # Reuse native PVE SSH trust for configuration operations on another node.
    # JSON metadata and subprocess output are tainted in authenticated pvedaemon requests.
    @arguments = map {
        die "Invalid DCT helper argument\n" unless /\A([A-Za-z0-9][A-Za-z0-9_.:-]*)\z/;
        $1;
    } @arguments;
    die "Invalid DCT node\n" unless $info->{node} =~ /\A([A-Za-z0-9][A-Za-z0-9.-]*)\z/;
    $info = { %$info, node => $1 };
    my $command = ['/usr/local/lib/betterdocker/ha.py', @arguments];
    if ($info->{node} ne PVE::INotify::nodename()) {
        my $ssh = PVE::SSHInfo::ssh_info_to_command(PVE::SSHInfo::get_ssh_info($info->{node}));
        $command = [@$ssh, '--', map { PVE::Tools::shellquote($_) } @$command];
    }
    my $stderr = '';
    eval { PVE::Tools::run_command($command, errfunc => sub { $stderr .= "$_[0]\n"; }); };
    if (my $error = $@) { die($stderr || $error); }
}
sub power_task {
    my ($haenv, $info, $action, $timeout) = @_;
    require PVE::API2::BetterDocker;
    my $rpc = PVE::RPCEnvironment::get();
    local $rpc->{type} = 'ha';
    my $upid = PVE::API2::BetterDocker->map_method_by_name('action')->{code}->({
        id => $info->{id}, node => $info->{node}, action => $action, timeout => $timeout // 60,
    });
    PVE::HA::Tools::upid_wait($upid, $haenv);
}
sub start {
    my ($class, $haenv, $id) = @_;
    my $info = $class->config($id);
    die "HA guest ownership changed\n" unless $info->{node} eq $haenv->nodename();
    helper($info, 'recover_guest', $info->{id}, $info->{node}) if $info->{ha_recovery};
    power_task($haenv, $info, 'start');
}
sub shutdown {
    my ($class, $haenv, $id, $timeout) = @_;
    my $info = $class->config($id);
    die "HA guest ownership changed\n" unless $info->{node} eq $haenv->nodename();
    power_task($haenv, $info, ($timeout // 60) ? 'shutdown' : 'force-stop', $timeout);
}
sub check_running {
    my ($class, $haenv, $id) = @_;
    my $info = $class->config($id);
    return 0 if $info->{node} ne $haenv->nodename() || $info->{ha_recovery};
    my $result = '';
    PVE::Tools::run_command(['/usr/local/lib/betterdocker/ha.py', 'running', $info->{id}, $info->{node}],
        outfunc => sub { $result .= $_[0]; });
    return decode_json($result) ? 1 : 0;
}
sub migrate {
    my ($class, $haenv, $id, $target, $online) = @_;
    my $source = $class->config($id);
    helper($source, 'check_target', $target);
    $class->shutdown($haenv, $id) if $class->check_running($haenv, $id);
    my $info = $class->config($id);
    require PVE::API2::BetterDocker;
    my $rpc = PVE::RPCEnvironment::get();
    local $rpc->{type} = 'ha';
    my $upid = PVE::API2::BetterDocker->map_method_by_name('migrate')->{code}->({
        id => $info->{id}, node => $info->{node}, target => $target,
    });
    PVE::HA::Tools::upid_wait($upid, $haenv);
    return $class->config($id)->{node} eq $target;
}
sub remove_locks { return undef; } # Kernel operation locks disappear with the fenced process.
sub get_static_stats {
    my ($class, $haenv, $id) = @_;
    my $info = $class->config($id);
    return { maxcpu => $info->{metrics}->{cpus} // 1, maxmem => $info->{metrics}->{maxmem} // 0 };
}

my $installed;
sub hook {
    return if $installed++;
    no warnings 'redefine';
    PVE::BetterDocker::Backup::hook_inventory('PVE::HA::Config');
    my $parse = \&PVE::HA::Config::parse_sid;
    *PVE::HA::Config::parse_sid = sub {
        my ($sid) = @_;
        if ($sid =~ /\A[1-9][0-9]{2,8}\z/) {
            my $info = PVE::BetterDocker::Backup::inventory({ ids => {} })->{ids}->{$sid};
            $sid = "docker:$sid" if $info;
        }
        return $parse->($sid);
    };
    my $steal = \&PVE::HA::Env::PVE2::steal_service;
    *PVE::HA::Env::PVE2::steal_service = sub {
        my ($env, $sid, $source, $target) = @_;
        return $steal->(@_) unless $sid =~ /\Adocker:([1-9][0-9]{2,8})\z/;
        my $info = __PACKAGE__->config($1);
        helper({ %$info, node => $target }, 'check_recovery', $info->{id}, $target);
        # Called exclusively by the native CRM after successful fencing.
        PVE::Cluster::cfs_lock_file('datacenter.cfg', 10, sub {
            my $path = "/etc/pve/betterdocker/$info->{id}.json";
            my $current = decode_json(PVE::Cluster::get_config("betterdocker/$info->{id}.json"));
            die "HA source ownership changed\n" unless $current->{node} eq $source;
            my $ownerpath = "/etc/pve/betterdocker/ids/$info->{vmid}.json";
            my $owner = decode_json(PVE::Cluster::get_config("betterdocker/ids/$info->{vmid}.json"));
            die "HA reservation changed\n" unless $owner->{id} eq $info->{id} && $owner->{node} eq $source;
            $current->{ha_recovery} = { source => $source, token => PVE::Tools::file_get_contents('/proc/sys/kernel/random/uuid') };
            $current->{node} = $target;
            $owner->{node} = $target;
            PVE::Tools::file_set_contents($ownerpath, encode_json($owner));
            eval { PVE::Tools::file_set_contents($path, encode_json($current)); };
            if (my $error = $@) {
                $owner->{node} = $source;
                PVE::Tools::file_set_contents($ownerpath, encode_json($owner));
                die $error;
            }
        });
        die $@ if $@;
        $env->cluster_state_update();
    };
}

1;
