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
my $net0 = option('string', 'Published IPv4 endpoint: bridge=<bridge>,ip=<CIDR|dhcp>,hwaddr=<MAC>,mtu=<bytes>,firewall=<0|1>.');
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
my %commands = (
    limits => ['Read or set administrator-owned aggregate DCT limits.', ['vmid'], {
        vmid => $vmid, cpus => option('number', 'Aggregate CPU ceiling; 0 means unlimited.', minimum => 0, maximum => 8192),
        memory => option('integer', 'Aggregate memory ceiling in MiB; 0 means unlimited.', minimum => 0, maximum => 1048576),
        pids => option('integer', 'Aggregate PID ceiling; 0 means unlimited.', minimum => 0, maximum => 4194304) }],
    list => ['List DCTs on this node.', [], { cluster => option('boolean', 'List guests on all cluster nodes.') }],
    create => ['Create a DCT from a Compose file, deployment JSON file or Docker image.', ['vmid', 'source'], {
        vmid => $vmid, source => { type => 'string', description => 'Compose file, deployment JSON file or Docker image.' },
        node => option('string', 'Destination node. Defaults to the local node.'), hostname => $hostname,
        net0 => $net0, pool => $pool, start => option('boolean', 'Start after creation.', default => 0),
        ports => option('string', 'Published:container port mappings for an image, separated by commas.'),
        'env-file' => option('string', 'Import environment variables from this file.'),
    }],
    config => ['Get DCT configuration.', ['vmid'], { vmid => $vmid, service => $service, current => option('boolean', 'Show current values instead of pending values.') }],
    status => ['Show DCT status.', ['vmid'], { vmid => $vmid, verbose => option('boolean', 'Verbose output format.') }],
    pending => ['Show current and pending configuration.', ['vmid'], { vmid => $vmid, service => $service }],
    network => ['Show the published IPv4 endpoint configuration.', ['vmid'], { vmid => $vmid }],
    set => ['Set DCT options.', ['vmid'], { vmid => $vmid, %config, %resources, service => $service, net0 => $net0,
        delete => option('string', 'Remove startup or hookscript.', enum => ['startup', 'hookscript']) }],
    start => ['Start the DCT.', ['vmid'], { vmid => $vmid }],
    stop => ['Stop the DCT immediately.', ['vmid'], { vmid => $vmid }],
    shutdown => ['Shut down the DCT gracefully.', ['vmid'], { vmid => $vmid, timeout => $timeout }],
    reboot => ['Reboot the DCT.', ['vmid'], { vmid => $vmid, timeout => $timeout }],
    destroy => ['Remove the DCT, optionally deleting owned data.', ['vmid'], { vmid => $vmid, 'destroy-owned-data' => option('boolean', 'Remove owned disks, volumes and project files.', default => 0) }],
    migrate => ['Migrate a stopped DCT to another node, or request native HA relocation.', ['vmid', 'target'], {
        vmid => $vmid, target => { type => 'string', description => 'Target node.' }, 'target-storage' => $storage }],
    clone => ['Create an independent full clone, initially stopped.', ['vmid', 'newid'], {
        vmid => $vmid, newid => $vmid, hostname => $hostname, net0 => { %$net0, optional => 0 }, pool => $pool,
        storage => $storage, full => option('boolean', 'Create a full copy. Linked clones are unavailable.', enum => [1], default => 1) }],
    listsnapshot => ['List all snapshots.', ['vmid'], { vmid => $vmid }],
    snapshot => ['Snapshot the DCT.', ['vmid', 'snapname'], { vmid => $vmid, snapname => $snapname,
        description => option('string', 'A textual description or comment.') }],
    delsnapshot => ['Delete a DCT snapshot.', ['vmid', 'snapname'], { vmid => $vmid, snapname => $snapname }],
    rollback => ['Roll back to a snapshot. The DCT remains stopped.', ['vmid', 'snapname'], { vmid => $vmid, snapname => $snapname }],
    template => ['Convert a stopped DCT to a full-clone template.', ['vmid'], { vmid => $vmid }],
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
