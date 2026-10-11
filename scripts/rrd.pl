#!/usr/bin/perl
# Publish Docker measurements through PVE's guest RRD schema and cluster transport.
use strict;
use warnings;
use JSON qw(decode_json encode_json);
use PVE::Cluster;
use PVE::INotify;
use PVE::RRD;
die "Root required\n" if $>;
local $/;
my $p = decode_json(<STDIN>);
my $id = $p->{id};
die "Invalid project\n" unless $id =~ /\A[0-9a-f]{32}\z/;
open(my $fh, '<', "/etc/pve/betterdocker/$id.json") or die "Project does not exist\n";
my $info = decode_json(<$fh>);
die "Wrong node\n" unless $info->{node} eq PVE::INotify::nodename();
my $vmid = $info->{vmid};
die "Invalid guest ID\n" unless $vmid =~ /\A[1-9][0-9]{2,8}\z/;
my $schema = -d '/var/lib/rrdcached/db/pve-vm-9.0' ? 'pve-vm-9.0' : 'pve2.3-vm';
my $rrdname = "$schema/$vmid";
if ($p->{action} eq 'read') {
    die "Invalid chart range\n" unless $p->{timeframe} =~ /\A(?:hour|day|week|month|year|decade)\z/;
    die "Invalid consolidation\n" unless $p->{cf} =~ /\A(?:AVERAGE|MAX)\z/;
    print encode_json(-f "/var/lib/rrdcached/db/$rrdname" ? PVE::RRD::create_rrd_data($rrdname, $p->{timeframe}, $p->{cf}) : []), "\n";
} elsif ($p->{action} eq 'update') {
    exit if $info->{template};
    my $m = $p->{metrics};
    my $running = $info->{status} eq 'running' || $info->{status} eq 'partial';
    my @data = ($m->{uptime} // 0, $info->{name}, $info->{status}, 0, $m->{time},
        $m->{cpus}, $running ? $m->{cpu} : undef, $m->{maxmem}, $running ? $m->{mem} : undef,
        $m->{maxdisk}, $m->{disk}, map { $running ? $m->{$_ . '_total'} : undef } qw(netin netout diskread diskwrite));
    push @data, (undef) x 7 if $schema eq 'pve-vm-9.0';
    # The RRD wire format is colon-separated. Names must not add fields.
    $data[1] =~ s/[:\r\n]/_/g;
    PVE::Cluster::broadcast_rrd($rrdname, join(':', map { defined($_) ? $_ : 'U' } @data));
} else { die "Invalid RRD operation\n"; }
