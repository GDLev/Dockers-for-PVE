package PVE::API2::BetterDocker::Power;

use strict;
use warnings;
use JSON qw(decode_json);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'shutdown', path => '{id}/shutdown', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Graceful shutdown with optional forced stop after timeout.', permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id,
            timeout => { type => 'integer', minimum => 0, maximum => 86400, optional => 1, default => 180 },
            'force-stop' => { type => 'boolean', optional => 1, default => 1 },
        } }, returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            if (PVE::API2::BetterDocker::ha_managed($param->{id}, $param->{node})) {
                return PVE::API2::BetterDocker->map_method_by_name('action')->{code}->({ id => $param->{id}, node => $param->{node},
                    action => 'shutdown', timeout => $param->{timeout} // 180 });
            }
            my $upid = PVE::API2::BetterDocker::node_stop(PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $param->{node}, $param->{timeout} // 180, $param->{'force-stop'} // 1);
            return $upid if defined($upid);
            return PVE::API2::BetterDocker->map_method_by_name('action')->{code}->({ id => $param->{id}, node => $param->{node},
                action => 'shutdown', timeout => $param->{timeout} // 180 });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'action', path => '{id}/action', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Control project power or remove a project. Shutdown is graceful; force-stop kills immediately. Legacy stop/restart retain Docker semantics.', permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, action => { type => 'string', enum => ['start', 'stop', 'restart', 'shutdown', 'reboot', 'force-stop', 'delete'] },
            timeout => { type => 'integer', minimum => 0, maximum => 86400, optional => 1, default => 60 },
            'overrule-shutdown' => { type => 'boolean', optional => 1, default => 0 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            my $rpcenv = PVE::RPCEnvironment::get();
            $param->{action} =~ /\A(start|stop|restart|shutdown|reboot|force-stop|delete)\z/ or die "Invalid Docker action\n";
            my $action = $1;
            die "can't remove DCT - protection mode enabled\n" if $action eq 'delete' && decode_json(PVE::API2::BetterDocker::output(['detail', $param->{id}, $param->{node}]))->{protection};
            die "Overrule is only available for Stop\n" if $param->{'overrule-shutdown'} && $action ne 'force-stop';
            if (PVE::API2::BetterDocker::ha_managed($param->{id}, $param->{node}) && $rpcenv->{type} ne 'ha') {
                die "Remove the DCT from HA before deleting it\n" if $action eq 'delete';
                die "Overrule is not applicable to HA resources\n" if $param->{'overrule-shutdown'};
                if ($action =~ /^(?:start|stop|shutdown|force-stop)$/) {
                    my $vmid = PVE::API2::BetterDocker::task_id($param->{id}, $param->{node});
                    my $command = $action eq 'start' ? ['ha-manager', 'set', "docker:$vmid", '--state', 'started'] :
                        ['ha-manager', 'crm-command', 'stop', "docker:$vmid", $action eq 'force-stop' ? 0 : $param->{timeout} // 60];
                    return $rpcenv->fork_worker($action eq 'start' ? 'hastart' : 'hastop', $vmid, $rpcenv->get_user(),
                        sub { PVE::Tools::run_command($command); });
                }
            }
            my $ha_agent = ($rpcenv->{type} // '') eq 'ha';
            my $vmid = PVE::API2::BetterDocker::task_id($param->{id}, $param->{node});
            (my $task_action = $action) =~ s/-//g;
            return $rpcenv->fork_worker('betterdocker' . $task_action, PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
                if ($ha_agent) {
                    PVE::Tools::run_command(['/usr/local/lib/betterdocker/ha.py', 'power', $param->{id}, $param->{node}, $action, $param->{timeout} // 60]);
                } else {
                    PVE::Tools::run_command([$helper, 'action', $param->{id}, $param->{node}, $action, $param->{timeout} // 60, $param->{'overrule-shutdown'} ? 1 : 0]);
                    PVE::AccessControl::remove_vm_access($vmid) if $action eq 'delete';
                }
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'destroy', path => '{id}', method => 'DELETE', protected => 1, proxyto => 'node', permissions => $root,
        description => 'Remove the project, optionally deleting its owned data.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
            'destroy-owned-data' => {type => 'boolean', optional => 1, default => 0, description => 'Remove owned disks, local volumes and project files. External mounts are retained.'} } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            my $rpcenv = PVE::RPCEnvironment::get();
            die "can't remove DCT - protection mode enabled\n" if decode_json(PVE::API2::BetterDocker::output(['detail', $param->{id}, $param->{node}]))->{protection};
            if ($param->{'destroy-owned-data'}) {
                my $cfg = decode_json(PVE::API2::BetterDocker::output(['storage-config', $param->{id}, $param->{node}]));
                for my $volid (@{$cfg->{unused} // []}, $cfg->{volid} // ()) {
                    my ($sid) = PVE::Storage::parse_volume_id($volid);
                    $rpcenv->check($rpcenv->get_user(), "/storage/$sid", ['Datastore.AllocateSpace']);
                }
            }
            my $vmid = PVE::API2::BetterDocker::task_id($param->{id}, $param->{node});
            return $rpcenv->fork_worker('betterdockerdelete', $vmid, $rpcenv->get_user(), sub {
                PVE::Tools::run_command([$helper, 'action', $param->{id}, $param->{node}, 'delete', 60, 0, $param->{'destroy-owned-data'} ? 1 : 0]);
                PVE::AccessControl::remove_vm_access($vmid);
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'service_action', path => '{id}/service-action', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Operate one Compose service without restarting dependencies.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
            service => { type => 'string', maxLength => 128 }, action => { type => 'string', enum => ['start', 'stop', 'restart', 'shutdown', 'reboot', 'force-stop'] },
            timeout => { type => 'integer', minimum => 0, maximum => 86400, optional => 1, default => 60 },
            'overrule-shutdown' => { type => 'boolean', optional => 1, default => 0 } } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            my $rpc = PVE::RPCEnvironment::get();
            $p->{action} =~ /\A(start|stop|restart|shutdown|reboot|force-stop)\z/ or die "Invalid service action\n";
            my $action = $1;
            die "Overrule is only available for Stop\n" if $p->{'overrule-shutdown'} && $action ne 'force-stop';
            (my $task_action = $action) =~ s/-//g;
            return $rpc->fork_worker('betterdocker' . $task_action, PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'service-action', $p->{id}, $p->{node}, $p->{service}, $action, $p->{timeout} // 60, $p->{'overrule-shutdown'} ? 1 : 0]);
            });
        },
    });
}

1;
