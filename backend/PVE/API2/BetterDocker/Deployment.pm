package PVE::API2::BetterDocker::Deployment;

use strict;
use warnings;
use JSON qw(decode_json);
use File::Temp qw(tempfile);
use PVE::Exception qw(raise_perm_exc);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'create', path => '', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Deploy Docker Compose or an image directly on this node with a dedicated published-port IP.',
        permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            start => { type => 'boolean', optional => 1, default => 1 },
            node => $node, deployment => { type => 'string', maxLength => 262144 },
            archive => { type => 'string', format => 'pve-volume-id', optional => 1 },
            newid => { type => 'integer', minimum => 100, optional => 1 },
            storage => { type => 'string', format => 'pve-storage-id', optional => 1 },
            pool => { type => 'string', format => 'pve-poolid', optional => 1 },
            hookscript => { type => 'string', format => 'pve-volume-id', optional => 1 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            if ($param->{hookscript}) {
                raise_perm_exc('changing the hookscript is only allowed for root@pam')
                    unless PVE::RPCEnvironment::get()->get_user() eq 'root@pam';
                PVE::GuestHelpers::check_hookscript($param->{hookscript});
                die "Set the restored hookscript through guest configuration\n" if $param->{archive};
            }
            return PVE::API2::BetterDocker::restore_backup($param) if $param->{archive};
            die "newid and storage are restore options\n" if defined($param->{newid}) || defined($param->{storage});
            my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path;
            print $fh $param->{deployment}; close $fh;
            # Parse Compose and enforce deployment policy before reserving a guest ID.
            # A rejected form stays open instead of creating a doomed task.
            eval { PVE::API2::BetterDocker::output(['preview', $path]); };
            if (my $err = $@) { unlink $path; die $err; }
            my $rpcenv = PVE::RPCEnvironment::get();
            my $identifier = PVE::API2::BetterDocker::output(['new-id']); chomp $identifier;
            $identifier = PVE::API2::BetterDocker::worker_id($identifier);
            my $data = decode_json($param->{deployment});
            my $vmid = eval { PVE::API2::BetterDocker::reserve_guest_id($identifier, $param->{node}, $data->{vmid}); };
            if (my $err = $@) { unlink $path; die $err; }
            my $upid = eval { $rpcenv->fork_worker('betterdockercreate', $vmid, $rpcenv->get_user(), sub {
                eval { PVE::Tools::run_command([$helper, 'create', $path, $param->{node}, $identifier, $param->{hookscript} // '', '0']); };
                my $err = $@; unlink $path;
                # Membership precedes Start, like LXC, and keeps a retained failed
                # deployment accessible to the pool owner for diagnosis/removal.
                if ($param->{pool} && grep { $_->{id} eq $identifier } @{PVE::API2::BetterDocker::summaries()}) {
                    PVE::AccessControl::add_vm_to_pool($vmid, $param->{pool});
                }
                PVE::API2::BetterDocker::output(['release-id', $identifier]) if $err;
                die $err if $err;
                PVE::Tools::run_command([$helper, 'action', $identifier, $param->{node}, 'start']) if $param->{start} // 1;
            }); };
            if (my $err = $@) { unlink $path; PVE::API2::BetterDocker::output(['release-id', $identifier]); die $err; }
            return $upid;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'update', path => '{id}', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Edit a deployment and apply Compose changes without changing its project IP.', permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, digest => $digest, deployment => { type => 'string', maxLength => 262144 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            PVE::API2::BetterDocker::check_digest($param);
            my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path;
            print $fh $param->{deployment}; close $fh;
            eval { PVE::Tools::run_command([$helper, 'validate', $path]); };
            if (my $err = $@) { unlink $path; die $err; }
            my $rpcenv = PVE::RPCEnvironment::get();
            my $upid = eval { $rpcenv->fork_worker('betterdockerupdate', PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
                eval { PVE::Tools::run_command([$helper, 'update', $param->{id}, $param->{node}, $path, $param->{digest} // '']); };
                my $err = $@; unlink $path; die $err if $err;
            }); };
            if (my $err = $@) { unlink $path; die $err; }
            return $upid;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'preview', path => '', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Validate Compose and preview non-secret deployment details.',
        parameters => { additionalProperties => 0, properties => { node => $node, pool => { type => 'string', format => 'pve-poolid', optional => 1 },
            deployment => { type => 'string', maxLength => 262144 }, id => { %$id, optional => 1 } } },
        returns => { type => 'array', items => { type => 'object' } },
        code => sub {
            my ($p) = @_;
            my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path; print $fh $p->{deployment}; close $fh;
            my $result = eval { decode_json(PVE::API2::BetterDocker::output(['preview', $path, defined($p->{id}) ? $p->{id} : ()])); };
            my $err = $@; unlink $path; die $err if $err;
            return $result;
        },
    });
}

1;
