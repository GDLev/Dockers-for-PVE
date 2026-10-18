#!/usr/bin/perl
# Native PVE storage lookup and PBS transport for the bounded restore worker.
use strict;
use warnings;
use JSON qw(encode_json);
use PVE::Storage;
use PVE::Storage::PBSPlugin;
use PVE::BetterDocker::Backup;

my ($action, $volid, $target) = @ARGV;
die "Usage: backup.pl source <volume> | fetch <volume> <directory>\n" unless
    defined($volid) && ($action eq 'source' || $action eq 'fetch' && defined($target));
die "Root required\n" if $>;
PVE::BetterDocker::Backup::hook_storage();
my $cfg = PVE::Storage::config();
my ($storage, $volume) = PVE::Storage::parse_volume_id($volid);
my $scfg = PVE::Storage::storage_config($cfg, $storage);
PVE::Storage::activate_storage($cfg, $storage);
if ($scfg->{type} eq 'pbs') {
    die "Not a DCT PBS backup\n" unless $volume =~
        m!\Abackup/(host/dct-[1-9][0-9]{2,8}/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)\z!;
    my $snapshot = $1;
    if ($action eq 'source') {
        print encode_json({ type => 'pbs' });
    } else {
        PVE::Storage::PBSPlugin::run_raw_client_cmd($scfg, $storage, 'restore', [$snapshot, 'dct.pxar', $target]);
    }
} else {
    die "Only PBS uses the fetch transport\n" unless $action eq 'source';
    my $archive = PVE::Storage::path($cfg, $volid);
    my $info = PVE::Storage::archive_info($archive);
    die "Not a DCT file backup\n" unless $info->{type} eq 'docker' && $info->{format} eq 'tar';
    print encode_json({ type => 'file', path => $archive, compression => $info->{compression} });
}
