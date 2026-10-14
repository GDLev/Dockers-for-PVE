package PVE::API2::BetterDocker::Console;

use strict;
use warnings;
use Fcntl qw(F_GETFD F_SETFD FD_CLOEXEC);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'termproxy', path => '{id}/termproxy', method => 'POST', protected => 1,
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
            PVE::Tools::run_command(PVE::API2::BetterDocker::console_helper_command($param, 'console-check', $shell, 0));
            my $rpcenv = PVE::RPCEnvironment::get();
            my $user = $rpcenv->get_user();
            my $authpath = "/vms/" . PVE::API2::BetterDocker::task_id($param->{id}, $param->{node});
            my $port = PVE::Tools::next_vnc_port();
            my $help = '';
            PVE::Tools::run_command(['/usr/bin/termproxy', '--help'], outfunc => sub { $help .= $_[0]; });
            my $modern = $help =~ /--ticket-fd/;
            my $ticket = PVE::AccessControl::assemble_vnc_ticket($user, $authpath, $modern ? $port : undef);
            my $upid = $rpcenv->fork_worker('betterdockerconsole', PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $user, sub {
                my $cmd = ['/usr/bin/termproxy', $port, '--path', $authpath, '--perm', 'VM.Console'];
                if ($modern) {
                    pipe(my $rd, my $wr) or die "ticket pipe: $!";
                    my $flags = fcntl($rd, F_GETFD, 0) // die "ticket flags: $!";
                    fcntl($rd, F_SETFD, $flags & ~FD_CLOEXEC) // die "ticket fd: $!";
                    push @$cmd, '--vncticket-endpoint', '--verify-port', '--ticket-fd', fileno($rd);
                    push @$cmd, '--', @{PVE::API2::BetterDocker::console_helper_command($param, 'console', $shell, 1)};
                    PVE::Tools::run_command($cmd, afterfork => sub { print {$wr} $ticket; close($wr); });
                } else {
                    push @$cmd, '--', @{PVE::API2::BetterDocker::console_helper_command($param, 'console', $shell, 1)};
                    PVE::Tools::run_command($cmd);
                }
            });
            PVE::Tools::wait_for_vnc_port($port);
            return { user => $user, ticket => $ticket, port => $port, upid => $upid };
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'vncwebsocket', path => '{id}/vncwebsocket', method => 'GET', permissions => $root,
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
            PVE::AccessControl::verify_vnc_ticket($param->{vncticket}, $user, "/vms/" . PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $help =~ /--ticket-fd/ ? $param->{port} : undef);
            return { port => $param->{port} };
        },
    });
}

1;
