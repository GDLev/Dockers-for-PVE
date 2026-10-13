package PVE::API2::BetterDocker::Inventory;

use strict;
use warnings;
use JSON qw(decode_json);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'recover', path => '{id}/recover', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Recover interrupted local configuration operations without image or snapshot rollback.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            my $rpc = PVE::RPCEnvironment::get();
            return $rpc->fork_worker('betterdockersettings', PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'recover', $p->{id}, $p->{node}]);
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'list', path => '', method => 'GET', protected => 1, proxyto => 'node',
        description => 'List host Docker projects on this node.', permissions => $root,
        parameters => { additionalProperties => 0, properties => { node => $node, diagnostics => { type => 'boolean', optional => 1, default => 0 } } },
        returns => { type => 'array', items => { type => 'object' } },
        code => sub { my ($param) = @_;
            return [decode_json(PVE::API2::BetterDocker::output(['diagnostics', $param->{node}]))] if $param->{diagnostics};
            return [grep { $_->{node} eq $param->{node} && PVE::API2::BetterDocker::capabilities($_->{id}, $_->{node})->{audit} } @{PVE::API2::BetterDocker::summaries()}]; },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'detail', path => '{id}', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Get a Docker deployment and its network settings.', permissions => $root,
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'object' },
        code => sub { my ($param) = @_; return decode_json(PVE::API2::BetterDocker::output(['detail', $param->{id}, $param->{node}])); },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'logs', path => '{id}/logs', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read bounded project, service or container logs.', permissions => $root,
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
            service => { type => 'string', maxLength => 128, optional => 1 },
            container => { type => 'string', pattern => '^[0-9a-f]{64}$', optional => 1 },
            tail => { type => 'integer', minimum => 1, maximum => 5000, default => 200, optional => 1 } } },
        returns => { type => 'string' },
        code => sub { my ($p) = @_; return PVE::API2::BetterDocker::output(['logs', $p->{id}, $p->{node}, $p->{service} // '', $p->{container} // '', $p->{tail} // 200]); },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'management', path => '{id}/management', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read service resources, mounts, container state and live usage.', permissions => $root,
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'object' },
        code => sub { my ($param) = @_; my $info = decode_json(PVE::API2::BetterDocker::output(['management', $param->{id}, $param->{node}]));
            $info->{capabilities} = PVE::API2::BetterDocker::capabilities($param->{id}, $param->{node});
            $info->{ha} = PVE::HA::Config::get_service_status("docker:" . PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}));
            my $op = $info->{operation} // {};
            my $owner = $op->{user} // '';
            my $actor = $ENV{BETTERDOCKER_TASK_USER};
            $owner =~ s/!.*$//; $actor =~ s/!.*$//;
            $info->{capabilities}->{overrule_shutdown} = (!$info->{ha}->{managed} && $op->{active} && $op->{action} eq 'shutdown' &&
                $info->{capabilities}->{power} && ($ENV{BETTERDOCKER_TASK_SYS_MODIFY} eq '1' ||
                ($owner ne '' && $owner eq $actor))) ? 1 : 0;
            delete $info->{deployment} unless $info->{capabilities}->{configure};
            delete $info->{image_update}->{token} unless $info->{capabilities}->{images};
            if (!$info->{capabilities}->{configure}) {
                for my $service (@{$info->{services} // []}) {
                    $service->{mounts} = [map { my $mount = $_;
                        +{ map { exists($mount->{$_}) ? ($_ => $mount->{$_}) : () } qw(type source target read_only) }
                    } @{$service->{mounts} // []}];
                }
            }
            delete $info->{digest} unless $info->{capabilities}->{settings} || $info->{capabilities}->{clone} || $info->{capabilities}->{migrate} || $info->{capabilities}->{template};
            return $info; },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'capabilities', path => '{id}/capabilities', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read permissions and current operation state for this project.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'object' },
        code => sub {
            my ($p) = @_;
            my $state = decode_json(PVE::API2::BetterDocker::output(['access-state', $p->{id}, $p->{node}]));
            $state->{capabilities} = PVE::API2::BetterDocker::capabilities($p->{id}, $p->{node});
            $state->{ha} = PVE::HA::Config::get_service_status("docker:" . PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}));
            my $op = $state->{operation} // {};
            my $owner = $op->{user} // ''; my $actor = $ENV{BETTERDOCKER_TASK_USER};
            $owner =~ s/!.*$//; $actor =~ s/!.*$//;
            $state->{capabilities}->{overrule_shutdown} = (!$state->{ha}->{managed} && $op->{active} && $op->{action} eq 'shutdown' &&
                !$op->{service} && $state->{capabilities}->{power} &&
                ($ENV{BETTERDOCKER_TASK_SYS_MODIFY} eq '1' || ($owner ne '' && $owner eq $actor))) ? 1 : 0;
            return $state;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'rrddata', path => '{id}/rrddata', method => 'GET', protected => 1, proxyto => 'node', permissions => $root,
        description => 'Read Docker metrics from native PVE RRD archives.',
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, timeframe => { type => 'string', enum => ['hour', 'day', 'week', 'month', 'year', 'decade'] },
            cf => { type => 'string', enum => ['AVERAGE', 'MAX'], optional => 1, default => 'AVERAGE' },
        } },
        returns => { type => 'array', items => { type => 'object' } },
        code => sub { my ($p) = @_; return decode_json(PVE::API2::BetterDocker::output(['rrddata', $p->{id}, $p->{node}, $p->{timeframe}, $p->{cf} // 'AVERAGE'])); },
    });
}

1;
