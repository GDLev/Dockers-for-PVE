package PVE::API2::BetterDocker::Snapshots;

use strict;
use warnings;
use JSON qw(decode_json);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'snapshots', path => '{id}/snapshots', method => 'GET', protected => 1, proxyto => 'node',
        description => 'List complete private DCT snapshots without deployment secrets.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'array', items => { type => 'object' } },
        code => sub { my ($p) = @_; return decode_json(PVE::API2::BetterDocker::output(['snapshots', $p->{id}, $p->{node}])); },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'snapshot', path => '{id}/snapshot', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Create, restore or delete a DCT snapshot. Live snapshots freeze and resume services. Rollback stops services and creates no other snapshot.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
            action => { type => 'string', enum => ['create', 'restore', 'delete'] },
            snapname => { type => 'string', pattern => '[A-Za-z][A-Za-z0-9_-]{0,39}', maxLength => 40 },
            description => { type => 'string', optional => 1, maxLength => 4096 } } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            my $rpc = PVE::RPCEnvironment::get();
            $p->{action} =~ /\A(create|restore|delete)\z/ or die "Invalid snapshot action\n";
            my $action = $1;
            # Names and descriptions are passed as argv, never through a shell or a public metadata file.
            return $rpc->fork_worker('betterdockersnapshot' . $action, PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'snapshot', $p->{id}, $p->{node}, $action, $p->{snapname}, $p->{description} // '']);
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'snapshot_config', path => '{id}/snapshot/{snapname}/config', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read public settings stored in a DCT snapshot.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
            snapname => { type => 'string', pattern => '[A-Za-z][A-Za-z0-9_-]{0,39}', maxLength => 40 } } },
        returns => { type => 'object' },
        code => sub { my ($p) = @_; return decode_json(PVE::API2::BetterDocker::output(['snapshot-config', $p->{id}, $p->{node}, $p->{snapname}])); },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'snapshot_edit', path => '{id}/snapshot/{snapname}/config', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Edit a DCT snapshot description without changing its saved contents.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
            snapname => { type => 'string', pattern => '[A-Za-z][A-Za-z0-9_-]{0,39}', maxLength => 40 },
            description => { type => 'string', maxLength => 4096 },
            digest => { type => 'string', pattern => '^[a-f0-9]{64}$', optional => 1 } } },
        returns => { type => 'null' },
        code => sub { my ($p) = @_; PVE::API2::BetterDocker::output(['snapshot-edit', $p->{id}, $p->{node}, $p->{snapname}, $p->{description}, $p->{digest} // '']); return undef; },
    });
}

1;
