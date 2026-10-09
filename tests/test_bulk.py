"""Exercise the PVE bulk adapter without a PVE installation."""
from pathlib import Path
import subprocess
import unittest

class BulkTests(unittest.TestCase):
    def test_guest_selection_and_native_callback_adapter(self):
        source=(Path(__file__).resolve().parents[1]/'backend/PVE/API2/BetterDocker.pm').read_text()
        functions=source[source.index('sub append_migration_list {'):source.index('sub lifecycle_identifier {')]
        lifecycle=source[source.index('sub append_lifecycle_list {'):source.index('sub append_migration_list {')]
        stop=source[source.index('sub node_stop {'):source.index('# Called after Cluster.pm')]
        adapter=source[source.index('package PVE::BetterDocker::BulkClient;'):source.rfind('\n1;')]
        code=r'''
use strict; use warnings;
package PVE::Tools; sub split_list { split /,/, $_[0] }
package PVE::JSONSchema; sub pve_parse_startup_order { {order=>2} }
package PVE::HA::Config; sub get_service_status { {managed=>0} }
package PVE::API2::Cluster::BulkAction::Guest;
our %methods = map { $_=>{code=>sub { $_[0]->() }} } qw(start shutdown migrate suspend);
sub map_method_by_name { $methods{$_[1]} }
sub get_type_text { 'native' }
sub handle_task_foreach_guest { my($list,$max,$start,$check)=@_;$start->(bless({},'Client'),123,{node=>'pve2',type=>'docker'}); }
package Client;
our @calls;
sub get { push @calls,['get',$_[1]]; return {status=>'partial'}; }
sub post { push @calls,['post',$_[1],$_[2]]; return 'UPID:task'; }
package PVE::API2::BetterDocker;
my %native_operation_guards;
sub LONG_MAX { 9999 }
sub worker_id { $_[0] }
sub summaries { [{id=>'a'x32,vmid=>123,node=>'pve2',status=>'running',onboot=>1}] }
sub capabilities { {power=>1} }
sub ha_managed { 1 }
sub map_method_by_name { {code=>sub { die 'incorrect HA action' unless $_[0]->{action} eq 'shutdown'; return 'HA task' }} }
sub lifecycle_identifier { die 'wrong VMID' unless $_[0]==123; 'a'x32 }
'''+functions+lifecycle+stop+adapter+r'''
die 'node shutdown bypasses HA' unless node_stop(123,'pve2',30,1) eq 'HA task';
my $list={};append_migration_list($list,undef,'123');die 'missing guest' unless $list->{123}->{node} eq 'pve2';
$list={};append_migration_list($list,'other','123');die 'wrong node included' if keys %$list;
$list={};append_lifecycle_list($list,undef,undef,'123');die 'cluster lifecycle missing' unless $list->{9999}->{123}->{node} eq 'pve2';
hook_bulk_operations();
$PVE::API2::Cluster::BulkAction::Guest::methods{shutdown}->{code}->(sub {
 PVE::API2::Cluster::BulkAction::Guest::handle_task_foreach_guest({},1,sub {
  my($client,$vmid,$guest)=@_;die 'changed display ID' unless $vmid==123;
  my $status=$client->get('/nodes/pve2/docker/123/status/current');die 'partial runtime treated stopped' unless $status->{status} eq 'running' && $status->{qmpstatus} eq 'running';
  $client->post('/nodes/pve2/docker/123/status/shutdown',{timeout=>8,forceStop=>1});
 },sub{});
});
my $call=$Client::calls[-1];die 'wrong route' unless $call->[1] eq '/nodes/pve2/docker/'.('a'x32).'/shutdown';
die 'lost force or timeout' unless $call->[2]->{timeout}==8 && $call->[2]->{'force-stop'}==1;
die 'type' unless PVE::API2::Cluster::BulkAction::Guest::get_type_text('docker') eq 'DCT';
'''
        subprocess.run(['perl','-e',code],check=True,capture_output=True,text=True)

    def test_backup_pool_inventory_is_scoped_to_vzdump(self):
        source = (Path(__file__).resolve().parents[1] / 'backend/PVE/BetterDocker/Backup.pm').read_text()
        hook = source[source.index('    my $native_pool_members ='):source.index('    for my $name (qw(dryrun delete))')]
        code = r"""
use strict; use warnings;
package PVE::Cluster; sub get_vmlist { {ids=>{100=>{}}} }
package PVE::API2Tools;
sub get_resource_pool_guest_members { my $v=PVE::Cluster::get_vmlist();return [grep {exists $v->{ids}->{$_}} (100,101,999)] }
package PVE::BetterDocker::Backup;
sub inventory { my $v=shift;return {%$v,ids=>{%{$v->{ids}},101=>{type=>'docker'}}} }
no warnings 'redefine';
""" + hook + r"""
package main;
die 'unrelated consumer changed' unless join(',',@{PVE::API2Tools::get_resource_pool_guest_members()}) eq '100';
package PVE::VZDump;
die 'pool DCT missing' unless join(',',@{PVE::API2Tools::get_resource_pool_guest_members()}) eq '100,101';
package main;
die 'inventory leaked' if exists PVE::Cluster::get_vmlist()->{ids}->{101};
"""
        subprocess.run(['perl', '-e', code], check=True, capture_output=True, text=True)
