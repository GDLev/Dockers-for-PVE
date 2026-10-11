#!/usr/bin/perl
use strict;
use warnings;
use JSON qw(decode_json encode_json);
use Storable qw(dclone);
use PVE::Firewall;

# PVE remains the parser for guest files, macros, aliases and security groups.
# The packet-path adapter consumes its expanded rules without modifying PVE's
# own host/VM chains or inventing a second configuration format.
my $request = decode_json(do { local $/; <STDIN> });
my $cluster = PVE::Firewall::load_clusterfw_conf();
my @guests;
for my $value (@{$request->{vmids} // []}) {
    $value =~ /\A([1-9][0-9]{2,8})\z/ or die "Invalid guest ID\n";
    my $vmid = $1;
    my $conf = PVE::Firewall::load_vmfw_conf($cluster, 'vm', $vmid);
    my $sets = {};
    local $SIG{__WARN__} = sub { die @_ };
    for my $scope ($cluster, $conf, $cluster->{sdn}) {
        next unless $scope;
        PVE::Firewall::generate_ipset_chains($sets, $cluster, $scope, {}, $scope->{ipset} // {});
    }
    my %per_interface;
    my @interfaces = @{$request->{interfaces}->{$vmid} // ['net0']};
    for my $interface (@interfaces) {
        die "Invalid interface\n" unless $interface =~ /\Anet(?:[0-9]|1[0-5])\z/;
        for my $family (4, 6) {
            my %directions;
            for my $direction (qw(in out)) {
                my $chain = "d${vmid}$direction";
                my $ruleset = { $chain => [] };
                my $expand;
                $expand = sub {
                    my ($rules, $scope, $seen, $group_iface) = @_;
                    for my $source (@$rules) {
                        next unless $source->{enable};
                        die "Invalid firewall rule for DCT $vmid\n" if $source->{errors};
                        my $iface = $source->{iface} || $group_iface;
                        next if $iface && $iface ne $interface;
                        if ($source->{type} eq 'group') {
                            my $name = $source->{action};
                            die "Recursive security group '$name'\n" if $seen->{$name};
                            my $group = $cluster->{groups}->{$name} // die "Missing security group '$name'\n";
                            $expand->($group, $cluster, { %$seen, $name => 1 }, $iface);
                            next;
                        }
                        next unless $source->{type} eq $direction;
                        next if $source->{ipversion} && $source->{ipversion} != $family;
                        my $rule = dclone($source);
                        die "Unsupported firewall action\n" unless $rule->{action} =~ /\A(?:ACCEPT|DROP|REJECT)\z/;
                        $rule->{logmsg} = "$rule->{action}: ";
                        PVE::Firewall::ruleset_generate_rule($ruleset, $chain, $family, $rule, $cluster, $scope, $vmid);
                    }
                };
                $expand->($conf->{rules} // [], $conf, {});
                $directions{$direction} = $ruleset->{$chain};
            }
            $per_interface{$interface}->{$family} = \%directions;
        }
    }
    my $directions = $per_interface{net0}->{4};
    push @guests, { vmid => 0 + $vmid, options => $conf->{options} // {}, rules => $directions, interfaces => \%per_interface, sets => $sets };
}
my $lograte = PVE::JSONSchema::parse_property_string(
    $PVE::Firewall::cluster_option_properties->{log_ratelimit}->{format},
    $cluster->{options}->{log_ratelimit} // 'enable=1');
print encode_json({ log_ratelimit => $lograte, cluster_enabled => $cluster->{options}->{enable} ? 1 : 0, guests => \@guests });
