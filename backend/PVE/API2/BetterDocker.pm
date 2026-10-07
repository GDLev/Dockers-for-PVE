package PVE::API2::BetterDocker;

use strict;
use warnings;
use base qw(PVE::RESTHandler);
use File::Temp qw(tempfile);
use JSON qw(decode_json);
use PVE::RPCEnvironment;
use PVE::Tools;
use PVE::Cluster;
use PVE::AccessControl;
use Fcntl qw(F_GETFD F_SETFD FD_CLOEXEC);

my $helper = '/usr/local/lib/betterdocker/provision.py';
my $node = { type => 'string', format => 'pve-node' };
my $id = { type => 'string', pattern => '^[0-9a-f]{32}$' };
my $root = { description => 'Project privileges and host Sys.Console are checked by register_docker_method.' };

sub output {
    my ($args) = @_;
    my $text = '';
    PVE::Tools::run_command([$helper, @$args], outfunc => sub { $text .= "$_[0]\n"; });
    return $text;
}

sub worker_id {
    my ($value) = @_;
    # Metadata and subprocess output are tainted under pvedaemon's perl -T.
    # Only validated captures may become part of the worker's task-log path.
    if (defined($value) && $value =~ /\A([1-9][0-9]{2,8})\z/) {
        return $1;
    }
    if (defined($value) && $value =~ /\A([0-9a-f]{32})\z/) {
        return $1; # Existing projects without a numeric ID.
    }
    die "Invalid Docker worker ID\n";
}

sub summaries {
    my $directory = '/etc/pve/betterdocker';
    my @items;
    if (opendir(my $dh, $directory)) {
        while (my $file = readdir($dh)) {
            next unless $file =~ /\A([0-9a-f]{32}\.json)\z/;
            $file = $1;
            eval {
                open(my $fh, '<', "$directory/$file") or die $!;
                local $/;
                my $info = decode_json(<$fh>);
                close $fh;
                push @items, $info;
            };
        }
        closedir($dh);
    }
    return \@items;
}

sub reserve_guest_id {
    my ($identifier, $node, $preferred) = @_;
    my $vmid;
    PVE::Cluster::cfs_lock_file('datacenter.cfg', 10, sub {
        if (!defined($preferred) || $preferred eq '') {
            my $next = PVE::API2::Cluster->map_method_by_name('nextid');
            $preferred = $next->{code}->({});
        }
        my $args = ['reserve-id', $identifier, $node];
        push @$args, $preferred if defined($preferred) && $preferred ne '';
        $vmid = output($args); chomp $vmid;
    });
    die $@ if $@;
    return worker_id($vmid);
}

sub migrate_ids {
    for my $item (@{summaries()}) {
        next if $item->{vmid};
        reserve_guest_id($item->{id}, $item->{node});
    }
}

sub task_id {
    my ($identifier, $node) = @_;
    for my $item (@{summaries()}) {
        if ($item->{id} eq $identifier && $item->{node} eq $node) {
            return worker_id($item->{vmid} // $identifier);
        }
    }
    die "Docker project does not exist on this node\n";
}


# VM.Audit permits a public inventory only. Host configuration, logs and shells
# require Sys.Console because Docker runs directly on the PVE host.
sub capabilities {
    my ($identifier, $node) = @_;
    my $rpc = PVE::RPCEnvironment::get();
    my $user = $rpc->get_user();
    my $root_user = $user eq 'root@pam';
    my $vmid = defined($identifier) ? task_id($identifier, $node) : undef;
    my $host = $root_user || $rpc->check($user, "/nodes/$node", ['Sys.Console'], 1);
    my $audit = $host || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['VM.Audit'], 1));
    return { audit => $audit ? 1 : 0, configure => $host ? 1 : 0, console => $host ? 1 : 0,
        snapshot => ($host || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['VM.Snapshot'], 1))) ? 1 : 0,
        rollback => ($host || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['VM.Snapshot'], 1)) || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['VM.Snapshot.Rollback'], 1))) ? 1 : 0,
        power => ($host || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['VM.PowerMgmt'], 1))) ? 1 : 0,
        permissions => ($root_user || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['Permissions.Modify'], 1))) ? 1 : 0 };
}

sub register_docker_method {
    my ($method) = @_;
    my $code = $method->{code};
    my $name = $method->{name};
    $method->{permissions} = { user => 'all' };
    $method->{code} = sub {
        my ($param) = @_;
        my $caps = capabilities($param->{id}, $param->{node});
        my $scope = $name eq 'list' && $param->{diagnostics} ? 'configure' :
                    $name =~ /^(list|management|rrddata|notes|capabilities|snapshots|snapshot_config)$/ ? 'audit' :
                    ($name eq 'snapshot' && $param->{action} eq 'restore') ? 'rollback' :
                    ($name eq 'snapshot' || $name eq 'snapshot_edit') ? 'snapshot' :
                    ($name eq 'action' && $param->{action} ne 'delete') || $name eq 'service_action' ? 'power' : 'configure';
        # List and capabilities are filtered below; knowing an ID is not a grant.
        die "Permission denied\n" unless ($name eq 'list' && !$param->{diagnostics}) || $caps->{$scope};
        return $code->($param);
    };
    __PACKAGE__->register_method($method);
}

# Called after Cluster.pm has registered its resource method. No new daemon is exposed.
sub hook_resources {
    my $next = PVE::API2::Cluster->map_method_by_name('nextid');
    if ($next) {
        my $original_next = $next->{code};
        $next->{code} = sub {
            my ($param) = @_;
            if ($param->{vmid}) {
                PVE::Cluster::check_vmid_unused($param->{vmid});
                return $original_next->($param);
            }
            my $candidate = $original_next->($param);
            my $conf = PVE::Cluster::cfs_read_file('datacenter.cfg');
            my $upper = $conf->{'next-id'}->{upper} // 1000000;
            while ($candidate < $upper) {
                return $candidate if PVE::Cluster::check_vmid_unused($candidate, 1);
                $candidate++;
            }
            die "No free guest ID available\n";
        };
    }
    my $info = PVE::API2::Cluster->map_method_by_name('resources');
    my $original = $info->{code};
    $info->{returns}->{items}->{properties}->{text} = { type => 'string', optional => 1 };
    push @{$info->{returns}->{items}->{properties}->{type}->{enum}}, 'docker';
    $info->{code} = sub {
        my ($param) = @_;
        my $resources = $original->($param);
        return $resources if $param->{type} && $param->{type} ne 'vm';
        my %nodes = map { $_->{node} => $_->{status} } grep { $_->{type} eq 'node' } @$resources;
        for my $item (@{summaries()}) {
            next unless capabilities($item->{id}, $item->{node})->{audit};
            my $status = $item->{status};
            $status = 'unknown' if exists($nodes{$item->{node}}) && $nodes{$item->{node}} ne 'online';
            push @$resources, {
                id => "docker/$item->{id}", vmid => $item->{vmid}, type => 'docker', name => $item->{name},
                text => $item->{vmid} ? "$item->{vmid} ($item->{name})" : $item->{name},
                node => $item->{node}, status => $status, uptime => ($status eq 'running' || $status eq 'partial') ? ($item->{metrics}->{uptime} // 0) : 0,
                tags => $item->{tags} // '',
                cpu => $item->{metrics}->{cpu} // 0, maxcpu => $item->{metrics}->{cpus} // 0,
                mem => $item->{metrics}->{mem} // 0, maxmem => $item->{metrics}->{maxmem} // 0,
                hastate => 'unmanaged',
            };
        }
        return $resources;
    };
}

register_docker_method({
    name => 'list', path => '', method => 'GET', protected => 1, proxyto => 'node',
    description => 'List host Docker projects on this node.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, diagnostics => { type => 'boolean', optional => 1, default => 0 } } },
    returns => { type => 'array', items => { type => 'object' } },
    code => sub { my ($param) = @_;
        return [decode_json(output(['diagnostics', $param->{node}]))] if $param->{diagnostics};
        return [grep { $_->{node} eq $param->{node} && capabilities($_->{id}, $_->{node})->{audit} } @{summaries()}]; },
});

register_docker_method({
    name => 'create', path => '', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Deploy Docker Compose or an image directly on this node with a dedicated published-port IP.',
    permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, deployment => { type => 'string', maxLength => 262144 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh $param->{deployment}; close $fh;
        eval { PVE::Tools::run_command([$helper, 'validate', $path]); };
        if (my $err = $@) { unlink $path; die $err; }
        my $rpcenv = PVE::RPCEnvironment::get();
        my $identifier = output(['new-id']); chomp $identifier;
        $identifier = worker_id($identifier);
        my $data = decode_json($param->{deployment});
        my $vmid = eval { reserve_guest_id($identifier, $param->{node}, $data->{vmid}); };
        if (my $err = $@) { unlink $path; die $err; }
        my $upid = eval { $rpcenv->fork_worker('betterdockercreate', $vmid, $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'create', $path, $param->{node}, $identifier]); };
            my $err = $@; unlink $path;
            output(['release-id', $identifier]) if $err;
            die $err if $err;
        }); };
        if (my $err = $@) { unlink $path; output(['release-id', $identifier]); die $err; }
        return $upid;
    },
});

register_docker_method({
    name => 'update', path => '{id}', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Edit a deployment and apply Compose changes without changing its project IP.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, deployment => { type => 'string', maxLength => 262144 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh $param->{deployment}; close $fh;
        eval { PVE::Tools::run_command([$helper, 'validate', $path]); };
        if (my $err = $@) { unlink $path; die $err; }
        my $rpcenv = PVE::RPCEnvironment::get();
        my $upid = eval { $rpcenv->fork_worker('betterdockerupdate', task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'update', $param->{id}, $param->{node}, $path]); };
            my $err = $@; unlink $path; die $err if $err;
        }); };
        if (my $err = $@) { unlink $path; die $err; }
        return $upid;
    },
});

register_docker_method({
    name => 'detail', path => '{id}', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Get a Docker deployment and its network settings.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub { my ($param) = @_; return decode_json(output(['detail', $param->{id}, $param->{node}])); },
});

register_docker_method({
    name => 'action', path => '{id}/action', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Start, stop, restart or remove a Docker project (volumes retained).', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, action => { type => 'string', enum => ['start', 'stop', 'restart', 'delete'] },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        my $rpcenv = PVE::RPCEnvironment::get();
        $param->{action} =~ /\A(start|stop|restart|delete)\z/ or die "Invalid Docker action\n";
        my $action = $1;
        return $rpcenv->fork_worker('betterdocker' . $action, task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
            PVE::Tools::run_command([$helper, 'action', $param->{id}, $param->{node}, $action]);
        });
    },
});

register_docker_method({
    name => 'logs', path => '{id}/logs', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read bounded project, service or container logs.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        service => { type => 'string', maxLength => 128, optional => 1 },
        container => { type => 'string', pattern => '^[0-9a-f]{64}$', optional => 1 },
        tail => { type => 'integer', minimum => 1, maximum => 5000, default => 200, optional => 1 } } },
    returns => { type => 'string' },
    code => sub { my ($p) = @_; return output(['logs', $p->{id}, $p->{node}, $p->{service} // '', $p->{container} // '', $p->{tail} // 200]); },
});

register_docker_method({
    name => 'management', path => '{id}/management', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read service resources, mounts, container state and live usage.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub { my ($param) = @_; my $info = decode_json(output(['management', $param->{id}, $param->{node}]));
        $info->{capabilities} = capabilities($param->{id}, $param->{node});
        delete $info->{deployment} unless $info->{capabilities}->{configure};
        return $info; },
});

register_docker_method({
    name => 'settings', path => '{id}/settings', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Apply persistent resource, DNS and restart settings to one service.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, service => { type => 'string', maxLength => 128 },
        settings => { type => 'string', maxLength => 8192 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh $param->{settings}; close $fh;
        my $rpcenv = PVE::RPCEnvironment::get();
        my $upid = eval { $rpcenv->fork_worker('betterdockersettings', task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'settings', $param->{id}, $param->{node}, $param->{service}, $path]); };
            my $err = $@; unlink $path; die $err if $err;
        }); };
        if (my $err = $@) { unlink $path; die $err; }
        return $upid;
    },
});

register_docker_method({
    name => 'termproxy', path => '{id}/termproxy', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Open an interactive shell inside a running project container.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, container => { type => 'string', pattern => '^[0-9a-f]{64}$' },
        shell => { type => 'string', enum => ['/bin/sh', '/bin/bash'], default => '/bin/sh', optional => 1 },
    } },
    returns => { type => 'object', properties => {
        user => { type => 'string' }, ticket => { type => 'string' },
        port => { type => 'integer' }, upid => { type => 'string' },
    } },
    code => sub {
        my ($param) = @_;
        my $shell = $param->{shell} // '/bin/sh';
        PVE::Tools::run_command([$helper, 'console-check', $param->{id}, $param->{node}, $param->{container}, $shell]);
        my $rpcenv = PVE::RPCEnvironment::get();
        my $user = $rpcenv->get_user();
        my $authpath = "/nodes/$param->{node}";
        my $port = PVE::Tools::next_vnc_port();
        my $help = '';
        PVE::Tools::run_command(['/usr/bin/termproxy', '--help'], outfunc => sub { $help .= $_[0]; });
        my $modern = $help =~ /--ticket-fd/;
        my $ticket = PVE::AccessControl::assemble_vnc_ticket($user, $authpath, $modern ? $port : undef);
        my $upid = $rpcenv->fork_worker('betterdockerconsole', task_id($param->{id}, $param->{node}), $user, sub {
            my $cmd = ['/usr/bin/termproxy', $port, '--path', $authpath, '--perm', 'Sys.Console'];
            if ($modern) {
                pipe(my $rd, my $wr) or die "ticket pipe: $!";
                my $flags = fcntl($rd, F_GETFD, 0) // die "ticket flags: $!";
                fcntl($rd, F_SETFD, $flags & ~FD_CLOEXEC) // die "ticket fd: $!";
                push @$cmd, '--vncticket-endpoint', '--verify-port', '--ticket-fd', fileno($rd);
                push @$cmd, '--', $helper, 'console', $param->{id}, $param->{node}, $param->{container}, $shell;
                PVE::Tools::run_command($cmd, afterfork => sub { print {$wr} $ticket; close($wr); });
            } else {
                push @$cmd, '--', $helper, 'console', $param->{id}, $param->{node}, $param->{container}, $shell;
                PVE::Tools::run_command($cmd);
            }
        });
        PVE::Tools::wait_for_vnc_port($port);
        return { user => $user, ticket => $ticket, port => $port, upid => $upid };
    },
});

register_docker_method({
    name => 'vncwebsocket', path => '{id}/vncwebsocket', method => 'GET', proxyto => 'node', permissions => $root,
    description => 'Connect a project terminal websocket using its authenticated ticket.',
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, vncticket => { type => 'string', maxLength => 512 },
        port => { type => 'integer', minimum => 5900, maximum => 5999 },
    } },
    returns => { type => 'object', properties => { port => { type => 'string' } } },
    code => sub {
        my ($param) = @_;
        my $user = PVE::RPCEnvironment::get()->get_user();
        my $help = '';
        PVE::Tools::run_command(['/usr/bin/termproxy', '--help'], outfunc => sub { $help .= $_[0]; });
        PVE::AccessControl::verify_vnc_ticket($param->{vncticket}, $user, "/nodes/$param->{node}", $help =~ /--ticket-fd/ ? $param->{port} : undef);
        return { port => $param->{port} };
    },
});

register_docker_method({
    name => 'rrddata', path => '{id}/rrddata', method => 'GET', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Read recorded Docker metrics in the native chart format (seven-day retention).',
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, timeframe => { type => 'string', enum => ['hour', 'day', 'week', 'month', 'year'] },
        cf => { type => 'string', enum => ['AVERAGE', 'MAX'], optional => 1, default => 'AVERAGE' },
    } },
    returns => { type => 'array', items => { type => 'object' } },
    code => sub { my ($p) = @_; return decode_json(output(['rrddata', $p->{id}, $p->{node}, $p->{timeframe}, $p->{cf} // 'AVERAGE'])); },
});

register_docker_method({
    name => 'notes', path => '{id}/config', method => 'GET', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Read project notes.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub { my ($p) = @_; return { %{decode_json(output(['notes', $p->{id}, $p->{node}]))},
        %{decode_json(output(['tags', $p->{id}, $p->{node}]))} }; },
});

register_docker_method({
    name => 'set_notes', path => '{id}/config', method => 'PUT', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Save project notes.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        description => { type => 'string', maxLength => 8192, optional => 1 },
        tags => { type => 'string', maxLength => 2048, optional => 1 } } },
    returns => { type => 'null' },
    code => sub {
        my ($p) = @_;
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path; print $fh ($p->{description} // ''); close $fh;
        eval {
            output(['notes', $p->{id}, $p->{node}, $path]) if defined($p->{description});
            output(['tags', $p->{id}, $p->{node}, $p->{tags}]) if defined($p->{tags});
        };
        my $err = $@; unlink $path; die $err if $err;
        return undef;
    },
});

register_docker_method({
    name => 'preview', path => '', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Validate Compose and preview non-secret deployment details.',
    parameters => { additionalProperties => 0, properties => { node => $node,
        deployment => { type => 'string', maxLength => 262144 }, id => { %$id, optional => 1 } } },
    returns => { type => 'array', items => { type => 'object' } },
    code => sub {
        my ($p) = @_;
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path; print $fh $p->{deployment}; close $fh;
        my $result = eval { decode_json(output(['preview', $path, defined($p->{id}) ? $p->{id} : ()])); };
        my $err = $@; unlink $path; die $err if $err;
        return $result;
    },
});

register_docker_method({
    name => 'service_action', path => '{id}/service-action', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Operate one Compose service without restarting dependencies.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        service => { type => 'string', maxLength => 128 }, action => { type => 'string', enum => ['start', 'stop', 'restart'] } } },
    returns => { type => 'string' },
    code => sub {
        my ($p) = @_;
        my $rpc = PVE::RPCEnvironment::get();
        $p->{action} =~ /\A(start|stop|restart)\z/ or die "Invalid service action\n";
        my $action = $1;
        return $rpc->fork_worker('betterdocker' . $action, task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'service-action', $p->{id}, $p->{node}, $p->{service}, $action]);
        });
    },
});

register_docker_method({
    name => 'reset_settings', path => '{id}/reset-settings', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Restore Compose settings for one service while preserving stopped state.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        service => { type => 'string', maxLength => 128 } } },
    returns => { type => 'string' },
    code => sub {
        my ($p) = @_;
        my $rpc = PVE::RPCEnvironment::get();
        return $rpc->fork_worker('betterdockersettings', task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'reset-settings', $p->{id}, $p->{node}, $p->{service}]);
        });
    },
});


register_docker_method({
    name => 'storage_limit', path => '{id}/storage-limit', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Create or enlarge the bounded project data disk; services must be stopped.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        size_mib => { type => 'integer', minimum => 256, maximum => 1048576 } } },
    returns => { type => 'string' },
    code => sub {
        my ($p) = @_;
        my $rpc = PVE::RPCEnvironment::get();
        return $rpc->fork_worker('betterdockersettings', task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'storage-limit', $p->{id}, $p->{node}, $p->{size_mib}]);
        });
    },
});

register_docker_method({
    name => 'snapshots', path => '{id}/snapshots', method => 'GET', protected => 1, proxyto => 'node',
    description => 'List complete private DCT snapshots without deployment secrets.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'array', items => { type => 'object' } },
    code => sub { my ($p) = @_; return decode_json(output(['snapshots', $p->{id}, $p->{node}])); },
});

register_docker_method({
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
        return $rpc->fork_worker('betterdockersnapshot' . $action, task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'snapshot', $p->{id}, $p->{node}, $action, $p->{snapname}, $p->{description} // '']);
        });
    },
});

register_docker_method({
    name => 'snapshot_config', path => '{id}/snapshot/{snapname}/config', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read public settings stored in a DCT snapshot.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        snapname => { type => 'string', pattern => '[A-Za-z][A-Za-z0-9_-]{0,39}', maxLength => 40 } } },
    returns => { type => 'object' },
    code => sub { my ($p) = @_; return decode_json(output(['snapshot-config', $p->{id}, $p->{node}, $p->{snapname}])); },
});

register_docker_method({
    name => 'snapshot_edit', path => '{id}/snapshot/{snapname}/config', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Edit a DCT snapshot description without changing its saved contents.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        snapname => { type => 'string', pattern => '[A-Za-z][A-Za-z0-9_-]{0,39}', maxLength => 40 },
        description => { type => 'string', maxLength => 4096 } } },
    returns => { type => 'null' },
    code => sub { my ($p) = @_; output(['snapshot-edit', $p->{id}, $p->{node}, $p->{snapname}, $p->{description}]); return undef; },
});

1;
