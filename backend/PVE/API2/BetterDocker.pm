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

sub output {
    my ($args) = @_;
    my $text = '';
    PVE::Tools::run_command([$helper, @$args], outfunc => sub { $text .= "$_[0]\n"; });
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
        snapshot => $granted->('VM.Snapshot'),
        rollback => $granted->('VM.Snapshot.Rollback'),
        power => $granted->('VM.PowerMgmt'),
        ha => ($root_user || $rpc->check($user, '/', ['Sys.Modify'], 1)) ? 1 : 0,
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
    for my $key (qw(dct_name dct_status)) {
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
                    $item->{dct_status} = ($disk_states{$id}->{volid} // '') eq $item->{volid} ? 'in-use' : 'unused';
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

sub register_docker_method {
    my ($method) = @_;
    my $code = $method->{code};
    my $name = $method->{name};
    $method->{permissions} = { user => 'all' };
    $method->{code} = sub {
        my ($param) = @_;
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
        if ($param->{id} && $param->{node} && $name =~ /^(?:update|template|destroy|service_action|settings|reset_settings|snapshot)$/ &&
            ($name ne 'snapshot' || ($param->{action} // '') eq 'restore') && ha_managed($param->{id}, $param->{node})) {
            die "Remove the DCT from HA before changing its deployment, services or persistent state\n";
        }
        my $scope = $name eq 'list' && $param->{diagnostics} ? 'trusted' :
                    $name =~ /^(list|management|rrddata|notes|capabilities|pending|snapshots|snapshot_config|network_config)$/ ? 'audit' :
                    $name =~ /^(termproxy|vncwebsocket|logs)$/ ? 'console' :
                    $name =~ /^(storage_limit|storage_config)$/ ? 'disk' :
                    $name eq 'clone' ? 'clone' : $name eq 'migrate' ? 'migrate' :
                    $name eq 'template' ? 'template' :
                    ($name eq 'destroy' || ($name eq 'action' && ($param->{action} // '') eq 'delete')) ? 'remove' :
                    ($name eq 'settings' || $name eq 'revert_settings') ? 'settings' :
                    $name eq 'set_network' ? 'network' : $name eq 'set_notes' ? 'options' :
                    ($name eq 'snapshot' && ($param->{action} // '') eq 'restore') ? 'rollback' :
                    ($name eq 'snapshot' || $name eq 'snapshot_edit') ? 'snapshot' :
                    $name eq 'action' || $name eq 'service_action' || $name eq 'shutdown' ? 'power' : 'configure';
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
        local $ENV{BETTERDOCKER_TASK_USER} = $user;
        local $ENV{BETTERDOCKER_TRUSTED} = $caps->{trusted} ? '1' : '0';
        local $ENV{BETTERDOCKER_REQUIRE_SAFE} = (!$caps->{trusted} && ($scope =~ /^(configure|console|disk|clone|migrate|template|remove)$/ || $name eq 'set_network' || $scope eq 'rollback')) ? '1' : '0';
        # Passed only by this wrapper; the worker checks changed fields under its lock.
        my @keys;
        push @keys, qw(cpus cpuunits) if $caps->{cpu};
        push @keys, qw(memory swap) if $caps->{memory};
        push @keys, qw(dns dns_search bridge ipv4mode cidr hwaddr mtu firewall) if $caps->{network};
        push @keys, qw(pids restart protection description tags onboot startup) if $caps->{options};
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
        return restore_backup($param) if $param->{archive};
        die "newid and storage are restore options\n" if defined($param->{newid}) || defined($param->{storage});
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
            eval { PVE::Tools::run_command([$helper, 'create', $path, $param->{node}, $identifier, $param->{hookscript} // '', '0']); };
            my $err = $@; unlink $path;
            # Membership precedes Start, like LXC, and keeps a retained failed
            # deployment accessible to the pool owner for diagnosis/removal.
            if ($param->{pool} && grep { $_->{id} eq $identifier } @{summaries()}) {
                PVE::AccessControl::add_vm_to_pool($vmid, $param->{pool});
            }
            output(['release-id', $identifier]) if $err;
            die $err if $err;
            PVE::Tools::run_command([$helper, 'action', $identifier, $param->{node}, 'start']) if $param->{start} // 1;
        }); };
        if (my $err = $@) { unlink $path; output(['release-id', $identifier]); die $err; }
        return $upid;
    },
});

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

register_docker_method({
    name => 'clone', path => '{id}/clone', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Create an independent full clone, initially stopped.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, digest => $digest,
        newid => { type => 'integer', minimum => 100, optional => 1 },
        deployment => { type => 'string', maxLength => 262144 },
        storage => { type => 'string', format => 'pve-storage-id', optional => 1 },
        pool => { type => 'string', format => 'pve-poolid', optional => 1 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        check_digest($param);
        my $rpc = PVE::RPCEnvironment::get();
        my $user = $rpc->get_user();
        $rpc->check($user, '/vms/' . task_id($param->{id}, $param->{node}), ['VM.Clone']);
        check_destination_pool($param, $param->{newid});
        $rpc->check($user, '/storage/' . $param->{storage}, ['Datastore.AllocateSpace']) if $param->{storage};
        my $deployment = decode_json($param->{deployment});
        die "Clone deployment overrides must be an object\n" unless ref($deployment) eq 'HASH';
        my ($fh, $path) = tempfile('betterdocker-clone-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh encode_json({ deployment => $deployment, storage => $param->{storage} // '', digest => $param->{digest} }); close $fh;
        my $identifier = output(['new-id']); chomp $identifier;
        $identifier = worker_id($identifier);
        my $vmid = eval { reserve_guest_id($identifier, $param->{node}, $param->{newid}); };
        if (my $error = $@) { unlink $path; die $error; }
        my $upid = eval { $rpc->fork_worker('betterdockerclone', $vmid, $user, sub {
            eval { PVE::Tools::run_command(['/usr/local/lib/betterdocker/guest_state.py', 'clone',
                $param->{id}, $param->{node}, $identifier, $path]); };
            my $error = $@;
            unlink $path;
            output(['release-id', $identifier]) if $error;
            die $error if $error;
            PVE::AccessControl::add_vm_to_pool($vmid, $param->{pool}) if $param->{pool};
        }); };
        if (my $error = $@) { unlink $path; output(['release-id', $identifier]); die $error; }
        return $upid;
    },
});

register_docker_method({
    name => 'migrate', path => '{id}/migrate', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Move a stopped DCT to another cluster node with an exclusive ownership handoff.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, digest => $digest, target => $node,
        storage => { type => 'string', format => 'pve-storage-id', optional => 1 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        check_digest($param);
        my $rpc = PVE::RPCEnvironment::get();
        my $user = $rpc->get_user();
        $rpc->check($user, '/vms/' . task_id($param->{id}, $param->{node}), ['VM.Migrate']);
        $rpc->check($user, '/storage/' . $param->{storage}, ['Datastore.AllocateSpace']) if $param->{storage};
        die "Choose another cluster node\n" if $param->{node} eq $param->{target};
        PVE::Cluster::check_cfs_quorum();
        PVE::Cluster::remote_node_ip($param->{target});
        if (ha_managed($param->{id}, $param->{node}) && $rpc->{type} ne 'ha') {
            die "HA relocation preserves storage; omit the storage override\n" if $param->{storage};
            my $vmid = task_id($param->{id}, $param->{node});
            return $rpc->fork_worker('hamigrate', $vmid, $user, sub {
                PVE::Tools::run_command(['ha-manager', 'crm-command', 'relocate', "docker:$vmid", $param->{target}]);
            });
        }
        my $state = decode_json(output(['access-state', $param->{id}, $param->{node}]));
        die "DCT must be stopped before migration\n" if $state->{active_containers};
        return $rpc->fork_worker('betterdockermigrate', task_id($param->{id}, $param->{node}), $user, sub {
            PVE::Tools::run_command(['/usr/local/lib/betterdocker/transfer.py', 'migrate',
                $param->{id}, $param->{node}, $param->{target}, $param->{storage} // '', $param->{digest} // '']);
        });
    },
});

register_docker_method({
    name => 'template', path => '{id}/template', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Convert a stopped DCT to a non-bootable full-clone template.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        check_digest($param);
        my $rpc = PVE::RPCEnvironment::get();
        $rpc->check($rpc->get_user(), '/vms/' . task_id($param->{id}, $param->{node}), ['VM.Allocate']);
        return $rpc->fork_worker('betterdockertemplate', task_id($param->{id}, $param->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command(['/usr/local/lib/betterdocker/guest_state.py', 'template', $param->{id}, $param->{node}, $param->{digest} // '']);
        });
    },
});

register_docker_method({
    name => 'update', path => '{id}', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Edit a deployment and apply Compose changes without changing its project IP.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, digest => $digest, deployment => { type => 'string', maxLength => 262144 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        check_digest($param);
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh $param->{deployment}; close $fh;
        eval { PVE::Tools::run_command([$helper, 'validate', $path]); };
        if (my $err = $@) { unlink $path; die $err; }
        my $rpcenv = PVE::RPCEnvironment::get();
        my $upid = eval { $rpcenv->fork_worker('betterdockerupdate', task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'update', $param->{id}, $param->{node}, $path, $param->{digest} // '']); };
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
    name => 'network_config', path => '{id}/network', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read the DCT IPv4 endpoint configuration.', permissions => $root,
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub { my ($param) = @_; return decode_json(output(['network-config', $param->{id}, $param->{node}])); },
});

register_docker_method({
    name => 'set_network', path => '{id}/network', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Change the DCT endpoint; running guests apply pending changes at the next full start or reboot.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id,
        bridge => { type => 'string', pattern => '^[a-zA-Z0-9_.-]{1,15}$', optional => 1 },
        ipv4mode => { type => 'string', enum => ['static', 'dhcp'], optional => 1 },
        cidr => { type => 'string', maxLength => 64, optional => 1 },
        hwaddr => { type => 'string', maxLength => 17, optional => 1 },
        mtu => { type => 'integer', minimum => 0, maximum => 9216, optional => 1 },
        firewall => { type => 'boolean', optional => 1 },
        revert => { type => 'boolean', optional => 1, description => 'Discard pending net0 changes.' },
        digest => { type => 'string', pattern => '^[a-f0-9]{64}$', optional => 1 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        check_digest($param);
        my %values = map { exists($param->{$_}) ? ($_ => $param->{$_}) : () } qw(bridge ipv4mode cidr hwaddr mtu firewall revert);
        $values{firewall} = $values{firewall} ? 1 : 0 if exists($values{firewall});
        if (exists($values{revert})) {
            die "Revert cannot be combined with network settings\n" if !$values{revert} || keys(%values) != 1;
            $values{revert} = 1;
        }
        my ($fh, $path) = tempfile('betterdocker-network-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh encode_json(\%values); close $fh;
        my $rpcenv = PVE::RPCEnvironment::get();
        my $upid = eval { $rpcenv->fork_worker('betterdockernetwork', task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'network', $param->{id}, $param->{node}, $path, $param->{digest} // '']); };
            my $err = $@; unlink $path; die $err if $err;
        }); };
        if (my $err = $@) { unlink $path; die $err; }
        return $upid;
    },
});

register_docker_method({
    name => 'shutdown', path => '{id}/shutdown', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Graceful shutdown with optional forced stop after timeout.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id,
        timeout => { type => 'integer', minimum => 0, maximum => 86400, optional => 1, default => 180 },
        'force-stop' => { type => 'boolean', optional => 1, default => 1 },
    } }, returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        if (ha_managed($param->{id}, $param->{node})) {
            return __PACKAGE__->map_method_by_name('action')->{code}->({ id => $param->{id}, node => $param->{node},
                action => 'shutdown', timeout => $param->{timeout} // 180 });
        }
        my $upid = node_stop(task_id($param->{id}, $param->{node}), $param->{node}, $param->{timeout} // 180, $param->{'force-stop'} // 1);
        return $upid if defined($upid);
        return __PACKAGE__->map_method_by_name('action')->{code}->({ id => $param->{id}, node => $param->{node},
            action => 'shutdown', timeout => $param->{timeout} // 180 });
    },
});

register_docker_method({
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
        die "can't remove DCT - protection mode enabled\n" if $action eq 'delete' && decode_json(output(['detail', $param->{id}, $param->{node}]))->{protection};
        die "Overrule is only available for Stop\n" if $param->{'overrule-shutdown'} && $action ne 'force-stop';
        if (ha_managed($param->{id}, $param->{node}) && $rpcenv->{type} ne 'ha') {
            die "Remove the DCT from HA before deleting it\n" if $action eq 'delete';
            die "Overrule is not applicable to HA resources\n" if $param->{'overrule-shutdown'};
            if ($action =~ /^(?:start|stop|shutdown|force-stop)$/) {
                my $vmid = task_id($param->{id}, $param->{node});
                my $command = $action eq 'start' ? ['ha-manager', 'set', "docker:$vmid", '--state', 'started'] :
                    ['ha-manager', 'crm-command', 'stop', "docker:$vmid", $action eq 'force-stop' ? 0 : $param->{timeout} // 60];
                return $rpcenv->fork_worker($action eq 'start' ? 'hastart' : 'hastop', $vmid, $rpcenv->get_user(),
                    sub { PVE::Tools::run_command($command); });
            }
        }
        my $ha_agent = ($rpcenv->{type} // '') eq 'ha';
        my $vmid = task_id($param->{id}, $param->{node});
        (my $task_action = $action) =~ s/-//g;
        return $rpcenv->fork_worker('betterdocker' . $task_action, task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
            if ($ha_agent) {
                PVE::Tools::run_command(['/usr/local/lib/betterdocker/ha.py', 'power', $param->{id}, $param->{node}, $action, $param->{timeout} // 60]);
            } else {
                PVE::Tools::run_command([$helper, 'action', $param->{id}, $param->{node}, $action, $param->{timeout} // 60, $param->{'overrule-shutdown'} ? 1 : 0]);
                PVE::AccessControl::remove_vm_access($vmid) if $action eq 'delete';
            }
        });
    },
});

register_docker_method({
    name => 'destroy', path => '{id}', method => 'DELETE', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Remove the project while retaining persistent data and deployment files.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        my $rpcenv = PVE::RPCEnvironment::get();
        die "can't remove DCT - protection mode enabled\n" if decode_json(output(['detail', $param->{id}, $param->{node}]))->{protection};
        my $vmid = task_id($param->{id}, $param->{node});
        return $rpcenv->fork_worker('betterdockerdelete', $vmid, $rpcenv->get_user(), sub {
            PVE::Tools::run_command([$helper, 'action', $param->{id}, $param->{node}, 'delete']);
            PVE::AccessControl::remove_vm_access($vmid);
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
        $info->{ha} = PVE::HA::Config::get_service_status("docker:" . task_id($param->{id}, $param->{node}));
        my $op = $info->{operation} // {};
        my $owner = $op->{user} // '';
        my $actor = $ENV{BETTERDOCKER_TASK_USER};
        $owner =~ s/!.*$//; $actor =~ s/!.*$//;
        $info->{capabilities}->{overrule_shutdown} = (!$info->{ha}->{managed} && $op->{active} && $op->{action} eq 'shutdown' &&
            $info->{capabilities}->{power} && ($ENV{BETTERDOCKER_TASK_SYS_MODIFY} eq '1' ||
            ($owner ne '' && $owner eq $actor))) ? 1 : 0;
        delete $info->{deployment} unless $info->{capabilities}->{configure};
        delete $info->{digest} unless $info->{capabilities}->{settings} || $info->{capabilities}->{clone} || $info->{capabilities}->{migrate} || $info->{capabilities}->{template};
        return $info; },
});

register_docker_method({
    name => 'capabilities', path => '{id}/capabilities', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read permissions and current operation state for this project.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
    returns => { type => 'object' },
    code => sub {
        my ($p) = @_;
        my $state = decode_json(output(['access-state', $p->{id}, $p->{node}]));
        $state->{capabilities} = capabilities($p->{id}, $p->{node});
        $state->{ha} = PVE::HA::Config::get_service_status("docker:" . task_id($p->{id}, $p->{node}));
        my $op = $state->{operation} // {};
        my $owner = $op->{user} // ''; my $actor = $ENV{BETTERDOCKER_TASK_USER};
        $owner =~ s/!.*$//; $actor =~ s/!.*$//;
        $state->{capabilities}->{overrule_shutdown} = (!$state->{ha}->{managed} && $op->{active} && $op->{action} eq 'shutdown' &&
            !$op->{service} && $state->{capabilities}->{power} &&
            ($ENV{BETTERDOCKER_TASK_SYS_MODIFY} eq '1' || ($owner ne '' && $owner eq $actor))) ? 1 : 0;
        return $state;
    },
});

register_docker_method({
    name => 'pending', path => '{id}/pending', method => 'GET', protected => 1, proxyto => 'node',
    description => 'Read current and pending settings for one Compose service.',
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, service => { type => 'string', maxLength => 128 },
    } },
    returns => { type => 'array', items => { type => 'object' } },
    code => sub { my ($p) = @_; return decode_json(output(['pending', $p->{id}, $p->{node}, $p->{service}])); },
});

register_docker_method({
    name => 'revert_settings', path => '{id}/pending', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Discard selected pending settings without changing the running containers.',
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, digest => $digest, service => { type => 'string', maxLength => 128 },
        revert => { type => 'string', maxLength => 256 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($p) = @_;
        check_digest($p);
        my $rpc = PVE::RPCEnvironment::get();
        return $rpc->fork_worker('betterdockersettings', task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'revert-settings', $p->{id}, $p->{node}, $p->{service}, $p->{revert}, $p->{digest} // '']);
        });
    },
});

register_docker_method({
    name => 'settings', path => '{id}/settings', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Apply persistent resource, DNS and restart settings to one service.', permissions => $root,
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, digest => $digest, service => { type => 'string', maxLength => 128 },
        settings => { type => 'string', maxLength => 8192 },
    } },
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        check_digest($param);
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh $param->{settings}; close $fh;
        my $rpcenv = PVE::RPCEnvironment::get();
        my $upid = eval { $rpcenv->fork_worker('betterdockersettings', task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
            eval { PVE::Tools::run_command([$helper, 'settings', $param->{id}, $param->{node}, $param->{service}, $path, $param->{digest} // '']); };
            my $err = $@; unlink $path; die $err if $err;
        }); };
        if (my $err = $@) { unlink $path; die $err; }
        return $upid;
    },
});

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
        qw(BETTERDOCKER_TRUSTED BETTERDOCKER_TASK_USER BETTERDOCKER_REQUIRE_SAFE);
    return [@$ssh, PVE::Tools::cmd2string(['env', @environment, @$cmd])];
}

register_docker_method({
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
        PVE::Tools::run_command(console_helper_command($param, 'console-check', $shell, 0));
        my $rpcenv = PVE::RPCEnvironment::get();
        my $user = $rpcenv->get_user();
        my $authpath = "/vms/" . task_id($param->{id}, $param->{node});
        my $port = PVE::Tools::next_vnc_port();
        my $help = '';
        PVE::Tools::run_command(['/usr/bin/termproxy', '--help'], outfunc => sub { $help .= $_[0]; });
        my $modern = $help =~ /--ticket-fd/;
        my $ticket = PVE::AccessControl::assemble_vnc_ticket($user, $authpath, $modern ? $port : undef);
        my $upid = $rpcenv->fork_worker('betterdockerconsole', task_id($param->{id}, $param->{node}), $user, sub {
            my $cmd = ['/usr/bin/termproxy', $port, '--path', $authpath, '--perm', 'VM.Console'];
            if ($modern) {
                pipe(my $rd, my $wr) or die "ticket pipe: $!";
                my $flags = fcntl($rd, F_GETFD, 0) // die "ticket flags: $!";
                fcntl($rd, F_SETFD, $flags & ~FD_CLOEXEC) // die "ticket fd: $!";
                push @$cmd, '--vncticket-endpoint', '--verify-port', '--ticket-fd', fileno($rd);
                push @$cmd, '--', @{console_helper_command($param, 'console', $shell, 1)};
                PVE::Tools::run_command($cmd, afterfork => sub { print {$wr} $ticket; close($wr); });
            } else {
                push @$cmd, '--', @{console_helper_command($param, 'console', $shell, 1)};
                PVE::Tools::run_command($cmd);
            }
        });
        PVE::Tools::wait_for_vnc_port($port);
        return { user => $user, ticket => $ticket, port => $port, upid => $upid };
    },
});

register_docker_method({
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
        PVE::AccessControl::verify_vnc_ticket($param->{vncticket}, $user, "/vms/" . task_id($param->{id}, $param->{node}), $help =~ /--ticket-fd/ ? $param->{port} : undef);
        return { port => $param->{port} };
    },
});

register_docker_method({
    name => 'rrddata', path => '{id}/rrddata', method => 'GET', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Read recorded Docker metrics in the native chart format (seven-day retention).',
    parameters => { additionalProperties => 0, properties => {
        node => $node, id => $id, timeframe => { type => 'string', enum => ['hour', 'day', 'week'] },
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
    code => sub {
        my ($p) = @_;
        my $config = { %{decode_json(output(['notes', $p->{id}, $p->{node}]))},
            %{decode_json(output(['tags', $p->{id}, $p->{node}]))},
            %{decode_json(output(['protection', $p->{id}, $p->{node}]))},
            %{decode_json(output(['lifecycle', $p->{id}, $p->{node}]))} };
        delete $config->{digest} unless capabilities($p->{id}, $p->{node})->{settings};
        return $config;
    },
});

register_docker_method({
    name => 'set_notes', path => '{id}/config', method => 'PUT', protected => 1, proxyto => 'node', permissions => $root,
    description => 'Save project notes.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id,
        description => { type => 'string', maxLength => 8192, optional => 1 },
        tags => { type => 'string', maxLength => 2048, optional => 1 },
        protection => { type => 'boolean', optional => 1 },
        onboot => { type => 'boolean', optional => 1 },
        startup => { type => 'string', format => 'pve-startup-order', optional => 1 },
        hookscript => { type => 'string', format => 'pve-volume-id', optional => 1 },
        delete => { type => 'string', enum => ['startup', 'hookscript'], optional => 1 }, digest => $digest } },
    returns => { type => 'null' },
    code => sub {
        my ($p) = @_;
        check_digest($p);
        if (defined($p->{tags}) && !capabilities($p->{id}, $p->{node})->{configure}) {
            my $old = decode_json(output(['tags', $p->{id}, $p->{node}]))->{tags};
            PVE::GuestHelpers::assert_tag_permissions(task_id($p->{id}, $p->{node}), $old, $p->{tags},
                PVE::RPCEnvironment::get(), PVE::RPCEnvironment::get()->get_user());
        }
        my %changes = map { $_ => $p->{$_} } grep { defined($p->{$_}) } qw(description tags protection onboot startup hookscript);
        $changes{protection} = $p->{protection} ? 1 : 0 if defined($p->{protection});
        $changes{onboot} = $p->{onboot} ? 1 : 0 if defined($p->{onboot});
        $changes{$p->{delete}} = '' if $p->{delete};
        if (exists($changes{hookscript})) {
            raise_perm_exc('changing the hookscript is only allowed for root@pam')
                unless PVE::RPCEnvironment::get()->get_user() eq 'root@pam';
            PVE::GuestHelpers::check_hookscript($changes{hookscript}) if $changes{hookscript};
        }
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path; print $fh encode_json(\%changes); close $fh;
        eval { output(['config', $p->{id}, $p->{node}, $path, $p->{digest} // '']); };
        my $err = $@; unlink $path; die $err if $err;
        return undef;
    },
});

register_docker_method({
    name => 'preview', path => '', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Validate Compose and preview non-secret deployment details.',
    parameters => { additionalProperties => 0, properties => { node => $node, pool => { type => 'string', format => 'pve-poolid', optional => 1 },
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
        return $rpc->fork_worker('betterdocker' . $task_action, task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'service-action', $p->{id}, $p->{node}, $p->{service}, $action, $p->{timeout} // 60, $p->{'overrule-shutdown'} ? 1 : 0]);
        });
    },
});

register_docker_method({
    name => 'reset_settings', path => '{id}/reset-settings', method => 'POST', protected => 1, proxyto => 'node',
    description => 'Restore Compose settings for one service while preserving stopped state.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
        service => { type => 'string', maxLength => 128 } } },
    returns => { type => 'string' },
    code => sub {
        my ($p) = @_;
        check_digest($p);
        my $rpc = PVE::RPCEnvironment::get();
        return $rpc->fork_worker('betterdockersettings', task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'reset-settings', $p->{id}, $p->{node}, $p->{service}, $p->{digest} // '']);
        });
    },
});


register_docker_method({
    name => 'storage_limit', path => '{id}/storage-limit', method => 'PUT', protected => 1, proxyto => 'node',
    description => 'Create or enlarge the bounded project data disk; services must be stopped.',
    parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
        size_mib => { type => 'integer', minimum => 256, maximum => 1048576 },
        storage => { type => 'string', format => 'pve-storage-id', optional => 1 } } },
    returns => { type => 'string' },
    code => sub {
        my ($p) = @_;
        check_digest($p);
        my $rpc = PVE::RPCEnvironment::get();
        my $storage = $p->{storage} // decode_json(output(['storage-config', $p->{id}, $p->{node}]))->{storage};
        $rpc->check($rpc->get_user(), '/storage/' . $storage, ['Datastore.AllocateSpace']) if $storage;
        return $rpc->fork_worker('betterdockersettings', task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
            PVE::Tools::run_command([$helper, 'storage-limit', $p->{id}, $p->{node}, $p->{size_mib}, $p->{digest} // '', $p->{storage} // '']);
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
        description => { type => 'string', maxLength => 4096 },
        digest => { type => 'string', pattern => '^[a-f0-9]{64}$', optional => 1 } } },
    returns => { type => 'null' },
    code => sub { my ($p) = @_; output(['snapshot-edit', $p->{id}, $p->{node}, $p->{snapname}, $p->{description}, $p->{digest} // '']); return undef; },
});

1;

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
