package PVE::CLI::dct;

use strict;
use warnings;
use JSON qw(encode_json);
use PVE::CLIHandler;
use PVE::JSONSchema qw(get_standard_option);
use PVE::RPCEnvironment;
use PVE::Tools;
use base qw(PVE::CLIHandler);

sub setup_environment {
    PVE::RPCEnvironment->setup_default_cli_env();
}

sub option {
    my ($type, $description, %extra) = @_;
    return { type => $type, description => $description, optional => 1, %extra };
}

my $vmid = get_standard_option('pve-vmid');
my $service = option('string', 'Compose service. Required when the DCT contains more than one service.');
my $net0 = option('string', 'Published endpoint: bridge=<bridge>,ip=<CIDR|dhcp>,ip6=<CIDR|none>,gw=<IPv4>,gw6=<IPv6>,tag=<VLAN>,rate=<MB/s>,link_down=<0|1>,ports=<service:port/protocol>.');
my $hostname = option('string', 'Name of the DCT.');
my $pool = option('string', 'Add the new DCT to this resource pool.');
my $storage = option('string', 'Destination storage for the managed data disk.', format => 'pve-storage-id');
my $snapname = { type => 'string', description => 'The name of the snapshot.', pattern => '[A-Za-z][A-Za-z0-9_-]{0,39}' };
my $timeout = option('integer', 'Timeout in seconds for graceful shutdown.', minimum => 1, maximum => 86400);
my %config = (
    description => option('string', 'Description shown in the Summary screen.', maxLength => 8192),
    tags => option('string', 'Semicolon-separated guest tags.'),
    onboot => option('boolean', 'Start the DCT during system boot.'),
    protection => option('boolean', 'Protect the DCT against removal.'),
    startup => get_standard_option('pve-startup-order', { optional => 1 }),
    hookscript => option('string', 'Executable hook script on snippet storage.', format => 'pve-volume-id'),
);
my %resources = (
    cpulimit => option('number', 'CPU limit per service instance; 0 means unlimited.', minimum => 0, maximum => 8192),
    cpuunits => option('integer', 'CPU weight per service instance.', minimum => 0, maximum => 262144),
    memory => option('integer', 'Memory limit per service instance in MiB; 0 means unlimited.', minimum => 0, maximum => 1048576),
    swap => option('integer', 'Additional swap per service instance in MiB; -1 means unlimited.', minimum => -1, maximum => 1048576),
    pids => option('integer', 'Process limit per service instance; 0 means unlimited.', minimum => 0, maximum => 4194304),
    nameserver => option('string', 'Space-separated DNS server addresses.'),
    searchdomain => option('string', 'Space-separated DNS search domains.'),
    'restart-policy' => option('string', 'Docker restart policy.'),
);
my %network_options = map { ('net' . $_) => $net0 } (0..15);
my %mount_options = map { ('mp' . $_) => option('string', 'Managed disk: storage=<ID>,size=<MiB>,mp=<path>,service=<name>,backup=<0|1>,ro=<0|1>. Use delete to detach.') } (1..15);
my %commands = (
    cleanup => ['Inspect or remove retained data after deleting a DCT.', ['project-id'], {
        'project-id' => { type => 'string', pattern => '^[0-9a-f]{32}$', description => 'Project ID reported when removing the guest.' },
        'destroy-owned-data' => option('boolean', 'Remove the inspected project-owned data. The default only prints the cleanup plan.', default => 0) }],
    limits => ['Read or set administrator-owned aggregate DCT limits.', ['vmid'], {
        vmid => $vmid, cpus => option('number', 'Aggregate CPU ceiling; 0 means unlimited.', minimum => 0, maximum => 8192),
        memory => option('integer', 'Aggregate memory ceiling in MiB; 0 means unlimited.', minimum => 0, maximum => 1048576),
        pids => option('integer', 'Aggregate PID ceiling; 0 means unlimited.', minimum => 0, maximum => 4194304),
        services => option('integer', 'Maximum Compose service count.', minimum => 0, maximum => 100000),
        replicas => option('integer', 'Maximum total replica count.', minimum => 0, maximum => 100000),
        exempt => option('boolean', 'Explicit administrator exemption from aggregate budgets.') }],
    list => ['List DCTs on this node.', [], { cluster => option('boolean', 'List guests on all cluster nodes.') }],
    create => ['Create a DCT from a Compose file, deployment JSON file or Docker image.', ['vmid', 'source'], {
        vmid => $vmid, source => { type => 'string', description => 'Compose file, deployment JSON file or Docker image.' },
        node => option('string', 'Destination node. Defaults to the local node.'), hostname => $hostname,
        %network_options, pool => $pool, start => option('boolean', 'Start after creation.', default => 0),
        ports => option('string', 'Published:container port mappings for an image, separated by commas.'),
        'env-file' => option('string', 'Import environment variables from this file.'),
    }],
    recover => ['Recover interrupted local configuration operations.', ['vmid'], { vmid => $vmid }],
    config => ['Get DCT configuration.', ['vmid'], { vmid => $vmid, service => $service, current => option('boolean', 'Show current values instead of pending values.') }],
    status => ['Show DCT status.', ['vmid'], { vmid => $vmid, verbose => option('boolean', 'Verbose output format.') }],
    pending => ['Show current and pending configuration.', ['vmid'], { vmid => $vmid, service => $service }],
    network => ['Show the published IPv4 endpoint configuration.', ['vmid'], { vmid => $vmid }],
    set => ['Set DCT options.', ['vmid'], { vmid => $vmid, %config, %resources, service => $service, %network_options, %mount_options, 'security-policy' => option('string', 'Mandatory workload policy.', enum => ['standard', 'hardened']), delete => option('string', 'Remove startup, hookscript or comma-separated network interfaces.') }],
    start => ['Start the DCT.', ['vmid'], { vmid => $vmid }],
    stop => ['Stop the DCT immediately.', ['vmid'], { vmid => $vmid }],
    shutdown => ['Shut down the DCT gracefully.', ['vmid'], { vmid => $vmid, timeout => $timeout }],
    reboot => ['Reboot the DCT.', ['vmid'], { vmid => $vmid, timeout => $timeout }],
    destroy => ['Remove the DCT, optionally deleting owned data.', ['vmid'], { vmid => $vmid, 'destroy-owned-data' => option('boolean', 'Remove owned disks, volumes and project files.', default => 0) }],
    migrate => ['Migrate a DCT to another node, optionally shutting down and restarting it.', ['vmid', 'target'], {
        vmid => $vmid, target => { type => 'string', description => 'Target node.' }, 'target-storage' => $storage, restart => option('boolean', 'Shutdown and restart a running DCT.', default => 0),
        timeout => option('integer', 'Shutdown timeout in seconds.', minimum => 1, maximum => 86400, default => 180) }],
    clone => ['Clone a DCT, initially stopped.', ['vmid', 'newid'], {
        vmid => $vmid, newid => $vmid, hostname => $hostname, net0 => { %$net0, optional => 0 }, pool => $pool,
        storage => $storage, full => option('boolean', 'Create a full copy. Set 0 for an LVM-thin template linked clone.', default => 1) }],
    listsnapshot => ['List all snapshots.', ['vmid'], { vmid => $vmid }],
    snapshot => ['Snapshot the DCT.', ['vmid', 'snapname'], { vmid => $vmid, snapname => $snapname,
        description => option('string', 'A textual description or comment.') }],
    delsnapshot => ['Delete a DCT snapshot.', ['vmid', 'snapname'], { vmid => $vmid, snapname => $snapname }],
    rollback => ['Roll back to a snapshot. The DCT remains stopped.', ['vmid', 'snapname'], { vmid => $vmid, snapname => $snapname }],
    template => ['Convert a stopped DCT to a template.', ['vmid'], { vmid => $vmid }],
);

for my $action (qw(status log wait stop)) {
    $commands{'task-' . $action} = [{'status' => 'Show task status.', 'log' => 'Show task log.', 'wait' => 'Wait for a task to finish.', 'stop' => 'Request task cancellation.'}->{$action}, ['upid'], {
        upid => { type => 'string', description => 'PVE task identifier.', pattern => '^UPID:[^:]+:.+' },
    }];
}

our $cmddef = {};
for my $command (sort keys %commands) {
    my ($description, $arguments, $properties) = @{$commands{$command}};
    $properties->{'output-format'} = option('string', 'Output format.', enum => ['text', 'json', 'json-pretty'], default => 'text');
    __PACKAGE__->register_method({
        name => $command, path => $command, method => 'POST', description => $description,
        parameters => { additionalProperties => 0, properties => $properties },
        returns => { type => 'null' },
        code => sub {
            my ($param) = @_;
            PVE::Tools::run_command(['/usr/bin/python3', '/usr/local/lib/betterdocker/dct.py', $command],
                input => encode_json($param),
                outfunc => sub { print "$_[0]\n"; }, errfunc => sub { print STDERR "$_[0]\n"; });
            return undef;
        },
    });
    if ($command =~ /^task-(.+)$/) {
        $cmddef->{task}->{$1} = [__PACKAGE__, $command, $arguments];
    } else {
        $cmddef->{$command} = [__PACKAGE__, $command, $arguments];
    }
}

1;
