#!/usr/bin/perl
# Private, root-only bridge to the installed PVE storage implementation.
use strict;
use warnings;
use JSON qw(decode_json encode_json);
use PVE::Storage;
use PVE::RPCEnvironment;
use PVE::Storage::LVMPlugin;
use PVE::Cluster;
use PVE::INotify;
use PVE::JSONSchema;
use PVE::Tools;
use Fcntl qw(O_RDONLY O_WRONLY O_CREAT O_EXCL :flock);
die "Root required\n" if $>;
local $/;
my $p = decode_json(<STDIN>);
my $id = $p->{id};
die "Invalid project\n" unless $id =~ /\A[0-9a-f]{32}\z/;
my $base = "/var/lib/betterdocker/projects/$id";
my $operation_lock;
if (defined($p->{operation_fd})) {
    die "Invalid inherited operation descriptor\n" unless $p->{operation_fd} =~ /\A[0-9]+\z/;
    open($operation_lock, '+<&=', $p->{operation_fd}) or die "Cannot inherit storage operation lock\n";
    my @actual = stat($operation_lock);
    my @expected = stat("/var/lib/betterdocker/operations/$id.lock");
    die "Storage operation lock ownership changed\n" unless @actual && @expected && $actual[0] == $expected[0] && $actual[1] == $expected[1];
    flock($operation_lock, LOCK_EX | LOCK_NB) or die "Storage operation lock lost\n";
}
sub read_json {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "$path: $!\n";
    return decode_json(<$fh>);
}
sub durable_json {
    my ($path, $value) = @_;
    my $temporary = "$path.tmp-$$";
    sysopen(my $fh, $temporary, O_WRONLY|O_CREAT|O_EXCL, 0600) or die "$temporary: $!\n";
    print $fh encode_json($value) or die "Cannot write storage journal: $!\n";
    $fh->flush() or die "Cannot flush storage journal: $!\n";
    PVE::Tools::fsync(fileno($fh)) or die "Cannot sync storage journal: $!\n";
    close($fh) or die "Cannot close storage journal: $!\n";
    rename($temporary, $path) or die "Cannot publish storage journal: $!\n";
    sysopen(my $directory, $base, O_RDONLY) or die "$base: $!\n";
    PVE::Tools::fsync(fileno($directory)) or die "Cannot sync storage directory: $!\n";
    close($directory);
}
my $metadata = '/etc/pve/betterdocker';
if (defined($p->{metadata})) {
    die "Invalid transfer metadata\n" unless $p->{metadata} =~
        m!\A(/var/lib/betterdocker/transfers/[0-9a-f]{32}/metadata)\z!;
    $metadata = $1;
    my @stat = lstat($metadata);
    die "Unsafe transfer metadata\n" unless @stat && !-l $metadata && $stat[4] == 0 && ($stat[2] & 077) == 0;
    my $owner = read_json("/etc/pve/betterdocker/ids/" . read_json("$metadata/$id.json")->{vmid} . '.json');
    die "Transfer ownership changed\n" unless $owner->{id} eq $id;
}
my $exists = -f "$metadata/$id.json";
my $info = read_json($exists ? "$metadata/$id.json" : "$base/storage.json");
# A migrated guest can leave a local owned disk behind. Only explicit cleanup may use its local state.
if ($exists && $info->{node} ne PVE::INotify::nodename() && $p->{action} =~ /\Afree(?:-check)?\z/) {
    my $local = read_json("$base/storage.json");
    die "Local disk ownership changed\n" unless $local->{vmid} == $info->{vmid};
    $info = $local;
}
die "Deleted guest permits disk cleanup only\n" if !$exists && $p->{action} ne 'free' && $p->{action} ne 'free-check' && $p->{action} ne 'snapshot-delete';
my $vmid = $info->{vmid};
die "Wrong node\n" unless $info->{node} eq PVE::INotify::nodename();
my $reservation = read_json("$metadata/ids/$vmid.json");
die "Guest ownership changed\n" unless $reservation->{id} eq $id;
my $cfg = PVE::Storage::config();
my $action = $p->{action};
my $volid = $p->{volid};
my ($sid, $scfg);
if ($volid) {
    ($sid) = PVE::Storage::parse_volume_id($volid);
    my (undef, undef, $owner, undef, undef, $isbase, $format) = PVE::Storage::parse_volname($cfg, $volid);
    die "Volume ownership changed\n" unless $owner == $vmid && !$isbase && $format eq 'raw';
    # A matching numeric VMID alone does not authorize unrelated guest disks.
    my @owned;
    for my $file ('storage.json', 'storage-transaction.json', 'allocated-volume.json', 'replication.json') {
        next unless -f "$base/$file";
        my $state = read_json("$base/$file");
        push @owned, $state->{volid}, $state->{target}->{volid}, @{$state->{unused} // []};
        push @owned, map { $_->{volid} } values %{$state->{additional} // {}};
    }
    die "Unmanaged volume\n" unless grep { defined($_) && $_ eq $volid } @owned;
} else {
    $sid = $p->{storage};
    PVE::JSONSchema::parse_storage_id($sid);
}
if (($ENV{BETTERDOCKER_TRUSTED} // '1') ne '1' && $action =~ /\A(?:allocate|resize)\z/) {
    my $actor = $ENV{BETTERDOCKER_TASK_USER} // die "Missing authenticated storage owner\n";
    my $rpc = PVE::RPCEnvironment->init('cli');
    $rpc->init_request();
    $rpc->set_user($actor);
    $rpc->check($actor, "/storage/$sid", ['Datastore.AllocateSpace']);
}
$scfg = PVE::Storage::storage_config($cfg, $sid);
die "Only local directory, LVM-thin and ZFS rootdir storage is supported\n"
    unless ($scfg->{type} eq 'dir' || $scfg->{type} eq 'lvmthin' || $scfg->{type} eq 'zfspool') && $scfg->{content}->{rootdir} && !$scfg->{shared};
PVE::Storage::storage_check_enabled($cfg, $sid, $info->{node});
my $result = {};
if ($action eq 'linked-clone') {
    my $source_id = $p->{source_id};
    die "Invalid linked clone source\n" unless $source_id =~ /\A[0-9a-f]{32}\z/;
    my $source_info = read_json("$metadata/$source_id.json");
    die "Linked clones require a local template\n" unless $source_info->{template} && $source_info->{node} eq $info->{node};
    my $source_disk = read_json("/var/lib/betterdocker/projects/$source_id/storage.json");
    my $source_volume = $p->{source_volume};
    my @source_owned = ($source_disk->{volid}, map { $_->{volid} } values %{$source_disk->{additional} // {}});
    die "Unowned linked clone source\n" unless grep { $_ eq $source_volume } @source_owned;
    my ($source_sid) = PVE::Storage::parse_volume_id($source_volume);
    die "Linked clones require the same LVM-thin storage\n" unless $source_sid eq $sid && $scfg->{type} eq 'lvmthin';
    if (($ENV{BETTERDOCKER_TRUSTED} // '1') ne '1') {
        my $rpc = PVE::RPCEnvironment->init('cli');
        $rpc->init_request();
        $rpc->set_user($ENV{BETTERDOCKER_TASK_USER});
        $rpc->check($ENV{BETTERDOCKER_TASK_USER}, "/vms/$source_info->{vmid}", ['VM.Clone']);
        $rpc->check($ENV{BETTERDOCKER_TASK_USER}, "/storage/$sid", ['Datastore.AllocateSpace']);
    }
    die "An unfinished allocation exists\n" if -e "$base/allocated-volume.json";
    my $name = $p->{clone_tag};
    die "Invalid clone snapshot name\n" unless $name =~ /\Ab[0-9a-f]{32}\z/;
    durable_json("$base/linked-clone.json", { source_id => $source_id, source_volume => $source_volume, name => $name });
    my $failure;
    eval {
        PVE::Storage::volume_snapshot($cfg, $source_volume, $name);
        my $plugin = PVE::Storage::Plugin->lookup($scfg->{type});
        my $target_name = $plugin->find_free_diskname($sid, $scfg, $vmid);
        my $expected = "$sid:$target_name";
        durable_json("$base/allocated-volume.json", { volid => $expected });
        $volid = PVE::Storage::vdisk_clone($cfg, $source_volume, $vmid, $name);
        die "Linked clone allocation identity changed\n" unless $volid eq $expected;
    };
    $failure = $@;
    PVE::Storage::volume_snapshot_delete($cfg, $source_volume, $name, 0);
    unlink "$base/linked-clone.json";
    die $failure if $failure;
} elsif ($action eq 'allocate') {
    die "An unfinished allocation exists\n" if -e "$base/allocated-volume.json";
    my $size = $p->{size_mib};
    die "Invalid size\n" unless $size =~ /\A[0-9]+\z/ && $size >= 256 && $size <= 1048576;
    my $status = PVE::Storage::storage_info($cfg, 'rootdir');
    die "Insufficient storage space\n" unless $status->{$sid}->{avail} >= $size * 1048576;
    my $plugin = PVE::Storage::Plugin->lookup($scfg->{type});
    my $name = $plugin->find_free_diskname($sid, $scfg, $vmid, 'raw', $scfg->{type} eq 'dir' ? 1 : 0);
    my $expected = "$sid:" . ($scfg->{type} eq 'dir' ? "$vmid/" : '') . $name;
    # Record the exact native volume name before allocation, including node reboot.
    durable_json("$base/allocated-volume.json", { volid => $expected });
    $volid = PVE::Storage::vdisk_alloc($cfg, $sid, $vmid, 'raw', $name, $size * 1024);
    die "Storage allocation identity changed\n" unless $volid eq $expected;
} elsif ($action eq 'resize') {
    my $size = $p->{size_mib} * 1048576;
    my ($old) = PVE::Storage::volume_size_info($cfg, $volid);
    die "Disks may only grow\n" unless $size > $old;
    PVE::Storage::volume_resize($cfg, $volid, $size, 0);
} elsif ($action =~ /\A(snapshot|rollback|snapshot-delete|rollback-check|snapshot-export|snapshot-export-cleanup)\z/) {
    my $name = $p->{name};
    die "Invalid snapshot\n" unless $name =~ /\A[a-zA-Z][a-zA-Z0-9_-]{0,39}\z/;
    if (!$exists) {
        my $manifest = read_json("/var/lib/betterdocker/snapshots/$id/$name/manifest.json");
        die "Retained snapshot ownership changed\n" unless $manifest->{id} eq $id && $manifest->{name} eq $name
            && $manifest->{native_storage}->{volid} eq $volid && $manifest->{native_storage}->{name} eq $name;
    }
    die "Storage snapshots unsupported\n" unless $scfg->{type} eq 'lvmthin' || $scfg->{type} eq 'zfspool';
    my (undef, $volume) = PVE::Storage::parse_volume_id($volid);
    my $exists;
    if ($scfg->{type} eq 'zfspool') {
        $exists = PVE::Storage::volume_snapshot_info($cfg, $volid)->{$name};
    } else {
        my $volumes = PVE::Storage::LVMPlugin::lvm_list_volumes($scfg->{vgname});
        $exists = $volumes->{$scfg->{vgname}}->{"snap_${volume}_$name"};
    }
    die "Storage snapshot does not exist\n" if !$exists && ($action eq 'rollback' || $action eq 'rollback-check' || $action eq 'snapshot-export');
    if ($action eq 'snapshot-export' || $action eq 'snapshot-export-cleanup') {
        my $output = $p->{output};
        die "Invalid snapshot export path\n" unless $output =~ m!\A/var/lib/betterdocker/transfers/[0-9a-f]{32}/state/history/[A-Za-z][A-Za-z0-9_-]{0,39}/disk\.raw\z!;
        my $parent = $output;
        $parent =~ s!/[^/]+$!!;
        while ($parent ne '/') {
            my @stat = lstat($parent);
            die "Unsafe snapshot export directory\n" unless @stat && !-l $parent && -d $parent && $stat[4] == 0 && ($stat[2] & 022) == 0;
            $parent =~ s!/[^/]+$!!;
            $parent ||= '/';
        }
        my $journal = "$output.snapdev";
        if (-e $journal) {
            die "Unsafe snapshot export journal\n" if -l $journal;
            my $saved = read_json($journal);
            die "Invalid snapshot export journal\n" unless $scfg->{type} eq 'zfspool' && $saved->{volid} eq $volid && $saved->{previous} =~ /\A(?:hidden|visible)\z/;
            PVE::Tools::run_command(['zfs', 'set', "snapdev=$saved->{previous}", "$scfg->{pool}/$volume"]);
            unlink $journal or die $!;
        }
        if ($action eq 'snapshot-export-cleanup') { print "{}\n"; exit; }
        sysopen(my $export, $output, O_WRONLY|O_CREAT|O_EXCL, 0600) or die "Export file already exists: $!\n";
        close $export;
        my $previous;
        if ($scfg->{type} eq 'zfspool') {
            my $dataset = "$scfg->{pool}/$volume";
            PVE::Tools::run_command(['zfs', 'get', '-H', '-o', 'value', 'snapdev', $dataset], outfunc => sub { $previous = $_[0]; });
            sysopen(my $saved, $journal, O_WRONLY|O_CREAT|O_EXCL, 0600) or die $!;
            print $saved encode_json({ volid => $volid, previous => $previous }); close $saved or die $!;
            PVE::Tools::run_command(['zfs', 'set', 'snapdev=visible', $dataset]);
            PVE::Tools::run_command(['udevadm', 'settle']);
        } else { PVE::Storage::activate_volumes($cfg, [$volid], $name); }
        my $path = PVE::Storage::path($cfg, $volid, $name);
        eval { PVE::Tools::run_command(['dd', "if=$path", "of=$output", 'bs=4M', 'conv=sparse', 'status=none']); chmod 0600, $output; };
        my $error = $@;
        if (defined($previous)) { PVE::Tools::run_command(['zfs', 'set', "snapdev=$previous", "$scfg->{pool}/$volume"]); unlink $journal or die $!; }
        die $error if $error;
        print "{}\n"; exit;
    }
    if ($action eq 'snapshot') { PVE::Storage::volume_snapshot($cfg, $volid, $name); }
    elsif ($action eq 'rollback') { PVE::Storage::volume_snapshot_rollback($cfg, $volid, $name); }
    elsif ($action eq 'snapshot-delete') {
        PVE::Storage::volume_snapshot_delete($cfg, $volid, $name, 0) if $exists;
    }
    else { PVE::Storage::volume_rollback_is_possible($cfg, $volid, $name); }
} elsif ($action eq 'free' || $action eq 'free-check') {
    my %planned;
    if ($action eq 'free-check' && exists($p->{snapshots})) {
        die "Invalid planned snapshots\n" unless ref($p->{snapshots}) eq 'ARRAY';
        for my $name (@{$p->{snapshots}}) {
            die "Invalid planned snapshot\n" unless $name =~ /\A[a-zA-Z][a-zA-Z0-9_-]{0,39}\z/;
            my $manifest = read_json("/var/lib/betterdocker/snapshots/$id/$name/manifest.json");
            die "Planned snapshot ownership changed\n" unless $manifest->{id} eq $id && $manifest->{name} eq $name
                && $manifest->{native_storage}->{volid} eq $volid && $manifest->{native_storage}->{name} eq $name;
            $planned{$name} = 1;
        }
    }
    if ($scfg->{type} eq 'lvmthin') {
        my (undef, $volume) = PVE::Storage::parse_volume_id($volid);
        my $volumes = PVE::Storage::LVMPlugin::lvm_list_volumes($scfg->{vgname});
        my $prefix = "snap_${volume}_";
        die "Remove storage snapshots before freeing the disk\n" if grep { index($_, $prefix) == 0 && !$planned{substr($_, length($prefix))} } keys %{$volumes->{$scfg->{vgname}} // {}};
    } elsif ($scfg->{type} eq 'zfspool' && -e PVE::Storage::path($cfg, $volid)) {
        my $snapshots = PVE::Storage::volume_snapshot_info($cfg, $volid);
        die "Remove storage snapshots before freeing the disk\n" if grep { !$planned{$_} } keys %$snapshots;
    }
    if ($action eq 'free-check') { print "{}\n"; exit; }
    my $path = PVE::Storage::path($cfg, $volid);
    my $exists = -e $path;
    if ($scfg->{type} eq 'lvmthin') {
        my (undef, $volume) = PVE::Storage::parse_volume_id($volid);
        my $volumes = PVE::Storage::LVMPlugin::lvm_list_volumes($scfg->{vgname});
        $exists = $volumes->{$scfg->{vgname}}->{$volume};
    }
    if ($scfg->{type} eq 'zfspool') {
        my $volumes = PVE::Storage::vdisk_list($cfg, $sid, $vmid);
        $exists = grep { $_->{volid} eq $volid } @{$volumes->{$sid} // []};
    }
    PVE::Storage::vdisk_free($cfg, $volid) if $exists;
    print "{}\n"; exit;
} elsif ($action ne 'inspect') { die "Invalid storage operation\n"; }
PVE::Storage::activate_volumes($cfg, [$volid]);
my ($size) = PVE::Storage::volume_size_info($cfg, $volid);
$result = {volid => $volid, path => scalar(PVE::Storage::path($cfg, $volid)), size_mib => int($size / 1048576),
    storage => $sid, type => $scfg->{type}, native_snapshots => (($scfg->{type} eq 'lvmthin' || $scfg->{type} eq 'zfspool') ? JSON::true : JSON::false)};
print encode_json($result), "\n";
