#!/usr/bin/perl
# Restore a DCT archive through the configured PVE storage/PBS credentials.
use strict;
use warnings;
use File::Temp qw(tempdir tempfile);
use File::Path qw(remove_tree);
use JSON qw(decode_json);
use PVE::Storage;
use PVE::Storage::PBSPlugin;
use PVE::Tools;
use PVE::BetterDocker::Backup;

my ($action, $volid, $identifier, $node, $options) = @ARGV;
die "Usage: backup.pl restore <volume> <project> <node> <options>\n" unless
    defined($options) && $action eq 'restore' && $identifier =~ /\A[0-9a-f]{32}\z/;
die "Root required\n" if $>;
PVE::BetterDocker::Backup::hook_storage();
my $cfg = PVE::Storage::config();
my ($storage, $volume) = PVE::Storage::parse_volume_id($volid);
my $scfg = PVE::Storage::storage_config($cfg, $storage);
PVE::Storage::activate_storage($cfg, $storage);
my $parent = tempdir('betterdocker-restore-XXXXXX', DIR => '/var/tmp', CLEANUP => 1);
chmod 0700, $parent;
my $state = "$parent/state";
my $helper = '/usr/local/lib/betterdocker/guest_state.py';
eval {
    if ($scfg->{type} eq 'pbs') {
        die "Not a DCT PBS backup\n" unless $volume =~
            m!\Abackup/(host/dct-[1-9][0-9]{2,8}/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)\z!;
        my $snapshot = $1;
        PVE::Storage::PBSPlugin::run_raw_client_cmd($scfg, $storage, 'restore', [$snapshot, 'dct.pxar', $state]);
    } else {
        my $archive = PVE::Storage::path($cfg, $volid);
        my $info = PVE::Storage::archive_info($archive);
        die "Not a DCT file backup\n" unless $info->{type} eq 'docker' && $info->{format} eq 'tar';
        my $plain = $archive;
        if (my $compression = $info->{compression}) {
            my $commands = { gz => ['gzip', '-dc'], zst => ['zstd', '-q', '-dc'],
                lzo => ['lzop', '-dc'], bz2 => ['bzip2', '-dc'] };
            my $command = $commands->{$compression} or die "Unsupported DCT backup compression\n";
            my ($fh, $path) = tempfile('archive-XXXXXX', DIR => $parent);
            chmod 0600, $path;
            PVE::Tools::run_command([@$command, '--', $archive], output => '>&' . fileno($fh));
            close $fh;
            $plain = $path;
        }
        PVE::Tools::run_command([$helper, 'unpack', $plain, $state]);
    }
    PVE::Tools::run_command([$helper, 'restore', $state, $identifier, $node, $options]);
};
my $error = $@;
remove_tree($parent);
die $error if $error;
