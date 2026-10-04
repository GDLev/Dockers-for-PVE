package PVE::API2::BetterDocker;
use strict;
use warnings;
use base qw(PVE::RESTHandler);
use File::Temp qw(tempfile);
use PVE::API2::LXC;
use PVE::RPCEnvironment;
use PVE::Tools;

# Preserve schema callbacks: Storable cannot clone CODE references.
my $native = PVE::API2::LXC->map_method_by_name('create_vm')->{parameters};
my $schema = { %$native, properties => { %{$native->{properties}} } };
$schema->{properties}->{docker} = {
    type => 'string', maxLength => 262144,
    description => 'JSON: image, ports, environment, command, compose, mode.',
};
__PACKAGE__->register_method({
    name => 'create', path => '', method => 'POST',
    description => 'Create a dedicated unprivileged Docker LXC and provision Docker/Compose.',
    protected => 1, proxyto => 'node',
    # No user/check grant: PVE defaults to root@pam only.
    permissions => { description => 'Only root@pam can provision Docker machines.' },
    parameters => $schema,
    returns => { type => 'string' },
    code => sub {
        my ($param) = @_;
        my %ct = %$param;
        my $docker = delete $ct{docker};
        die "Restore and force are not supported\n" if $ct{restore} || $ct{force};
        die "Choose a Debian 12 or 13 standard template\n"
            unless $ct{ostemplate} =~ m{:vztmpl/debian-(?:12|13)-standard_[^/]+$};
        die "An independent network interface (net0) is required\n" unless $ct{net0} && $ct{net0} =~ /(?:^|,)ip=(?:dhcp|[0-9.]+\/[0-9]+)(?:,|$)/;
        $ct{unprivileged} = 1;
        $ct{features} = 'nesting=1,keyctl=1';
        $ct{start} = 0;
        $ct{tags} = join(';', grep { length } ($ct{tags} // '', 'betterdocker'));
        # Validate Docker settings before allocating storage or creating the guest.
        my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
        chmod 0600, $path;
        print $fh $docker;
        close $fh;
        eval { PVE::Tools::run_command(['/usr/local/lib/betterdocker/provision.py', '--validate', $path]); };
        if (my $err = $@) { unlink $path; die $err; }
        my $rpcenv = PVE::RPCEnvironment::get();
        my $worker = sub {
            eval {
                my $upid = PVE::API2::LXC->create_vm(\%ct);
                PVE::Tools::run_command([
                    '/usr/local/lib/betterdocker/provision.py', '--provision', $path,
                    "$ct{vmid}", $ct{node}, $upid,
                ]);
            };
            my $err = $@;
            unlink $path;
            die $err if $err;
        };
        my $upid = eval { $rpcenv->fork_worker('betterdocker', $ct{vmid}, $rpcenv->get_user(), $worker); };
        if (my $err = $@) { unlink $path; die $err; }
        return $upid;
    },
});
1;
