"""Availability models use ordinary fixtures; no host services or quorum are changed."""
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch
from test_worker import worker
from dctlib import recovery
import transfer


class FailureModels(unittest.TestCase):
    def test_lost_ssh_is_not_a_successful_remote_capacity_claim(self):
        with patch.object(transfer,'ssh_command',return_value=['fixture-ssh']), patch.object(worker,'run',side_effect=subprocess.CalledProcessError(255,['fixture-ssh'],stderr='Fixture connection closed')):
            with self.assertRaisesRegex(ValueError,'Fixture connection closed'):
                transfer.remote('fixture-node','prepare','a'*32)

    def test_daemon_loss_does_not_confirm_absent_resources(self):
        with patch.object(worker.node_runtime,'service',return_value=subprocess.CompletedProcess([],3)), patch.object(worker,'run') as command:
            with self.assertRaises(recovery.DockerUnavailable):recovery.require_docker()
            command.assert_not_called()

    def test_pbs_failure_does_not_publish_size_or_query_a_finished_archive(self):
        source=(Path(__file__).resolve().parents[1]/'backend/PVE/VZDump/Docker.pm').read_text()
        body=source.split('sub archive {',1)[1].split('\nsub cleanup',1)[0]
        program = """use strict; use warnings;
our ($backup,$files)=(0,0);
{ package PVE::Storage::PBSPlugin;
 sub run_raw_client_cmd { $main::backup++; die "Fixture PBS unavailable\n"; }
 sub run_client_cmd { $main::files++; die "Unreachable fixture publication"; }
}
{ package Fixture;
 sub archive {""" + body + """}
my $plugin=bless {vzdump=>{opts=>{pbs=>1,scfg=>{},storage=>'fixture'}}},'Fixture';
my $task={dct_state=>'/ordinary-fixture',backup_time=>1,target=>'fixture',size=>123};
eval { $plugin->archive($task,100,'fixture',''); };
die 'PBS error disappeared' unless $@ =~ /Fixture PBS unavailable/;
die 'Archive was incorrectly published' unless $backup==1 && $files==0 && $task->{size}==123;
"""
        subprocess.run(['perl','-e',program],check=True,capture_output=True,text=True)

    def test_quorum_failure_precedes_remote_contact_and_worker_creation(self):
        source=(Path(__file__).resolve().parents[1]/'backend/PVE/API2/BetterDocker/Transfer.pm').read_text()
        body=source.split("name => 'migrate'",1)[1].split('code => sub {',1)[1].split('\n        },',1)[0]
        program="""use strict; use warnings;
our ($remote,$fork)=(0,0);
{ package PVE::API2::BetterDocker; sub check_digest {} sub task_id {100} }
{ package PVE::RPCEnvironment; sub get {bless {},'FixtureRPC'} }
{ package FixtureRPC; sub get_user {'fixture@pve'} sub check {} sub fork_worker {$main::fork++;die 'Unreachable fixture worker'} }
{ package PVE::Cluster; sub check_cfs_quorum {die "Fixture quorum unavailable\n"} sub remote_node_ip {$main::remote++;die 'Unreachable fixture contact'} }
sub action {"""+body+"""}
eval { action({node=>'fixture1',target=>'fixture2',id=>'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}); };
die 'Quorum error disappeared' unless $@ =~ /Fixture quorum unavailable/;
die 'Side effect preceded quorum' unless $remote==0 && $fork==0;
"""
        subprocess.run(['perl','-e',program],check=True,capture_output=True,text=True)
