package PVE::API2::BetterDocker::Network;

use strict;
use warnings;
use JSON qw(decode_json encode_json);
use File::Temp qw(tempfile);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'network_config', path => '{id}/network', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read per-interface DCT endpoint configuration.', permissions => $root,
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'object' },
        code => sub { my ($param) = @_; return decode_json(PVE::API2::BetterDocker::output(['network-config', $param->{id}, $param->{node}])); },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'set_network', path => '{id}/network', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Change the DCT endpoint; running guests apply pending changes at the next full start or reboot.', permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id,
            bridge => { type => 'string', pattern => '^[a-zA-Z0-9_.-]{1,15}$', optional => 1 },
            ipv4mode => { type => 'string', enum => ['static', 'dhcp', 'none'], optional => 1 },
            cidr => { type => 'string', maxLength => 64, optional => 1 },
            hwaddr => { type => 'string', maxLength => 17, optional => 1 },
            mtu => { type => 'integer', minimum => 0, maximum => 9216, optional => 1 },
            firewall => { type => 'boolean', optional => 1 },
            net => { type => 'string', pattern => '^net([0-9]|1[0-5])$', optional => 1 },
            interfaces => { type => 'string', maxLength => 65536, optional => 1 },
            delete => { type => 'boolean', optional => 1 },
            gw => { type => 'string', maxLength => 64, optional => 1 },
            ipv6mode => { type => 'string', enum => ['static', 'auto', 'dhcp', 'none'], optional => 1 },
            cidr6 => { type => 'string', maxLength => 64, optional => 1 },
            gw6 => { type => 'string', maxLength => 64, optional => 1 },
            tag => { type => 'integer', minimum => 0, maximum => 4094, optional => 1 },
            link_down => { type => 'boolean', optional => 1 },
            rate => { type => 'number', minimum => 0, maximum => 100000, optional => 1 },
            ports => { type => 'string', maxLength => 8192, optional => 1 },
            revert => { type => 'boolean', optional => 1, description => 'Discard pending network changes.' },
            digest => { type => 'string', pattern => '^[a-f0-9]{64}$', optional => 1 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            PVE::API2::BetterDocker::check_digest($param);
            my %values = map { exists($param->{$_}) ? ($_ => $param->{$_}) : () } qw(bridge ipv4mode cidr hwaddr mtu firewall revert net interfaces delete gw ipv6mode cidr6 gw6 tag link_down rate ports);
            $values{interfaces} = decode_json($values{interfaces}) if exists($values{interfaces});
            $values{$_} = $values{$_} ? 1 : 0 for grep { exists($values{$_}) } qw(firewall link_down delete revert);
            if (exists($values{revert})) {
                die "Revert cannot be combined with network settings\n" if !$values{revert} || keys(%values) != 1;
                $values{revert} = 1;
            }
            my ($fh, $path) = tempfile('betterdocker-network-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path;
            print $fh encode_json(\%values); close $fh;
            my $rpcenv = PVE::RPCEnvironment::get();
            my $upid = eval { $rpcenv->fork_worker('betterdockernetwork', PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
                eval { PVE::Tools::run_command([$helper, 'network', $param->{id}, $param->{node}, $path, $param->{digest} // '']); };
                my $err = $@; unlink $path; die $err if $err;
            }); };
            if (my $err = $@) { unlink $path; die $err; }
            return $upid;
        },
    });
}

1;
