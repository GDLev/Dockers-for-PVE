package PVE::BetterDocker::Backup;

use strict;
use warnings;
use JSON qw(decode_json);
use POSIX qw(strftime);
use PVE::Storage;
use PVE::APIClient::LWP;
use PVE::AccessControl;
use PVE::Storage::PBSPlugin;
use PVE::API2::Storage::PruneBackups;

sub pbs_connection {
    my ($scfg, $password) = @_;
    my $username = $scfg->{username} // 'root@pam';
    my $params = {};
    if (my $token = PVE::AccessControl::pve_verify_tokenid($username, 1)) {
        $params->{apitoken} = "PBSAPIToken=$token:$password";
    } else {
        $params->{username} = $username;
        $params->{password} = $password;
    }
    $params->{cached_fingerprints}->{uc($scfg->{fingerprint})} = 1 if $scfg->{fingerprint};
    return PVE::APIClient::LWP->new(%$params, host => $scfg->{server}, port => $scfg->{port} // 8007,
        timeout => 120, cookie_name => 'PBSAuthCookie');
}

sub files_encrypted {
    my ($files) = @_;
    my $content = [grep { $_->{filename} ne 'client.log.blob' && $_->{filename} ne 'index.json.blob' } @{$files // []}];
    return @$content && !grep { ($_->{'crypt-mode'} // '') ne 'encrypt' } @$content;
}

# Keep Docker ownership out of native VM/CT consumers until they explicitly opt in.
sub inventory {
    my ($native) = @_;
    my $result = { %$native, ids => { %{$native->{ids} // {}} } };
    my $directory = '/etc/pve/betterdocker';
    if (opendir(my $dh, $directory)) {
        while (my $file = readdir($dh)) {
            next unless $file =~ /\A([0-9a-f]{32}\.json)\z/;
            my $path = "$directory/$1";
            open(my $fh, '<', $path) or die "Cannot read DCT inventory: $!\n";
            local $/;
            my $info = decode_json(<$fh>);
            next unless ($info->{vmid} // '') =~ /\A([1-9][0-9]{2,8})\z/;
            my $vmid = $1;
            die "Guest ID collision for DCT $vmid\n" if $result->{ids}->{$vmid};
            $result->{ids}->{$vmid} = { type => 'docker', node => $info->{node},
                id => $info->{id}, name => $info->{name}, template => $info->{template} // 0 };
        }
        closedir($dh);
    }
    return $result;
}

my (%inventory_callers, $inventory_installed);
sub hook_inventory {
    $inventory_callers{$_} = 1 for @_;
    return if $inventory_installed++;
    my $native = \&PVE::Cluster::get_vmlist;
    no warnings 'redefine';
    *PVE::Cluster::get_vmlist = sub {
        my $list = $native->(@_);
        return $inventory_callers{scalar caller} ? inventory($list) : $list;
    };
}

my $installed;
sub hook_storage {
    return if $installed++;
    no warnings 'redefine';
    for my $name (qw(dryrun delete)) {
        my $method = PVE::API2::Storage::PruneBackups->map_method_by_name($name);
        my $property = $method->{parameters}->{properties}->{type};
        my $types = $property->{enum};
        push @$types, 'docker' unless grep { $_ eq 'docker' } @$types;
        $property->{description} = "Only consider backups for guests of this type: qemu, lxc or docker.";
    }
    my $native_extract = \&PVE::Storage::extract_vzdump_config;
    *PVE::Storage::extract_vzdump_config = sub {
        my ($cfg, $volid) = @_;
        my ($sid, $name) = PVE::Storage::parse_volume_id($volid);
        my $scfg = PVE::Storage::storage_config($cfg, $sid);
        if ($scfg->{type} eq 'pbs' && $name =~ m!\Abackup/(host/dct-[1-9][0-9]{2,8}/[0-9TZ:-]+)\z!) {
            my $snapshot = $1;
            my $text = '';
            PVE::Storage::PBSPlugin::run_raw_client_cmd($scfg, $sid, 'restore', [$snapshot, 'dct.conf', '-'],
                outfunc => sub { $text .= "$_[0]\n"; });
            return $text;
        }
        if ($name =~ m!\Abackup/vzdump-docker-[1-9][0-9]{2,8}-!) {
            my $archive = PVE::Storage::path($cfg, $volid);
            return PVE::Storage::extract_vzdump_config_tar($archive, qr!^(\./configuration/request\.json)$!);
        }
        return $native_extract->(@_);
    };
    my $native_parse = \&PVE::Storage::PBSPlugin::parse_volname;
    *PVE::Storage::PBSPlugin::parse_volname = sub {
        my @result = $native_parse->(@_);
        if ($_[1] =~ m!\Abackup/host/dct-([1-9][0-9]{2,8})/!) {
            $result[2] = int($1);
        }
        return wantarray ? @result : $result[-1];
    };
    my $native_list = \&PVE::Storage::PBSPlugin::list_volumes;
    *PVE::Storage::PBSPlugin::list_volumes = sub {
        my ($class, $storeid, $scfg, $vmid, $types) = @_;
        my $result = $native_list->(@_);
        return $result unless grep { $_ eq 'backup' } @$types;
        my $password = PVE::Storage::PBSPlugin::pbs_get_password($scfg, $storeid);
        my $connection = pbs_connection($scfg, $password);
        my $param = { 'backup-type' => 'host' };
        $param->{'backup-id'} = "dct-$vmid" if defined($vmid);
        $param->{ns} = $scfg->{namespace} if defined($scfg->{namespace});
        my $items = $connection->get("/api2/json/admin/datastore/$scfg->{datastore}/snapshots", $param);
        for my $item (@$items) {
            next unless $item->{'backup-type'} eq 'host' && $item->{'backup-id'} =~ /\Adct-([1-9][0-9]{2,8})\z/;
            my $id = int($1);
            next if defined($vmid) && $id != $vmid;
            my $time = $item->{'backup-time'};
            my $snapshot = strftime('%FT%TZ', gmtime($time));
            my $info = { volid => "$storeid:backup/host/dct-$id/$snapshot", format => 'pbs-host',
                vmid => $id, ctime => $time, size => $item->{size} // 1, content => 'backup', subtype => 'docker' };
            $info->{verification} = $item->{verification} if defined($item->{verification});
            $info->{notes} = $item->{comment} if defined($item->{comment});
            $info->{protected} = 1 if $item->{protected};
            if (defined($item->{fingerprint})) {
                $info->{encrypted} = $item->{fingerprint};
            } elsif (files_encrypted($item->{files})) {
                $info->{encrypted} = '1';
            }
            push @$result, $info;
        }
        return $result;
    };
    my $docker_list = \&PVE::Storage::PBSPlugin::list_volumes;
    my $native_prune = \&PVE::Storage::PBSPlugin::prune_backups;
    *PVE::Storage::PBSPlugin::prune_backups = sub {
        my ($class, $scfg, $storeid, $keep, $vmid, $type, $dryrun, $log) = @_;
        my $result = [];
        if (!defined($type) || $type ne 'docker') {
            # The native implementation builds host/<numeric-id> from every listed entry.
            local *PVE::Storage::PBSPlugin::list_volumes = $native_list;
            $result = $native_prune->($class, $scfg, $storeid, { %$keep }, $vmid, $type, $dryrun, $log);
        }
        return $result if defined($type) && $type ne 'docker';
        my $groups = {};
        for my $item (@{$docker_list->($class, $storeid, $scfg, $vmid, ['backup'])}) {
            $groups->{$item->{vmid}} = 1 if ($item->{subtype} // '') eq 'docker';
        }
        return $result if $keep->{'keep-all'};
        my @options;
        for my $key (sort keys %$keep) {
            next unless $key =~ /\Akeep-(?:last|hourly|daily|weekly|monthly|yearly)\z/ && $keep->{$key};
            push @options, "--$key", $keep->{$key};
        }
        push @options, '--dry-run' if $dryrun;
        for my $id (sort { $a <=> $b } keys %$groups) {
            $log->('info', "pruning DCT backup group host/dct-$id") if $log && !$dryrun;
            my $items = PVE::Storage::PBSPlugin::run_client_cmd($scfg, $storeid, 'prune', ["host/dct-$id", @options]);
            for my $item (@$items) {
                die "Unexpected DCT prune result\n" unless $item->{'backup-type'} eq 'host' &&
                    $item->{'backup-id'} eq "dct-$id" && defined($item->{'backup-time'}) && defined($item->{keep});
                push @$result, { vmid => int($id), type => 'docker', ctime => $item->{'backup-time'},
                    mark => $item->{protected} ? 'protected' : $item->{keep} ? 'keep' : 'remove',
                    volid => "$storeid:backup/host/dct-$id/" . strftime('%FT%TZ', gmtime($item->{'backup-time'})) };
            }
        }
        return $result;
    };
    my $native_archive = \&PVE::Storage::archive_info;
    *PVE::Storage::archive_info = sub {
        my ($path) = @_;
        return $native_archive->(@_) unless $path =~ m!(?:\A|/)vzdump-docker-[1-9][0-9]{2,8}-!;
        (my $native_path = $path) =~ s/vzdump-docker-/vzdump-lxc-/;
        my $info = $native_archive->($native_path);
        $info->{type} = 'docker';
        for my $key (qw(filename logfilename notesfilename)) {
            $info->{$key} =~ s/vzdump-lxc-/vzdump-docker-/ if defined($info->{$key});
        }
        return $info;
    };
    my $native_directory_parse = \&PVE::Storage::Plugin::parse_volname;
    *PVE::Storage::Plugin::parse_volname = sub {
        my @result = $native_directory_parse->(@_);
        $result[2] = int($1) if $_[1] =~ m!\Abackup/vzdump-docker-([1-9][0-9]{2,8})-! && $result[0] eq 'backup';
        return wantarray ? @result : $result[-1];
    };
}

1;
