package PVE::API2::BetterDocker;
use strict;
use warnings;
use base qw(PVE::RESTHandler);
use File::Temp qw(tempfile);
use JSON qw(decode_json);
use PVE::RPCEnvironment;
use PVE::Tools;
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

sub summaries {
    my $directory = '/etc/pve/betterdocker';
    my @items;
    if (opendir(my $dh, $directory)) {
        while (my $file = readdir($dh)) {
            next unless $file =~ /^[0-9a-f]{32}\.json$/;
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

# Called after Cluster.pm has registered its resource method. No new daemon is exposed.
sub hook_resources {
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
                id => "docker/$item->{id}", type => 'docker', name => $item->{name}, text => $item->{name},
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
        my $upid = eval { $rpcenv->fork_worker('betterdocker', $identifier, $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'create', $path, $param->{node}, $identifier]); };
            my $err = $@; unlink $path; die $err if $err;
        }); };
        if (my $err = $@) { unlink $path; die $err; }
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
        my $upid = eval { $rpcenv->fork_worker('betterdocker', $param->{id}, $rpcenv->get_user(), sub {
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
        return $rpcenv->fork_worker('betterdocker', $param->{id}, $rpcenv->get_user(), sub {
            PVE::Tools::run_command([$helper, 'action', $param->{id}, $param->{node}, $param->{action}]);
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
        my $upid = eval { $rpcenv->fork_worker('betterdocker', $param->{id}, $rpcenv->get_user(), sub {
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
        my $upid = $rpcenv->fork_worker('betterdockerconsole', $param->{id}, $user, sub {
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
1;
