package PVE::API2::BetterDocker::Storage;

use strict;
use warnings;
use JSON qw(decode_json);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'set_disk', path => '{id}/disks/{disk}', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Create, grow, move or detach an additional managed disk on a stopped DCT.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
            disk => { type => 'string', pattern => '^mp([1-9]|1[0-5])$' },
            service => { type => 'string', optional => 1, maxLength => 128 },
            target => { type => 'string', optional => 1, maxLength => 4096 },
            storage => { type => 'string', optional => 1, format => 'pve-storage-id' },
            size_mib => { type => 'integer', optional => 1, minimum => 256, maximum => 1048576 },
            read_only => { type => 'boolean', optional => 1 }, backup => { type => 'boolean', optional => 1 },
            detach => { type => 'boolean', optional => 1 } } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            PVE::API2::BetterDocker::check_digest($p);
            my $rpc = PVE::RPCEnvironment::get();
            my $state = decode_json(PVE::API2::BetterDocker::output(['storage-config', $p->{id}, $p->{node}]));
            my $storage = $p->{storage} // $state->{additional}->{$p->{disk}}->{storage};
            $rpc->check($rpc->get_user(), '/storage/' . $storage, ['Datastore.AllocateSpace']) if $storage;
            my %values = map { exists($p->{$_}) ? ($_ => $p->{$_}) : () } qw(service target storage size_mib read_only backup detach);
            $values{$_} = 0 + $values{$_} for grep { exists($values{$_}) } qw(size_mib read_only backup detach);
            my $payload = JSON::encode_json(\%values);
            return $rpc->fork_worker('betterdockersettings', PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'disk', $p->{id}, $p->{node}, $p->{disk}, $payload, $p->{digest} // '']);
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'storage_limit', path => '{id}/storage-limit', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Create or enlarge the bounded project data disk; services must be stopped.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
            size_mib => { type => 'integer', minimum => 256, maximum => 1048576 },
            storage => { type => 'string', format => 'pve-storage-id', optional => 1 } } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            PVE::API2::BetterDocker::check_digest($p);
            my $rpc = PVE::RPCEnvironment::get();
            my $storage = $p->{storage} // decode_json(PVE::API2::BetterDocker::output(['storage-config', $p->{id}, $p->{node}]))->{storage};
            $rpc->check($rpc->get_user(), '/storage/' . $storage, ['Datastore.AllocateSpace']) if $storage;
            return $rpc->fork_worker('betterdockersettings', PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'storage-limit', $p->{id}, $p->{node}, $p->{size_mib}, $p->{digest} // '', $p->{storage} // '']);
            });
        },
    });
}

1;
