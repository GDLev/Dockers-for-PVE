package PVE::API2::BetterDocker::Settings;

use strict;
use warnings;
use JSON qw(decode_json encode_json);
use File::Temp qw(tempfile);
use PVE::Exception qw(raise_perm_exc);

sub register_api {
    my ($helper, $node, $id, $digest, $root) = PVE::API2::BetterDocker::method_defaults();

    PVE::API2::BetterDocker::register_docker_method({
        name => 'image_state', path => '{id}/images', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read the prepared image update and its recovery snapshot.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'object' },
        code => sub { my ($p) = @_; my $state = decode_json(PVE::API2::BetterDocker::output(['image-state', $p->{id}, $p->{node}]));
            delete $state->{token} unless PVE::API2::BetterDocker::capabilities($p->{id}, $p->{node})->{images};
            return $state; },
    });
    PVE::API2::BetterDocker::register_docker_method({
        name => 'image_preview', path => '{id}/images/preview', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Pull selected images and prepare an immutable change preview without recreating containers.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
            images => { type => 'string', maxLength => 8192 } } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            PVE::API2::BetterDocker::check_digest($p);
            my $data = decode_json($p->{images});
            die "Images must be a JSON object\n" unless ref($data) eq 'HASH';
            my ($fh, $path) = tempfile('betterdocker-images-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path; print $fh encode_json($data); close $fh;
            my $rpc = PVE::RPCEnvironment::get();
            my $upid = eval { $rpc->fork_worker('betterdockerimagepreview', PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                eval { PVE::Tools::run_command([$helper, 'image-preview', $p->{id}, $p->{node}, $path, $p->{digest} // '']); };
                my $err = $@; unlink $path; die $err if $err;
            }); };
            if (my $err = $@) { unlink $path; die $err; }
            return $upid;
        },
    });
    PVE::API2::BetterDocker::register_docker_method({
        name => 'image_apply', path => '{id}/images', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Update stopped services to previewed images after creating a named recovery snapshot.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
            token => { type => 'string', pattern => '^[0-9a-f]{32}$' },
            recovery => { type => 'string', maxLength => 40 } } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            PVE::API2::BetterDocker::check_digest($p);
            raise_perm_exc('VM.Snapshot') unless PVE::API2::BetterDocker::capabilities($p->{id}, $p->{node})->{snapshot};
            my $rpc = PVE::RPCEnvironment::get();
            return $rpc->fork_worker('betterdockerimageupdate', PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'image-apply', $p->{id}, $p->{node}, $p->{token}, $p->{recovery}, $p->{digest} // '']);
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'project_limits', path => '{id}/limits', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read administrator-owned aggregate resource ceilings.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'object' },
        code => sub { my ($p) = @_; return decode_json(PVE::API2::BetterDocker::output(['project-limits', $p->{id}, $p->{node}])); },
    });
    PVE::API2::BetterDocker::register_docker_method({
        name => 'set_project_limits', path => '{id}/limits', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Set aggregate ceilings for a stopped DCT. Requires node Sys.Modify.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
            cpus => { type => 'number', minimum => 0, maximum => 8192 },
            memory => { type => 'integer', minimum => 0, maximum => 1048576 },
            pids => { type => 'integer', minimum => 0, maximum => 4194304 } } },
        returns => { type => 'object' },
        code => sub {
            my ($p) = @_;
            my $rpc = PVE::RPCEnvironment::get();
            $rpc->check($rpc->get_user(), '/nodes/' . $p->{node}, ['Sys.Modify']);
            my ($fh, $path) = tempfile('betterdocker-limits-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path;
            print $fh encode_json({ map { $_ => 0 + $p->{$_} } qw(cpus memory pids) }); close $fh;
            my $result = eval { decode_json(PVE::API2::BetterDocker::output(['project-limits', $p->{id}, $p->{node}, $path, $p->{digest} // ''])); };
            my $err = $@; unlink $path; die $err if $err;
            return $result;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'pending', path => '{id}/pending', method => 'GET', protected => 1, proxyto => 'node',
        description => 'Read current and pending settings for one Compose service.',
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, service => { type => 'string', maxLength => 128 },
        } },
        returns => { type => 'array', items => { type => 'object' } },
        code => sub { my ($p) = @_; return decode_json(PVE::API2::BetterDocker::output(['pending', $p->{id}, $p->{node}, $p->{service}])); },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'revert_settings', path => '{id}/pending', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Discard selected pending settings without changing the running containers.',
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, digest => $digest, service => { type => 'string', maxLength => 128 },
            revert => { type => 'string', maxLength => 256 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            PVE::API2::BetterDocker::check_digest($p);
            my $rpc = PVE::RPCEnvironment::get();
            return $rpc->fork_worker('betterdockersettings', PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'revert-settings', $p->{id}, $p->{node}, $p->{service}, $p->{revert}, $p->{digest} // '']);
            });
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'settings', path => '{id}/settings', method => 'PUT', protected => 1, proxyto => 'node',
        description => 'Apply persistent resource, DNS and restart settings to one service.', permissions => $root,
        parameters => { additionalProperties => 0, properties => {
            node => $node, id => $id, digest => $digest, service => { type => 'string', maxLength => 128 },
            settings => { type => 'string', maxLength => 8192 },
        } },
        returns => { type => 'string' },
        code => sub {
            my ($param) = @_;
            PVE::API2::BetterDocker::check_digest($param);
            my $settings = decode_json($param->{settings});
            die "Settings must be a JSON object\n" unless ref($settings) eq 'HASH';
            my $allowed = $ENV{BETTERDOCKER_CONFIG_KEYS} // '*';
            if ($allowed ne '*') {
                my %granted = map { $_ => 1 } split(',', $allowed);
                my $rows = decode_json(PVE::API2::BetterDocker::output(['pending', $param->{id}, $param->{node}, $param->{service}]));
                my %effective = map { $_->{key} => exists($_->{pending}) ? $_->{pending} : $_->{value} } @$rows;
                for my $key (keys %$settings) {
                    next if $granted{$key};
                    next if exists($effective{$key}) && encode_json($settings->{$key}) eq encode_json($effective{$key});
                    raise_perm_exc('configuration field requires additional privileges');
                }
            }
            my ($fh, $path) = tempfile('betterdocker-XXXXXX', DIR => '/run', UNLINK => 0);
            chmod 0600, $path;
            print $fh $param->{settings}; close $fh;
            my $rpcenv = PVE::RPCEnvironment::get();
            my $upid = eval { $rpcenv->fork_worker('betterdockersettings', PVE::API2::BetterDocker::task_id($param->{id}, $param->{node}), $rpcenv->get_user(), sub {
                eval { PVE::Tools::run_command([$helper, 'settings', $param->{id}, $param->{node}, $param->{service}, $path, $param->{digest} // '']); };
                my $err = $@; unlink $path; die $err if $err;
            }); };
            if (my $err = $@) { unlink $path; die $err; }
            return $upid;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'notes', path => '{id}/config', method => 'GET', protected => 1, proxyto => 'node', permissions => $root,
        description => 'Read project notes.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id } },
        returns => { type => 'object' },
        code => sub {
            my ($p) = @_;
            my $config = { %{decode_json(PVE::API2::BetterDocker::output(['notes', $p->{id}, $p->{node}]))},
                %{decode_json(PVE::API2::BetterDocker::output(['tags', $p->{id}, $p->{node}]))},
                %{decode_json(PVE::API2::BetterDocker::output(['protection', $p->{id}, $p->{node}]))},
                %{decode_json(PVE::API2::BetterDocker::output(['lifecycle', $p->{id}, $p->{node}]))} };
            delete $config->{digest} unless PVE::API2::BetterDocker::capabilities($p->{id}, $p->{node})->{settings};
            return $config;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
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
            PVE::API2::BetterDocker::check_digest($p);
            if (defined($p->{tags}) && !PVE::API2::BetterDocker::capabilities($p->{id}, $p->{node})->{configure}) {
                my $old = decode_json(PVE::API2::BetterDocker::output(['tags', $p->{id}, $p->{node}]))->{tags};
                PVE::GuestHelpers::assert_tag_permissions(PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $old, $p->{tags},
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
            eval { PVE::API2::BetterDocker::output(['config', $p->{id}, $p->{node}, $path, $p->{digest} // '']); };
            my $err = $@; unlink $path; die $err if $err;
            return undef;
        },
    });

    PVE::API2::BetterDocker::register_docker_method({
        name => 'reset_settings', path => '{id}/reset-settings', method => 'POST', protected => 1, proxyto => 'node',
        description => 'Restore Compose settings for one service while preserving stopped state.',
        parameters => { additionalProperties => 0, properties => { node => $node, id => $id, digest => $digest,
            service => { type => 'string', maxLength => 128 } } },
        returns => { type => 'string' },
        code => sub {
            my ($p) = @_;
            PVE::API2::BetterDocker::check_digest($p);
            my $rpc = PVE::RPCEnvironment::get();
            return $rpc->fork_worker('betterdockersettings', PVE::API2::BetterDocker::task_id($p->{id}, $p->{node}), $rpc->get_user(), sub {
                PVE::Tools::run_command([$helper, 'reset-settings', $p->{id}, $p->{node}, $p->{service}, $p->{digest} // '']);
            });
        },
    });
}

1;
