package PVE::API2::BetterDocker::Network;

use strict;
use warnings;
use JSON qw(decode_json encode_json);
use File::Temp qw(tempfile);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'network_config', path => '{id}/network', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read the DCT IPv4 endpoint configuration.', permissions => $root,
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
            ipv4mode => { type => 'string', enum => ['static', 'dhcp'], optional => 1 },
            cidr => { type => 'string', maxLength => 64, optional => 1 },
            hwaddr => { type => 'string', maxLength => 17, optional => 1 },
            mtu => { type => 'integer', minimum => 0, maximum => 9216, optional => 1 },
            firewall => { type => 'boolean', optional => 1 },
            revert => { type => 'boolean', optional => 1, description => 'Discard pending net0 changes.' },
            digest => { type => 'string', pattern => '^[a-f0-9]{64}$', optional => 1 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            PVE::API2::BetterDocker::check_digest($param);
            my %values = map { exists($param->{$_}) ? ($_ => $param->{$_}) : () } qw(bridge ipv4mode cidr hwaddr mtu firewall revert);
            $values{firewall} = $values{firewall} ? 1 : 0 if exists($values{firewall});
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
