#!/usr/bin/perl
# Root-only adapter to PVE's snippet validation and guest hook executor.
use strict;
use warnings;
use JSON qw(decode_json encode_json);
use PVE::GuestHelpers;
use PVE::Cluster;
use PVE::INotify;
use PVE::RPCEnvironment;
die "Root required\n" if $>;
local $/;
my $request = decode_json(<STDIN>);
my $script = $request->{hookscript};
if ($request->{action} eq 'run') {
    my $vmid = $request->{vmid};
    my $phase = $request->{phase};
    die "Invalid guest ID\n" unless $vmid =~ /\A([1-9][0-9]{2,8})\z/;
    $vmid = $1;
    die "Invalid hook phase\n" unless $phase =~ /\A(pre-start|post-start|pre-stop|post-stop)\z/;
    $phase = $1;
    PVE::GuestHelpers::exec_hookscript({ hookscript => $script }, $vmid, $phase, $phase eq 'pre-start');
} elsif ($request->{action} eq 'check') {
    PVE::GuestHelpers::check_hookscript($script);
} else {
    die "Invalid hook operation\n";
}
print encode_json({ ok => 1 }) . "\n";
