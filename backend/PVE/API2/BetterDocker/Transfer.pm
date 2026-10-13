package PVE::API2::BetterDocker::Transfer;

use strict;
use warnings;
use JSON qw(decode_json encode_json);
use File::Temp qw(tempfile);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'clone', path => '{id}/clone', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Clone a stopped DCT; linked clones require LVM-thin template disks.', permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, digest => $digest,
            newid => { type => 'integer', minimum => 100, optional => 1 },
            deployment => { type => 'string', maxLength => 262144 },
            full => { type => 'boolean', optional => 1, default => 1 },
            storage => { type => 'string', format => 'pve-storage-id', optional => 1 },
            pool => { type => 'string', format => 'pve-poolid', optional => 1 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            PVE::API2::BetterDocker::check_digest($param);
            my $rpc = PVE::RPCEnvironment::get();
            my $user = $rpc->get_user();
            $rpc->check($user, '/vms/' . PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), ['VM.Clone']);
            PVE::API2::BetterDocker::check_destination_pool($param, $param->{newid});
            $rpc->check($user, '/storage/' . $param->{storage}, ['Datastore.AllocateSpace']) if $param->{storage};
            my $deployment = decode_json($param->{deployment});
            die "Clone deployment overrides must be an object\n" unless ref($deployment) eq 'HASH';
            my ($fh, $path) = tempfile('betterdocker-clone-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path;
            print $fh encode_json({ deployment => $deployment, storage => $param->{storage} // '', digest => $param->{digest}, full => defined($param->{full}) ? 0 + $param->{full} : 1 }); close $fh;
            my $identifier = PVE::API2::BetterDocker::output(['new-id']); chomp $identifier;
            $identifier = PVE::API2::BetterDocker::worker_id($identifier);
            my $vmid = eval { PVE::API2::BetterDocker::reserve_guest_id($identifier, $param->{node}, $param->{newid}); };
            if (my $error = $@) { unlink $path; die $error; }
            my $context = decode_json($ENV{BETTERDOCKER_REQUEST_CONTEXT});
            $context->{destination_id} = $identifier;
            $context->{destination_vmid} = 0 + $vmid;
            local $ENV{BETTERDOCKER_REQUEST_CONTEXT} = JSON::encode_json($context);
            my $upid = eval { $rpc->fork_worker('betterdockerclone', $vmid, $user, sub {
                eval { PVE::Tools::run_command(['/usr/local/lib/betterdocker/guest_state.py', 'clone',
                    $param->{id}, $param->{node}, $identifier, $path]); };
                my $error = $@;
                unlink $path;
                PVE::API2::BetterDocker::output(['release-id', $identifier]) if $error;
                die $error if $error;
                PVE::AccessControl::add_vm_to_pool($vmid, $param->{pool}) if $param->{pool};
            }); };
            if (my $error = $@) { unlink $path; PVE::API2::BetterDocker::output(['release-id', $identifier]); die $error; }
            return $upid;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'migrate', path => '{id}/migrate', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Move a DCT to another cluster node, optionally shutting down and restarting it.', permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, digest => $digest, target => $node,
            restart => { type => 'boolean', optional => 1, default => 0 },
            timeout => { type => 'integer', minimum => 1, maximum => 86400, optional => 1, default => 180 },
            storage => { type => 'string', format => 'pve-storage-id', optional => 1 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            PVE::API2::BetterDocker::check_digest($param);
            my $rpc = PVE::RPCEnvironment::get();
            my $user = $rpc->get_user();
            $rpc->check($user, '/vms/' . PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), ['VM.Migrate']);
            $rpc->check($user, '/storage/' . $param->{storage}, ['Datastore.AllocateSpace']) if $param->{storage};
            die "Choose another cluster node\n" if $param->{node} eq $param->{target};
            PVE::Cluster::check_cfs_quorum();
            PVE::Cluster::remote_node_ip($param->{target});
            if (PVE::API2::BetterDocker::ha_managed($param->{id}, $param->{node}) && $rpc->{type} ne 'ha') {
                die "HA relocation preserves storage; omit the storage override\n" if $param->{storage};
                my $vmid = PVE::API2::BetterDocker::task_id($param->{id}, $param->{node});
                return $rpc->fork_worker('hamigrate', $vmid, $user, sub {
                    PVE::Tools::run_command(['ha-manager', 'crm-command', 'relocate', "docker:$vmid", $param->{target}]);
                });
            }
            my $state = decode_json(PVE::API2::BetterDocker::output(['access-state', $param->{id}, $param->{node}]));
            die "Running DCT migration requires restart=1\n" if $state->{active_containers} && !$param->{restart};
            return $rpc->fork_worker('betterdockermigrate', PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $user, sub {
                PVE::Tools::run_command(['/usr/local/lib/betterdocker/transfer.py', 'migrate',
                    $param->{id}, $param->{node}, $param->{target}, $param->{storage} // '', $param->{digest} // '', $param->{restart} // 0, $param->{timeout} // 180]);
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'template', path => '{id}/template', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Convert a stopped DCT to a non-bootable full-clone template.', permissions => $root,
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            PVE::API2::BetterDocker::check_digest($param);
            my $rpc = PVE::RPCEnvironment::get();
            $rpc->check($rpc->get_user(), '/vms/' . PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), ['VM.Allocate']);
            return $rpc->fork_worker('betterdockertemplate', PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command(['/usr/local/lib/betterdocker/guest_state.py', 'template', $param->{id}, $param->{node}, $param->{digest} // '']);
            });
        },
    });
}

1;
