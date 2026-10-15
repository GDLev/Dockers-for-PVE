package PVE::API2::BetterDocker;

use strict;
use warnings;
use base qw(PVE::RESTHandler);
use File::Temp qw(tempfile);
use JSON qw(decode_json encode_json);
use PVE::RPCEnvironment;
use PVE::Exception qw(raise_perm_exc);
use PVE::Tools;
use PVE::Cluster;
use PVE::AccessControl;
use PVE::GuestHelpers;
use PVE::API2::Storage::Content;
use PVE::Storage;
use PVE::JSONSchema;
use PVE::BetterDocker::Firewall;
use POSIX qw(LONG_MAX);
use Fcntl qw(F_GETFD F_SETFD FD_CLOEXEC);

my $helper = '/usr/local/lib/betterdocker/provision.py';
my $node = { type => 'string', format => 'pve-node' };
my $id = { type => 'string', pattern => '^[0-9a-f]{32}$' };
my $digest = { type => 'string', pattern => '^[0-9a-f]{64}$', optional => 1,
    description => 'Configuration digest from GET; prevents concurrent modification when supplied.' };
my $root = { description => 'Guest privileges and deployment safety are checked by register_docker_method.' };

sub method_defaults {
    return ($helper, $node, $id, $digest, $root);
}

sub output {
    my ($args) = @_;
    my $text = '';
    my $reason = '';
    my %options = (outfunc => sub { $text .= "$_[0]\n"; });
    $options{errfunc} = sub { $reason .= "$_[0]\n"; } if $args->[0] eq 'preview';
    eval { PVE::Tools::run_command([$helper, @$args], %options); };
    if (my $error = $@) {
        die substr($1, 0, 2000) . "\n" if $args->[0] eq 'preview' && $reason =~ /BetterDocker: ([\s\S]+)/;
        die $error;
    }
    return $text;
}

sub check_digest {
    my ($param) = @_;
    return unless defined($param->{digest});
    my $info = decode_json(output(['detail', $param->{id}, $param->{node}]));
    PVE::Tools::assert_if_modified($param->{digest}, $info->{digest});
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


# Guest privileges apply to each DCT. Node console access only bypasses the
# delegated runtime policy; it does not grant guest privileges.
sub capabilities {
    my ($identifier, $node) = @_;
    my $rpc = PVE::RPCEnvironment::get();
    my $user = $rpc->get_user();
    my $root_user = $user eq 'root@pam';
    my $vmid = defined($identifier) ? task_id($identifier, $node) : undef;
    my $host = $root_user || $rpc->check($user, "/nodes/$node", ['Sys.Console'], 1);
    my $audit = $root_user || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['VM.Audit'], 1));
    my $granted = sub { my ($priv) = @_; return $root_user || (defined($vmid) && $rpc->check($user, "/vms/$vmid", [$priv], 1)) ? 1 : 0; };
    my $cpu = $granted->('VM.Config.CPU');
    my $memory = $granted->('VM.Config.Memory');
    my $network = $granted->('VM.Config.Network');
    my $options = $granted->('VM.Config.Options');
    my $configure = $root_user || ($cpu && $memory && $network && $options && $granted->('VM.Config.Disk'));
    my $console = $root_user || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['VM.Console'], 1));
    my $disk = $granted->('VM.Config.Disk');
    my $clone = $granted->('VM.Clone');
    my $migrate = $granted->('VM.Migrate');
    my $allocate = $granted->('VM.Allocate');
    my $safe = 1;
    if (!$host && defined($identifier) && ($configure || $console || $disk || $clone || $migrate || $allocate)) {
        $safe = eval { decode_json(output(['delegated-check', $identifier, $node]))->{safe} } ? 1 : 0;
    }
    return { audit => $audit ? 1 : 0, trusted => $host ? 1 : 0,
        configure => ($configure && $safe) ? 1 : 0, console => ($console && $safe) ? 1 : 0,
        disk => ($disk && $safe) ? 1 : 0, clone => ($clone && $safe) ? 1 : 0,
        migrate => ($migrate && $safe) ? 1 : 0, template => ($allocate && $safe) ? 1 : 0,
        remove => ($allocate && $safe) ? 1 : 0,
        cpu => $cpu, memory => $memory, network => $network, options => $options, hookscript => $root_user ? 1 : 0,
        settings => ($cpu || $memory || $network || $options || $disk) ? 1 : 0,
        images => ($configure && $safe && $granted->('VM.Snapshot') && $granted->('VM.Snapshot.Rollback')) ? 1 : 0,
        snapshot => $granted->('VM.Snapshot'),
        rollback => $granted->('VM.Snapshot.Rollback'),
        power => $granted->('VM.PowerMgmt'),
        ha => ($root_user || $rpc->check($user, '/', ['Sys.Modify'], 1)) ? 1 : 0,
        hardening => ($root_user || ($configure && $rpc->check($user, "/nodes/$node", ['Sys.Modify'], 1))) ? 1 : 0,
        permissions => ($root_user || (defined($vmid) && $rpc->check($user, "/vms/$vmid", ['Permissions.Modify'], 1))) ? 1 : 0 };
}


sub storage_guest_owner {
    my ($vmid) = @_;
    return undef unless defined($vmid) && $vmid =~ /\A([1-9][0-9]{2,8})\z/;
    $vmid = $1;
    my $path = "/etc/pve/betterdocker/ids/$vmid.json";
    return undef unless -f $path;
    open(my $fh, '<', $path) or die "Cannot read DCT disk owner: $!\n";
    local $/;
    my $owner = decode_json(<$fh>);
    die "Invalid DCT disk owner\n" unless $owner->{id} =~ /\A[0-9a-f]{32}\z/;
    $owner->{vmid} = int($vmid);
    return $owner;
}

sub check_storage_delete {
    my ($cfg, $volid) = @_;
    my ($type, undef, $vmid) = PVE::Storage::parse_volname($cfg, $volid);
    return unless $type eq 'images' || $type eq 'rootdir';
    if (storage_guest_owner($vmid)) {
        die "Cannot remove a DCT disk from storage. Manage disks from the DCT; retained disks require explicit cleanup after DCT removal.\n";
    }
}

sub hook_storage_content {
    my $index = PVE::API2::Storage::Content->map_method_by_name('index');
    my $native_index = $index->{code};
    $index->{parameters}->{properties}->{dct} = {
        type => 'boolean', optional => 1,
        description => 'Filter Docker volumes (1) or exclude them (0). Omit for the complete native list.',
    };
    for my $key (qw(dct_name dct_status dct_id dct_node)) {
        $index->{returns}->{items}->{properties}->{$key} = {type => 'string', optional => 1};
    }
    $index->{code} = sub {
        my ($param) = @_;
        my $filter = $param->{dct};
        my $native_param = {%$param};
        delete $native_param->{dct};
        # Directory raw disks and LVM-thin disks can be reported under different content types.
        delete $native_param->{content} if $filter;
        my $items = $native_index->($native_param);
        return $items unless defined($filter);
        if ($filter) {
            my $cfg = PVE::Storage::config();
            my $extra = PVE::Storage::vdisk_list($cfg, $param->{storage}, $param->{vmid});
            my %seen = map { $_->{volid} => 1 } @$items;
            my $rpc = PVE::RPCEnvironment::get();
            for my $disk (@{$extra->{$param->{storage}} // []}) {
                next if $seen{$disk->{volid}} || !storage_guest_owner($disk->{vmid});
                eval { PVE::Storage::check_volume_access($rpc, $rpc->get_user(), $cfg, undef, $disk->{volid}); };
                next if $@;
                $disk->{content} = 'images';
                $disk->{vmid} = int($disk->{vmid});
                $disk->{size} = int($disk->{size});
                push @$items, $disk;
            }
        }
        my @result;
        my %guests = map { $_->{vmid} => $_ } grep { $_->{vmid} } @{summaries()};
        my %disk_states;
        for my $item (@$items) {
            my $disk = ($item->{content} // '') =~ /\A(images|rootdir)\z/;
            my $owner = $disk ? storage_guest_owner($item->{vmid}) : undef;
            next if $filter ? !$owner : $owner;
            if ($owner) {
                my $guest = $guests{$item->{vmid}};
                my $rpc = PVE::RPCEnvironment::get();
                my $user = $rpc->get_user();
                my $audit = $user eq 'root@pam' || $rpc->check($user, '/vms/' . $item->{vmid}, ['VM.Audit'], 1) ||
                    $rpc->check($user, '/nodes/' . $owner->{node}, ['Sys.Console'], 1);
                $item->{dct_name} = $audit ? ($guest ? $guest->{name} : $owner->{name} // '') : '';
                $item->{dct_id} = $audit ? $owner->{id} : '';
                $item->{dct_node} = $audit && $guest ? $guest->{node} : '';
                $item->{dct_status} = 'retained';
                if ($guest && $guest->{node} eq $param->{node}) {
                    my $id = $owner->{id};
                    if (!exists($disk_states{$id})) {
                        my $path = "/var/lib/betterdocker/projects/$id/storage.json";
                        my $state = {};
                        if (-f $path) {
                            open(my $fh, '<', $path) or die "Cannot read DCT disk state: $!\n";
                            local $/;
                            $state = decode_json(<$fh>);
                        }
                        $disk_states{$id} = $state;
                    }
                    my %attached = map { $_ => 1 } grep { defined($_) } ($disk_states{$id}->{volid},
                        map { $_->{volid} } values %{$disk_states{$id}->{additional} // {}});
                    $item->{dct_status} = $attached{$item->{volid}} ? 'in-use' : 'unused';
                }
            }
            $item->{vmid} = int($item->{vmid}) if defined($item->{vmid});
            $item->{size} = int($item->{size}) if defined($item->{size});
            $item->{used} = int($item->{used}) if defined($item->{used});
            push @result, $item;
        }
        return \@result;
    };
    my $delete = PVE::API2::Storage::Content->map_method_by_name('delete');
    my $native_delete = $delete->{code};
    $delete->{code} = sub {
        my ($param) = @_;
        my $volid = $param->{volume};
        $volid = $param->{storage} . ':' . $volid if $volid !~ /:/ && $param->{storage};
        check_storage_delete(PVE::Storage::config(), $volid);
        # Recheck inside the native worker immediately before freeing, closing the fork race.
        my $native_free = \&PVE::Storage::vdisk_free;
        no warnings 'redefine';
        local *PVE::Storage::vdisk_free = sub {
            check_storage_delete($_[0], $_[1]);
            return $native_free->(@_);
        };
        return $native_delete->($param);
    };
}


# Retained guests no longer have guest ACLs. Cleaning those disks requires node administration.
__PACKAGE__->register_method({
    name => 'delete_storage_volume', path => '{id}/volumes/{vmid}', method => 'DELETE',
    protected => 1, proxyto => 'node', permissions => {user => 'all'},
    description => 'Remove one unused or retained DCT-owned PVE disk.',
    parameters => {additionalProperties => 0, properties => {
        node => $node, id => $id, vmid => {type => 'integer', minimum => 100, maximum => 999999999},
        volume => {type => 'string', format => 'pve-volume-id'},
    }}, returns => {type => 'string'},
    code => sub {
        my ($p) = @_;
        my $owner = storage_guest_owner($p->{vmid}) or die "Not a DCT disk owner\n";
        die "DCT disk ownership changed\n" unless $owner->{id} eq $p->{id};
        my $rpc = PVE::RPCEnvironment::get();
        my $user = $rpc->get_user();
        my ($guest) = grep { $_->{id} eq $owner->{id} && $_->{node} eq $p->{node} } @{summaries()};
        my $caps = $guest ? capabilities($owner->{id}, $p->{node}) : {};
        if ($guest) {
            raise_perm_exc('DCT disk') unless $caps->{disk};
            die "Remove DCT from HA before deleting disks\n" if ha_managed($owner->{id}, $p->{node});
        } else {
            $rpc->check($user, "/nodes/$p->{node}", ['Sys.Modify']);
        }
        my ($sid) = PVE::Storage::parse_volume_id($p->{volume});
        $rpc->check($user, "/storage/$sid", ['Datastore.AllocateSpace']);
        local $ENV{BETTERDOCKER_TASK_USER} = $user;
        local $ENV{BETTERDOCKER_TRUSTED} = $guest ? ($caps->{trusted} ? '1' : '0') : '1';
        local $ENV{BETTERDOCKER_REQUIRE_SAFE} = $guest && !$caps->{trusted} ? '1' : '0';
        local $ENV{BETTERDOCKER_REQUEST_CONTEXT} = $guest ? encode_json({id => $p->{id}, node => $p->{node},
            vmid => 0 + $p->{vmid}, runtime_mode => $guest->{runtime_mode}}) : '';
        my $state = decode_json(output(['storage-volume-state', $p->{id}, $p->{node}, $p->{vmid}]));
        my @allowed = @{$state->{unused} // []};
        push @allowed, $state->{volid} if !$guest && $state->{volid};
        push @allowed, map { $_->{volid} } values %{$state->{additional} // {}} if !$guest;
        die "Cannot delete an attached or unowned DCT disk\n" unless grep { $_ eq $p->{volume} } @allowed;
        return $rpc->fork_worker('betterdockerdiskdelete', worker_id($p->{vmid}), $user, sub {
            PVE::Tools::run_command([$helper, 'delete-storage-volume', $p->{vmid}, $p->{node}, $p->{volume}]);
        });
    },
});

hook_storage_content();
require PVE::BetterDocker::Backup;
PVE::BetterDocker::Backup::hook_storage();

sub check_native_guest_ids {
    my ($operation, @values) = @_;
    for my $value (@values) {
        next unless defined($value);
        for my $vmid (split(/[\s,;]+/, $value)) {
            if (storage_guest_owner($vmid)) {
                die "DCT $vmid does not support native $operation. No changes were made.\n";
            }
        }
    }
}

my %native_operation_guards;
sub guard_native_method {
    my ($class, $name, $operation, $ids) = @_;
    return if $native_operation_guards{"$class:$name"};
    my $method = $class->map_method_by_name($name)
        or die "Unsupported PVE method $class/$name\n";
    my $native = $method->{code};
    $method->{code} = sub {
        check_native_guest_ids($operation, $ids->($_[0]));
        return $native->(@_);
    };
    $native_operation_guards{"$class:$name"} = 1;
}

sub hook_native_node_operations {
    for my $class (qw(PVE::API2::LXC PVE::API2::Qemu)) {
        guard_native_method($class, 'create_vm', 'VM/CT creation or restore', sub { $_[0]->{vmid} });
        guard_native_method($class, 'clone_vm', 'cloning', sub { $_[0]->{vmid}, $_[0]->{newid} });
        guard_native_method($class, 'template', 'templates', sub { $_[0]->{vmid} });
        for my $name (qw(migrate_vm remote_migrate_vm)) {
            guard_native_method($class, $name, 'migration', sub { $_[0]->{vmid} });
        }
    }
}

sub hook_native_cluster_operations {
    hook_bulk_operations();
    for my $name (qw(create update migrate relocate delete)) {
        my $method = PVE::API2::HA::Resources->map_method_by_name($name)
            or die "Unsupported HA API method $name\n";
        next if $native_operation_guards{"HA:$name"}++;
        my $native = $method->{code};
        $method->{code} = sub {
            my ($param) = @_;
            check_native_guest_ids('HA', $1) if ($param->{sid} // '') =~ /\A(?:vm|ct):([1-9][0-9]{2,8})\z/;
            my ($sid, $type, $vmid) = PVE::HA::Config::parse_sid($param->{sid});
            return $native->(@_) unless $type eq 'docker';
            # Keep enrollment and native registration in one per-guest transaction.
            # Otherwise a failed concurrent create can release another request's HA marker.
            my $result = PVE::Tools::lock_file("/run/lock/betterdocker-ha-$vmid.lock", 10, sub {
                my $info = PVE::HA::Resources::Docker->config($vmid);
                if ($name eq 'create') {
                    my $config = PVE::HA::Config::read_resources_config();
                    return $native->($param) if $config->{ids}->{$sid};
                    PVE::HA::Resources::Docker::helper($info, 'enroll', $info->{id}, $info->{node});
                }
                my $result = eval { $native->($param); };
                if (my $error = $@) {
                    eval { PVE::HA::Resources::Docker::helper($info, 'release', $info->{id}, $info->{node}); } if $name eq 'create';
                    die $error;
                }
                PVE::HA::Resources::Docker::helper($info, 'release', $info->{id}, $info->{node}) if $name eq 'delete';
                return $result;
            });
            die $@ if $@;
            return $result;
        };
    }
}

sub ha_managed {
    my ($identifier, $node) = @_;
    my $vmid = task_id($identifier, $node);
    return PVE::HA::Config::get_service_status("docker:$vmid")->{managed};
}

# Use the same destination allocation rule as native CT creation and cloning.
sub check_destination_pool {
    my ($param, $vmid) = @_;
    my $rpc = PVE::RPCEnvironment::get();
    my $user = $rpc->get_user();
    $rpc->check_pool_exist($param->{pool}) if $param->{pool};
    return if $rpc->check($user, defined($vmid) ? '/vms/' . worker_id($vmid) : '/vms', ['VM.Allocate'], 1);
    return if $param->{pool} && $rpc->check($user, '/pool/' . $param->{pool}, ['VM.Allocate'], 1);
    raise_perm_exc('DCT allocation');
}

sub hook_pools {
    require PVE::API2::Pool;
    require PVE::API2Tools;
    require PVE::BetterDocker::Backup;
    PVE::BetterDocker::Backup::hook_inventory('PVE::API2::Pool');
    for my $name (qw(index read_pool)) {
        my $method = PVE::API2::Pool->map_method_by_name($name);
        push @{$method->{parameters}->{properties}->{type}->{enum}}, 'docker';
        my $properties = $name eq 'index' ? $method->{returns}->{items}->{properties} : $method->{returns}->{properties};
        push @{$properties->{members}->{items}->{properties}->{type}->{enum}}, 'docker';
    }
    my $native = \&PVE::API2Tools::extract_vm_stats;
    no warnings 'redefine';
    *PVE::API2Tools::extract_vm_stats = sub {
        my ($vmid, $data) = @_;
        return $native->(@_) unless $data->{type} eq 'docker';
        my ($item) = grep { $_->{vmid} == $vmid } @{summaries()};
        die "DCT $vmid disappeared from inventory\n" unless $item;
        return { id => "docker/$item->{id}", vmid => $vmid + 0, type => 'docker',
            node => $item->{node}, name => $item->{name}, status => $item->{status},
            template => $item->{template} ? 1 : 0,
            uptime => $item->{metrics}->{uptime} // 0,
            cpu => $item->{metrics}->{cpu} // 0, maxcpu => $item->{metrics}->{cpus} // 0,
            mem => $item->{metrics}->{mem} // 0, maxmem => $item->{metrics}->{maxmem} // 0 };
    };
}

# Every extension route must declare an authorization policy. Unknown routes fail closed.
my %method_scopes = (
    list => 'audit', management => 'audit', rrddata => 'audit', notes => 'audit',
    capabilities => 'audit', pending => 'audit', snapshots => 'audit', snapshot_config => 'audit',
    network_config => 'audit', project_limits => 'audit', image_state => 'audit',
    termproxy => 'console', vncwebsocket => 'console', logs => 'console',
    storage_limit => 'disk', set_disk => 'disk', image_preview => 'images', image_apply => 'images',
    clone => 'clone', migrate => 'migrate', template => 'template', recover => 'configure',
    destroy => 'remove', settings => 'settings', revert_settings => 'settings',
    set_network => 'network', set_notes => 'options', snapshot_edit => 'snapshot',
    service_action => 'power', shutdown => 'power', detail => 'configure',
    create => 'configure', preview => 'configure', update => 'configure',
    reset_settings => 'configure', set_project_limits => 'configure',
    action => 'power', snapshot => 'snapshot', security_policy => 'audit', set_security_policy => 'configure',
);

sub method_scope {
    my ($name, $param) = @_;
    die "No authorization policy for DCT method $name\n" unless exists($method_scopes{$name});
    return 'trusted' if $name eq 'list' && $param->{diagnostics};
    return 'remove' if $name eq 'action' && ($param->{action} // '') eq 'delete';
    return 'rollback' if $name eq 'snapshot' && ($param->{action} // '') eq 'restore';
    return $method_scopes{$name};
}

sub request_context {
    my ($identifier, $node) = @_;
    return '' unless defined($identifier);
    my ($info) = grep { $_->{id} eq $identifier && $_->{node} eq $node } @{summaries()};
    die "DCT ownership changed\n" unless $info;
    return JSON->new->canonical->encode({ id => $identifier, node => $node,
        vmid => $info->{vmid}, runtime_mode => $info->{runtime_mode} });
}

sub register_docker_method {
    my ($method) = @_;
    my $code = $method->{code};
    my $name = $method->{name};
    method_scope($name, {});
    $method->{permissions} = { user => 'all' };
    $method->{code} = sub {
        my ($param) = @_;
        my $context = request_context($param->{id}, $param->{node});
        # Websocket authorization runs in unprivileged pveproxy, like native LXC.
        # The root terminal helper checks deployment safety again before exec.
        my $caps;
        if ($name eq 'vncwebsocket' || $name eq 'termproxy') {
            my $rpc = PVE::RPCEnvironment::get();
            my $vmid = task_id($param->{id}, $param->{node});
            $caps = {console => $rpc->check($rpc->get_user(), "/vms/$vmid", ['VM.Console'], 1),
                trusted => ($rpc->get_user() eq 'root@pam' || $rpc->check($rpc->get_user(), "/nodes/$param->{node}", ['Sys.Console'], 1)) ? 1 : 0};
        } else {
            $caps = capabilities($param->{id}, $param->{node});
        }
        if ($param->{id} && $param->{node} && $name =~ /^(?:image_preview|image_apply|update|template|destroy|service_action|set_disk|settings|set_project_limits|reset_settings|set_security_policy|snapshot)$/ &&
            ($name ne 'snapshot' || ($param->{action} // '') eq 'restore') && ha_managed($param->{id}, $param->{node})) {
            die "Remove the DCT from HA before changing its deployment, services or persistent state\n";
        }
        my $scope = method_scope($name, $param);
        my $creating = $name eq 'create' || ($name eq 'preview' && !$param->{id});
        raise_perm_exc('DCT') unless $creating || ($name eq 'list' && !$param->{diagnostics}) || $caps->{$scope};
        if ($creating) {
            my $rpc = PVE::RPCEnvironment::get();
            raise_perm_exc('DCT') unless defined($param->{deployment});
            my $data = decode_json($param->{deployment});
            my $vmid = $param->{newid} // $data->{vmid};
            check_destination_pool($param, $vmid);
        }
        my $rpc = PVE::RPCEnvironment::get();
        my $user = $rpc->get_user();
        die "DCT ownership changed during authorization\n" if request_context($param->{id}, $param->{node}) ne $context;
        local $ENV{BETTERDOCKER_REQUEST_CONTEXT} = $context;
        local $ENV{BETTERDOCKER_TASK_USER} = $user;
        local $ENV{BETTERDOCKER_TRUSTED} = $caps->{trusted} ? '1' : '0';
        local $ENV{BETTERDOCKER_REQUIRE_SAFE} = (!$caps->{trusted} && ($scope =~ /^(configure|images|settings|console|disk|clone|migrate|template|remove)$/ || $name eq 'set_network' || $scope eq 'rollback')) ? '1' : '0';
        # Passed only by this wrapper; the worker checks changed fields under its lock.
        my @keys;
        push @keys, qw(cpus cpuunits) if $caps->{cpu};
        push @keys, qw(memory swap) if $caps->{memory};
        push @keys, qw(dns dns_search bridge ipv4mode cidr hwaddr mtu firewall) if $caps->{network};
        push @keys, qw(pids restart protection description tags onboot startup user read_only cap_drop no_new_privileges apparmor) if $caps->{options};
        push @keys, 'hookscript' if $user eq 'root@pam';
        push @keys, 'disk' if $caps->{disk};
        local $ENV{BETTERDOCKER_CONFIG_KEYS} = $caps->{configure} ? '*' : join(',', @keys);
        local $ENV{BETTERDOCKER_TASK_SYS_MODIFY} = ($user eq 'root@pam' ||
            $rpc->check($user, "/nodes/$param->{node}", ['Sys.Modify'], 1)) ? '1' : '0';
        if ($name =~ /^(create|clone|preview)$/ || $name eq 'set_network') {
            my $deployment = $param->{deployment} ? decode_json($param->{deployment}) : $param;
            if (my $bridge = $deployment->{bridge}) {
                die "Invalid bridge\n" unless $bridge =~ /\A([a-zA-Z0-9_.-]{1,15})\z/;
                $bridge = $1;
                require PVE::LXC;
                PVE::LXC::check_bridge_access($rpc, $user, "name=eth0,bridge=$bridge");
            }
        }
        return $code->($param);
    };
    __PACKAGE__->register_method($method);
}

# Extend only the native power list, not get_vmlist consumers such as vzdump/HA.
sub append_lifecycle_list {
    my ($list, $node, $autostart, $filter) = @_;
    my %wanted = map { $_ => 1 } PVE::Tools::split_list($filter // '');
    for my $item (@{summaries()}) {
        next if (defined($node) && $item->{node} ne $node) || !$item->{vmid};
        next if defined($filter) && !$wanted{$item->{vmid}};
        next if $item->{template} || $item->{migration};
        next if !defined($filter) && PVE::HA::Config::get_service_status("docker:$item->{vmid}")->{managed};
        next if $autostart && !$item->{onboot};
        my $startup = $item->{startup} ? PVE::JSONSchema::pve_parse_startup_order($item->{startup}) : {};
        die "Invalid DCT startup configuration\n" unless $startup;
        my $vmid = worker_id($item->{vmid});
        my $order = $startup->{order} // LONG_MAX;
        die "Guest ID collision for DCT $vmid\n" if grep { exists($_->{$vmid}) } values %$list;
        $list->{$order}->{$vmid} = { %$startup, type => 'docker', node => $item->{node} };
    }
    return $list;
}


sub append_migration_list {
    my ($list, $node, $filter) = @_;
    my %wanted = map { $_ => 1 } PVE::Tools::split_list($filter // '');
    for my $item (@{summaries()}) {
        next if !$item->{vmid} || (defined($node) && $item->{node} ne $node);
        next if defined($filter) && !$wanted{$item->{vmid}};
        my $vmid = worker_id($item->{vmid});
        die "Guest ID collision for DCT $vmid\n" if exists($list->{$vmid});
        $list->{$vmid} = { type => 'docker', node => $item->{node} };
    }
}

sub node_migrate {
    my ($vmid, $node, $target) = @_;
    my $id = lifecycle_identifier($vmid, $node);
    my $state = decode_json(output(['access-state', $id, $node]));
    die "DCT $vmid must be stopped before migration\n"
        if $state->{active_containers} && !ha_managed($id, $node);
    print "Migrating DCT $vmid\n";
    return __PACKAGE__->map_method_by_name('migrate')->{code}->({ id => $id, node => $node, target => $target });
}

our $bulk_action;
sub hook_bulk_operations {
    return if $native_operation_guards{'bulk'}++;
    my $class = 'PVE::API2::Cluster::BulkAction::Guest';
    for my $action (qw(start shutdown migrate suspend)) {
        my $method = $class->map_method_by_name($action) or die "Unsupported PVE bulk API\n";
        my $native = $method->{code};
        $method->{code} = sub { local $bulk_action = $action; return $native->(@_); };
    }
    my $native_tasks = \&PVE::API2::Cluster::BulkAction::Guest::handle_task_foreach_guest;
    my $native_type = \&PVE::API2::Cluster::BulkAction::Guest::get_type_text;
    no warnings 'redefine';
    *PVE::API2::Cluster::BulkAction::Guest::get_type_text = sub {
        return $_[0] eq 'docker' ? 'DCT' : $native_type->(@_);
    };
    *PVE::API2::Cluster::BulkAction::Guest::handle_task_foreach_guest = sub {
        my ($list, $max, $start, $check) = @_;
        my $action = $bulk_action;
        my $dispatch = sub {
            my ($client, $vmid, $guest) = @_;
            return $start->(@_) unless $guest->{type} eq 'docker';
            return $start->(@_) if $action eq 'suspend'; # native explicit unsupported warning
            my $id = lifecycle_identifier($vmid, $guest->{node});
            my $base = "/nodes/$guest->{node}/docker/$id";
            # The native callback owns timeout, target and task handling. Translate
            # only its guest URLs and parameters, retaining the caller's credentials.
            my $adapter = bless { client => $client, base => $base }, 'PVE::BetterDocker::BulkClient';
            return $start->($adapter, $vmid, $guest);
        };
        return $native_tasks->($list, $max, $dispatch, $check);
    };
}

sub lifecycle_identifier {
    my ($vmid, $node) = @_;
    for my $item (@{summaries()}) {
        if ($item->{node} eq $node && ($item->{vmid} // 0) == $vmid && $item->{id} =~ /\A([0-9a-f]{32})\z/) {
            return $1;
        }
    }
    die "DCT not found on node\n";
}

sub node_start {
    my ($vmid, $node) = @_;
    my $id = lifecycle_identifier($vmid, $node);
    my $state = decode_json(output(['access-state', $id, $node]));
    return undef if $state->{status} eq 'running';
    return __PACKAGE__->map_method_by_name('action')->{code}->({ id => $id, node => $node, action => 'start' });
}

sub node_stop {
    my ($vmid, $node, $timeout, $force) = @_;
    my $id = lifecycle_identifier($vmid, $node);
    raise_perm_exc('DCT') unless capabilities($id, $node)->{power};
    if (ha_managed($id, $node)) {
        return __PACKAGE__->map_method_by_name('action')->{code}->({ id => $id, node => $node,
            action => 'shutdown', timeout => $timeout });
    }
    my $state = decode_json(output(['access-state', $id, $node]));
    return undef unless $state->{active_containers};
    my $rpc = PVE::RPCEnvironment::get();
    my $user = $rpc->get_user();
    return $rpc->fork_worker('betterdockershutdown', worker_id($vmid), $user, sub {
        local $ENV{BETTERDOCKER_TASK_USER} = $user;
        output(['lifecycle-stop', $id, $node, $timeout, $force ? 1 : 0]);
    });
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
        my $rpc = PVE::RPCEnvironment::get();
        my %pools = map { $_->{pool} => $_ } grep { $_->{type} eq 'pool' } @$resources;
        for my $item (@{summaries()}) {
            my $pool = $rpc->{user_cfg}->{vms}->{$item->{vmid}};
            if ($pool && $pools{$pool} && $item->{metrics}->{uptime}) {
                my $entry = $pools{$pool};
                my $metrics = $item->{metrics};
                $entry->{uptime} = $metrics->{uptime} if ($entry->{uptime} // 0) < $metrics->{uptime};
                $entry->{mem} += $metrics->{mem} // 0;
                $entry->{maxmem} += $metrics->{maxmem} // 0;
                my $cpus = $metrics->{cpus} // 0;
                my $total = ($entry->{maxcpu} // 0) + $cpus;
                $entry->{cpu} = (($entry->{cpu} // 0) * ($entry->{maxcpu} // 0) + ($metrics->{cpu} // 0) * $cpus) / $total if $total;
                $entry->{maxcpu} = $total;
            }
            next unless capabilities($item->{id}, $item->{node})->{audit};
            my $status = $item->{status};
            $status = 'unknown' if exists($nodes{$item->{node}}) && $nodes{$item->{node}} ne 'online';
            push @$resources, {
                id => "docker/$item->{id}", vmid => $item->{vmid} + 0, type => 'docker', name => $item->{name},
                text => $item->{vmid} ? "$item->{vmid} ($item->{name})" : $item->{name},
                node => $item->{node}, status => $status, uptime => ($status eq 'running' || $status eq 'partial') ? ($item->{metrics}->{uptime} // 0) : 0,
                pool => PVE::RPCEnvironment::get()->{user_cfg}->{vms}->{$item->{vmid}} // '',
                tags => $item->{tags} // '', protection => $item->{protection} // 0, template => $item->{template} ? 1 : 0,
                cpu => $item->{metrics}->{cpu} // 0, maxcpu => $item->{metrics}->{cpus} // 0,
                mem => $item->{metrics}->{mem} // 0, maxmem => $item->{metrics}->{maxmem} // 0,
                hastate => PVE::HA::Config::get_service_status("docker:$item->{vmid}")->{state} // 'unmanaged',
            };
            delete $resources->[-1]->{pool} unless $pool && $rpc->check($rpc->get_user(), "/pool/$pool", ['Pool.Audit'], 1);
        }
        return $resources;
    };
}


# Docker archives must never be restored by the native VM/CT restore handlers.
sub restore_backup {
        my ($param) = @_;
        my $rpc = PVE::RPCEnvironment::get();
        my $user = $rpc->get_user();
        my $cfg = PVE::Storage::config();
        PVE::Storage::check_volume_access($rpc, $user, $cfg, undef, $param->{archive});
        check_destination_pool($param, $param->{newid});
        $rpc->check($user, '/storage/' . $param->{storage}, ['Datastore.AllocateSpace']) if $param->{storage};
        my $deployment = decode_json($param->{deployment});
        die "Deployment overrides must be an object\n" unless ref($deployment) eq 'HASH';
        my ($fh, $path) = tempfile('betterdocker-restore-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh encode_json({ deployment => $deployment, storage => $param->{storage} // '' }); close $fh;
        my $identifier = output(['new-id']); chomp $identifier;
        $identifier = worker_id($identifier);
        my $vmid = eval { reserve_guest_id($identifier, $param->{node}, $param->{newid}); };
        if (my $error = $@) { unlink $path; die $error; }
        my $upid = eval { $rpc->fork_worker('betterdockerrestore', $vmid, $user, sub {
            eval { PVE::Tools::run_command(['/usr/local/lib/betterdocker/backup.pl', 'restore',
                $param->{archive}, $identifier, $param->{node}, $path]); };
            my $error = $@;
            unlink $path;
            output(['release-id', $identifier]) if $error;
            die $error if $error;
            PVE::AccessControl::add_vm_to_pool($vmid, $param->{pool}) if $param->{pool};
        }); };
        if (my $error = $@) { unlink $path; output(['release-id', $identifier]); die $error; }
        return $upid;
}


# Keep termproxy and websocket on the request node, as native LXC does. Only
# the container shell runs remotely, through the cluster's existing SSH trust.
sub console_helper_command {
    my ($param, $action, $shell, $interactive) = @_;
    my $cmd = [$helper, $action, $param->{id}, $param->{node}, $param->{container}, $shell];
    require PVE::INotify;
    return $cmd if $param->{node} eq 'localhost' || $param->{node} eq PVE::INotify::nodename();
    require PVE::SSHInfo;
    my $sshinfo = PVE::SSHInfo::get_ssh_info($param->{node});
    my $ssh = PVE::SSHInfo::ssh_info_to_command($sshinfo, $interactive ? ('-t') : ());
    my @environment = map { $_ . '=' . ($ENV{$_} // '') }
        qw(BETTERDOCKER_TRUSTED BETTERDOCKER_TASK_USER BETTERDOCKER_REQUIRE_SAFE BETTERDOCKER_REQUEST_CONTEXT);
    return [@$ssh, PVE::Tools::cmd2string(['env', @environment, @$cmd])];
}


1;

require PVE::API2::BetterDocker::Inventory;
PVE::API2::BetterDocker::Inventory::register_api();
require PVE::API2::BetterDocker::Deployment;
PVE::API2::BetterDocker::Deployment::register_api();
require PVE::API2::BetterDocker::Transfer;
PVE::API2::BetterDocker::Transfer::register_api();
require PVE::API2::BetterDocker::Network;
PVE::API2::BetterDocker::Network::register_api();
require PVE::API2::BetterDocker::Power;
PVE::API2::BetterDocker::Power::register_api();
require PVE::API2::BetterDocker::Settings;
PVE::API2::BetterDocker::Settings::register_api();
require PVE::API2::BetterDocker::Storage;
PVE::API2::BetterDocker::Storage::register_api();
require PVE::API2::BetterDocker::Console;
PVE::API2::BetterDocker::Console::register_api();
require PVE::API2::BetterDocker::Snapshots;
PVE::API2::BetterDocker::Snapshots::register_api();

PVE::BetterDocker::Firewall::register_api(__PACKAGE__);
package PVE::BetterDocker::BulkClient;
sub get {
    my ($self, $url) = @_;
    $url =~ s!^/nodes/[^/]+/docker/[0-9]+!$self->{base}!;
    $url =~ s!/status/current$!/management!;
    my $state = $self->{client}->get($url);
    # Native bulk start distinguishes a running VM from a paused VM via qmpstatus.
    $state->{status} = 'running' if $state->{status} eq 'partial';
    $state->{qmpstatus} = 'running' if $state->{status} eq 'running';
    return $state;
}
sub post {
    my ($self, $url, $params) = @_;
    $url =~ s!^/nodes/[^/]+/docker/[0-9]+!$self->{base}!;
    if ($url =~ s!/status/(start|shutdown)$!/action!) {
        my $action = $1;
        # DCT shutdown uses the native node lifecycle path for timeout/force behavior.
        if ($action eq 'shutdown') {
            return $self->{client}->post($self->{base} . '/shutdown', {
                timeout => $params->{timeout}, 'force-stop' => $params->{forceStop},
            });
        }
        return $self->{client}->post($url, { action => $action });
    }
    return $self->{client}->post($url, $params);
}
package PVE::API2::BetterDocker;

1;
