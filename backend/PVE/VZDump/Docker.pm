package PVE::VZDump::Docker;

use strict;
use warnings;
use base qw(PVE::VZDump::Plugin);
use Fcntl qw(:flock F_GETFD F_SETFD FD_CLOEXEC);
use JSON qw(decode_json encode_json);
use PVE::Tools;
use PVE::INotify;
use PVE::Cluster;
use PVE::RPCEnvironment;
use PVE::BetterDocker::Backup;
use PVE::Storage::PBSPlugin;

my $helper = '/usr/local/lib/betterdocker/guest_state.py';
my $worker = '/usr/local/lib/betterdocker/provision.py';

sub new {
    my ($class, $vzdump) = @_;
    my $node = PVE::INotify::nodename();
    my $guests = PVE::BetterDocker::Backup::inventory({ ids => {} })->{ids};
    my $local = { map { $_ => $guests->{$_} } grep { $guests->{$_}->{node} eq $node } keys %$guests };
    return bless { vzdump => $vzdump, vmlist => $local, node => $node }, $class;
}
sub type { return 'docker'; }
sub identifier {
    my ($self, $vmid) = @_;
    my $guest = $self->{vmlist}->{$vmid} or die "DCT $vmid does not exist on this node\n";
    die "Invalid DCT project ID\n" unless $guest->{id} =~ /\A([0-9a-f]{32})\z/;
    return $1;
}
sub vm_status {
    my ($self, $vmid) = @_;
    open(my $fh, '-|', $worker, 'access-state', $self->identifier($vmid), $self->{node})
        or die "Cannot read DCT power state: $!\n";
    my $text = do { local $/; <$fh> };
    close($fh) or die "DCT power state query failed\n";
    my $state = decode_json($text);
    my $running = $state->{active_containers} ? 1 : 0;
    return wantarray ? ($running, $state->{status}) : $running;
}
sub helper {
    my ($self, @args) = @_;
    my $fd = $self->{lock} ? fileno($self->{lock}) : undef;
    push @args, '--lock-fd', $fd if defined($fd);
    # IPC::Open3 inherits this one descriptor; Python verifies its inode before use.
    local $^F = defined($fd) && $fd > $^F ? $fd : $^F;
    $self->cmd([$helper, @args]);
}
sub lock_vm {
    my ($self, $vmid) = @_;
    my $id = $self->identifier($vmid);
    my $directory = '/var/lib/betterdocker/operations';
    mkdir $directory, 0700 unless -d $directory;
    open(my $fh, '>>', "$directory/$id.lock") or die "Cannot open DCT backup lock: $!\n";
    chmod 0600, "$directory/$id.lock";
    flock($fh, LOCK_EX | LOCK_NB) or die "Another operation is running for DCT $vmid\n";
    my $flags = fcntl($fh, F_GETFD, 0);
    defined($flags) && defined(fcntl($fh, F_SETFD, $flags & ~FD_CLOEXEC))
        or die "Cannot inherit DCT backup lock: $!\n";
    $self->{lock} = $fh;
    $self->{locked_id} = $id;
    my $stat = PVE::Tools::file_get_contents("/proc/$$/stat");
    $stat =~ s/^.*\) //s;
    my @fields = split(/\s+/, $stat);
    PVE::Tools::file_set_contents("$directory/$id.json", encode_json({ active => JSON::true,
        action => $self->{operation_action} // 'backup', pid => $$, process_start => $fields[19], started => time(),
        user => PVE::RPCEnvironment::get()->get_user() }), 0600);
}
sub unlock_vm {
    my ($self, $vmid) = @_;
    return unless $self->{lock};
    # vzdump unlocks before cleanup/restart. Recover power state before exposing the guest.
    $self->helper('cleanup-capture', $self->identifier($vmid));
    my $path = '/var/lib/betterdocker/operations/' . $self->{locked_id} . '.json';
    my $state = decode_json(PVE::Tools::file_get_contents($path));
    $state->{active} = JSON::false;
    $state->{finished} = time();
    PVE::Tools::file_set_contents($path, encode_json($state), 0600);
    close(delete $self->{lock});
    delete $self->{locked_id};
}
sub prepare {
    my ($self, $task, $vmid, $mode) = @_;
    die "External backup providers are not supported for DCT\n" if $self->{vzdump}->{'backup-provider'};
    $task->{hostname} = $self->{vmlist}->{$vmid}->{name};
    $task->{dct_state} = "$task->{tmpdir}/state";
    $task->{snapshot_count} = 1;
}
sub stop_vm {
    my ($self, $task, $vmid) = @_;
    $self->helper('quiesce', $self->identifier($vmid), $self->{node}, 'stop');
}
sub suspend_vm {
    my ($self, $task, $vmid) = @_;
    $self->helper('quiesce', $self->identifier($vmid), $self->{node}, 'suspend');
    $task->{dct_suspended} = 1;
}
sub start_vm {
    my ($self, $task, $vmid) = @_;
    $self->helper('cleanup-capture', $self->identifier($vmid));
}
sub resume_vm { return shift->start_vm(@_); }
sub snapshot {
    my ($self, $task, $vmid) = @_;
    $self->helper('capture', $self->identifier($vmid), $self->{node}, $task->{dct_state}, '--keep-journal',
        $task->{dct_suspended} ? ('--paused') : ());
}
sub assemble {
    my ($self, $task, $vmid) = @_;
    $self->snapshot($task, $vmid) unless -f "$task->{dct_state}/manifest.json";
}
sub archive {
    my ($self, $task, $vmid, $filename, $compression) = @_;
    my $opts = $self->{vzdump}->{opts};
    if ($opts->{pbs}) {
        my @params = ("dct.conf:$task->{dct_state}/configuration/request.json", "dct.pxar:$task->{dct_state}", '--backup-type', 'host',
            '--backup-id', "dct-$vmid", '--backup-time', $task->{backup_time});
        push @params, '--rate', $opts->{bwlimit} * 1024 if $opts->{bwlimit};
        push @params, '--change-detection-mode', $opts->{'pbs-change-detection-mode'}
            if $opts->{'pbs-change-detection-mode'};
        PVE::Storage::PBSPlugin::run_raw_client_cmd($opts->{scfg}, $opts->{storage}, 'backup', \@params,
            logfunc => sub { $self->loginfo($_[0]); });
        my $files = PVE::Storage::PBSPlugin::run_client_cmd($opts->{scfg}, $opts->{storage}, 'files', [$task->{target}]);
        $task->{size} = 0;
        $task->{size} += $_->{size} // 0 for @$files;
    } else {
        my $pipeline = [['tar', '--numeric-owner', '--xattrs', '--acls', '--sparse',
            '-cf', '-', '-C', $task->{dct_state}, '.']];
        push @$pipeline, ['cstream', '-t', $opts->{bwlimit} * 1024] if $opts->{bwlimit};
        push @$pipeline, [split(/\s+/, $compression)] if $compression;
        if ($opts->{stdout}) {
            $self->cmd($pipeline, output => '>&' . fileno($opts->{stdout}));
        } else {
            push @{$pipeline->[-1]}, \('>' . PVE::Tools::shellquote($filename));
            $self->cmd($pipeline);
        }
    }
}
sub cleanup {
    my ($self, $task, $vmid) = @_;
    $self->helper('cleanup-capture', $self->identifier($vmid));
}

1;
