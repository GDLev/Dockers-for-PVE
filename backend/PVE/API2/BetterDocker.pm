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
my $root = { description => 'Only root@pam can manage host Docker projects.' };

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
        return $resources unless PVE::RPCEnvironment::get()->get_user() eq 'root@pam';
        return $resources if $param->{type} && $param->{type} ne 'vm';
        my %nodes = map { $_->{node} => $_->{status} } grep { $_->{type} eq 'node' } @$resources;
        for my $item (@{summaries()}) {
            my $status = $item->{status};
            $status = 'unknown' if exists($nodes{$item->{node}}) && $nodes{$item->{node}} ne 'online';
            push @$resources, {
                id => "docker/$item->{id}", vmid => $item->{vmid}, type => 'docker', name => $item->{name},
                text => $item->{vmid} ? "$item->{vmid} ($item->{name})" : $item->{name},
                node => $item->{node}, status => $status, uptime => $status eq 'running' ? 1 : 0,
                hastate => 'unmanaged',
            };
        }
        return $resources;
    };
}

__PACKAGE__->register_method({
    name => 'list', path => '', method => 'GET', protected => 1, proxyto => 'node',
    description => 'List host Docker projects on this node.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node } },
    returns => { type => 'array', items => { type => 'object' } },
    code => sub { my ($param) = @_; return [grep { $_->{node} eq $param->{node} } @{summaries()}]; },
});

__PACKAGE__->register_method({
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

__PACKAGE__->register_method({
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

__PACKAGE__->register_method({
    name => 'detail', path => '{id}', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Get a Docker deployment and its network settings.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub { my ($param) = @_; return decode_json(output(['detail', $param->{id}, $param->{node}])); },
});

__PACKAGE__->register_method({
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

__PACKAGE__->register_method({
    name => 'logs', path => '{id}/logs', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read the last 200 lines of project logs.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'string' },
    code => sub { my ($param) = @_; return output(['logs', $param->{id}, $param->{node}]); },
});

__PACKAGE__->register_method({
    name => 'management', path => '{id}/management', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read service resources, mounts, container state and live usage.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub { my ($param) = @_; return decode_json(output(['management', $param->{id}, $param->{node}])); },
});

__PACKAGE__->register_method({
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

__PACKAGE__->register_method({
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

__PACKAGE__->register_method({
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

__PACKAGE__->register_method({
    name => 'rrddata', path => '{id}/rrddata', method => 'GET', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Read recorded Docker metrics in the native chart format (seven-day retention).',
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, timeframe => { type => 'string', enum => ['hour', 'day', 'week', 'month', 'year'] },
        cf => { type => 'string', enum => ['AVERAGE', 'MAX'], optional => 1, default => 'AVERAGE' },
    } },
    returns => { type => 'array', items => { type => 'object' } },
    code => sub { my ($p) = @_; return decode_json(output(['rrddata', $p->{id}, $p->{node}, $p->{timeframe}, $p->{cf} // 'AVERAGE'])); },
});

__PACKAGE__->register_method({
    name => 'notes', path => '{id}/config', method => 'GET', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Read project notes.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub { my ($p) = @_; return decode_json(output(['notes', $p->{id}, $p->{node}])); },
});

__PACKAGE__->register_method({
    name => 'set_notes', path => '{id}/config', method => 'PUT', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Save project notes.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        description => { type => 'string', maxLength => 8192 } } },
    returns => { type => 'null' },
    code => sub {
        my ($p) = @_;
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path; print $fh $p->{description}; close $fh;
        eval { output(['notes', $p->{id}, $p->{node}, $path]); };
        my $err = $@; unlink $path; die $err if $err;
        return undef;
    },
});
1;
