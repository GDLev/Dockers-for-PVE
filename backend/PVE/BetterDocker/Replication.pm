package PVE::BetterDocker::Replication;

use strict;
use warnings;
use JSON qw(decode_json);
use PVE::Cluster;
use PVE::INotify;
use PVE::Storage;
use PVE::Tools;
use PVE::SSHInfo;
use PVE::BetterDocker::Backup;

my $helper = '/usr/local/lib/betterdocker/replica.py';

sub helper {
    my ($node, @arguments) = @_;
    # JSON metadata and subprocess output are tainted in authenticated pvedaemon requests.
    @arguments = map {
        die "Invalid DCT helper argument\n" unless /\A(--lock-fd|[A-Za-z0-9][A-Za-z0-9_.:-]*)\z/;
        $1;
    } @arguments;
    die "Invalid DCT node\n" unless $node =~ /\A([A-Za-z0-9][A-Za-z0-9.-]*)\z/;
    $node = $1;
    my $command = [$helper, @arguments];
    if ($node ne PVE::INotify::nodename()) {
        my $ssh = PVE::SSHInfo::ssh_info_to_command(PVE::SSHInfo::get_ssh_info($node));
        $command = [@$ssh, '--', map { PVE::Tools::shellquote($_) } @$command];
    }
    my $output = '';
    PVE::Tools::run_command($command, outfunc => sub { $output .= "$_[0]\n"; });
    return $output;
}

sub load_config {
    my ($class, $vmid, $node) = @_;
    my $entry = PVE::BetterDocker::Backup::inventory({ ids => {} })->{ids}->{$vmid}
        or die "DCT $vmid does not exist\n";
    my $id = $entry->{id};
    die "Invalid DCT identifier\n" unless $id =~ /\A([0-9a-f]{32})\z/;
    $id = $1;
    my $info = decode_json(PVE::Tools::file_get_contents("/etc/pve/betterdocker/$id.json"));
    die "DCT ownership changed\n" if defined($node) && $info->{node} ne $node;
    die "DCT migration is in progress\n" if $info->{migration};
    return $info;
}

sub get_replicatable_volumes {
    my ($class, $cfg, $vmid, $conf, $cleanup) = @_;
    die "DCT HA recovery is in progress\n" if $conf->{ha_recovery} && !$cleanup;
    my $volume = $conf->{replication};
    if (!$volume && !$cleanup) {
        $volume = decode_json(helper($conf->{node}, 'ensure', $conf->{id}, $conf->{node}));
    }
    return {} unless $volume;
    my $volid = $volume->{volid};
    my ($sid) = PVE::Storage::parse_volume_id($volid);
    my (undef, undef, $owner, undef, undef, $base, $format) = PVE::Storage::parse_volname($cfg, $volid);
    die "Invalid DCT replication volume\n" unless $owner == $vmid && !$base && $format eq 'raw' &&
        PVE::Storage::storage_config($cfg, $sid)->{type} eq 'zfspool';
    die "DCT replication storage is unsupported\n" unless $cleanup || PVE::Storage::volume_has_feature($cfg, 'replicate', $volid);
    return { $volid => 1 };
}

sub guest_type { return 'docker'; }
sub __snapshot_check_freeze_needed { my ($class, $vmid, $conf) = @_; return (($conf->{status} // '') =~ /^(running|partial)$/ ? 1 : 0, 0); }

my ($core_installed, $api_installed);
sub hook_core {
    return if $core_installed++;
    no warnings 'redefine';
    PVE::BetterDocker::Backup::hook_inventory(qw(PVE::API2::Replication PVE::API2::ReplicationConfig
        PVE::ReplicationState PVE::ReplicationConfig PVE::CLI::pvesr));
    my $native = \&PVE::Replication::replicate;
    *PVE::Replication::replicate = sub {
        return $native->(@_) unless $_[0] eq __PACKAGE__;
        my ($class, $job, $state, $time, $log) = @_;
        require PVE::VZDump::Docker;
        my $plugin = PVE::VZDump::Docker->new(undef);
        $plugin->{operation_action} = 'replication';
        $plugin->lock_vm($job->{guest});
        my $fd = fileno($plugin->{lock});
        local $^F = $fd > $^F ? $fd : $^F;
        my ($result, $error);
        eval {
            my $id = $plugin->identifier($job->{guest});
            helper($plugin->{node}, 'check-runtime', $job->{target}) unless $job->{remove_job};
            helper($plugin->{node}, 'prepare', $id, $plugin->{node}, '--lock-fd', $fd) unless $job->{remove_job};
            my $record_sync = \&PVE::ReplicationState::record_sync_end;
            local *PVE::ReplicationState::record_sync_end = sub {
                $record_sync->(@_);
                helper($plugin->{node}, 'publish', $id, $plugin->{node}, $job->{target},
                    $job->{id}, $_[2], '--lock-fd', $fd);
            };
            $result = $native->(@_);
            if ($job->{remove_job}) {
                my $config = PVE::ReplicationConfig->new();
                helper($plugin->{node}, 'cleanup', $id, $plugin->{node}, '--lock-fd', $fd)
                    unless grep { $_->{guest} == $job->{guest} } values %{$config->{ids}};
            }
        };
        $error = $@;
        eval { $plugin->unlock_vm($job->{guest}); };
        $error ||= $@;
        die $error if $error;
        return $result;
    };
}

sub hook_api {
    return if $api_installed++;
    my $native = $PVE::API2::Replication::lookup_guest_class;
    $PVE::API2::Replication::lookup_guest_class = sub {
        return __PACKAGE__ if $_[0] eq 'docker';
        return $native->(@_);
    };
}

1;
