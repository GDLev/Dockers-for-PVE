package PVE::API2::BetterDocker;
use strict;
use warnings;
use base qw(PVE::RESTHandler);
use File::Temp qw(tempfile);
use JSON qw(decode_json);
use PVE::RPCEnvironment;
use PVE::Tools;

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
        my $upid = eval { $rpcenv->fork_worker('betterdocker', undef, $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'create', $path, $param->{node}]); };
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
        my $upid = eval { $rpcenv->fork_worker('betterdocker', undef, $rpcenv->get_user(), sub {
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
        return $rpcenv->fork_worker('betterdocker', undef, $rpcenv->get_user(), sub {
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
1;
