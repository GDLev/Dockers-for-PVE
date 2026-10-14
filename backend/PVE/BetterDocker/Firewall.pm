package PVE::BetterDocker::Firewall;
use strict;
use warnings;
use Storable qw(dclone);
use JSON qw(decode_json);
use Fcntl qw(LOCK_EX LOCK_NB);
use Time::HiRes qw(time sleep);
use PVE::API2::Firewall::IPSet;
use PVE::API2::Firewall::VM;
use PVE::Tools;
use PVE::Cluster;

sub run_helper {
    my ($command) = @_;
    my $error = '';
    eval { PVE::Tools::run_command($command, errfunc => sub { $error .= $_[0] . "\n"; }); };
    die($error || $@) if $@;
}

sub register_api {
    my ($class) = @_;
    my ($tree, $index) = ([], {});
    PVE::RESTHandler::api_dump_full($tree, $index, 'PVE::API2::Firewall::CT', '/{id}/firewall', 1);
    my $visit;
    $visit = sub {
        my ($items) = @_;
        for my $item (@$items) {
            my $path = $item->{path};
            $path =~ s!^/!!;
            (my $lookup = "/$path") =~ s/\{[^}]+\}/\{\}/g;
            for my $verb (qw(GET POST PUT DELETE)) {
                my $native = $index->{"$verb $lookup"} or next;
                my $method = { %$native, path => $path, name => 'firewall_' . $native->{name},
                    parameters => dclone($native->{parameters}), permissions => { user => 'all' },
                    protected => 1, proxyto => 'node' };
                delete $method->{parameters}->{properties}->{vmid};
                if ($native->{name} eq 'set_options') {
                    delete @{$method->{parameters}->{properties}}{qw(dhcp ndp radv ips ips_queues)};
                }
                if ($native->{name} eq 'get_options') {
                    $method->{returns} = dclone($native->{returns});
                    $method->{returns}->{properties}->{status} = { type => 'string', optional => 1 };
                }
                $method->{parameters}->{properties}->{id} = { type => 'string', pattern => '^[0-9a-f]{32}$' };
                $method->{code} = sub {
                    my ($param) = @_;
                    my $rpc = PVE::RPCEnvironment::get();
                    my $vmid = PVE::API2::BetterDocker::task_id($param->{id}, $param->{node});
                    my $values = { %$param, vmid => $vmid };
                    delete $values->{id};
                    $rpc->check_api2_permissions($native->{permissions}, $rpc->get_user(), $values);
                    if ($verb eq 'GET') {
                        my $result = $native->{code}->($values);
                        if ($native->{name} eq 'get_options') {
                            my $status = eval { decode_json(PVE::Tools::file_get_contents('/var/lib/betterdocker/firewall/status.json')) };
                            $result->{status} = !$status || time() - $status->{updated} > 15 ? 'unknown' :
                                $status->{error} ? 'error' : (grep { $_ == $vmid } @{$status->{active} // []}) ? 'active' : 'inactive';
                        }
                        return $result;
                    }
                    my $result;
                    open(my $guard, '>>', '/run/lock/betterdocker-projects.lock') or die $!;
                    my $deadline = time() + 10;
                    while (!flock($guard, LOCK_EX | LOCK_NB)) {
                        die "A DCT operation is in progress\n" if time() >= $deadline;
                        sleep(0.1);
                    }
                    PVE::API2::BetterDocker::task_id($param->{id}, $param->{node});
                    PVE::Cluster::cfs_lock_firewall('dct-api-' . $vmid, 10, sub {
                        my $file = "/etc/pve/firewall/$vmid.fw";
                        my $before = -f $file ? PVE::Tools::file_get_contents($file) : undef;
                        eval {
                            run_helper(['/usr/local/lib/betterdocker/provision.py', 'firewall-check', $param->{id}, $param->{node}]);
                            $result = $native->{code}->($values);
                            run_helper(['/usr/local/lib/betterdocker/firewall.py', 'apply']);
                        };
                        if (my $error = $@) {
                            if (defined($before)) { PVE::Tools::file_set_contents($file, $before); }
                            else { unlink $file; }
                            eval { run_helper(['/usr/local/lib/betterdocker/firewall.py', 'apply']); };
                            $error .= "Firewall recovery failed: $@" if $@;
                            die $error;
                        }
                    });
                    die $@ if $@;
                    return $result;
                };
                delete @{$method}{qw(match_re match_name)};
                $class->register_method($method);
            }
            $visit->($item->{children}) if $item->{children};
        }
    };
    $visit->($tree);
}

1;
