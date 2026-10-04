"""Load the Perl extension with schema callbacks, without requiring a PVE host."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PerlAPITests(unittest.TestCase):
    def test_load_and_register_without_mutating_native_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'PVE/API2').mkdir(parents=True)
            (base / 'PVE/RESTHandler.pm').write_text('''
package PVE::RESTHandler;
sub register_method { $main::registered = $_[1]; }
1;
''')
            (base / 'PVE/API2/LXC.pm').write_text('''
package PVE::API2::LXC;
our $schema = { additionalProperties => 0, properties => {
    node => { type => 'string', completion => sub { 'pve' } },
    vmid => { type => 'integer' }, ostemplate => { type => 'string' },
} };
sub map_method_by_name { return { parameters => $schema }; }
1;
''')
            for name in ['RPCEnvironment', 'Tools']:
                (base / 'PVE' / (name + '.pm')).write_text(f'package PVE::{name}; 1;')
            subprocess.run(['perl', '-I' + folder, '-I' + str(ROOT / 'backend'),
                            '-MPVE::API2::BetterDocker', '-e', '''
die 'not registered' unless $main::registered->{path} eq '';
die 'bad permission' if $main::registered->{permissions}->{user} || $main::registered->{permissions}->{check};
die 'missing docker schema' unless $main::registered->{parameters}->{properties}->{docker};
die 'native schema mutated' if $PVE::API2::LXC::schema->{properties}->{docker};
die 'callback lost' unless $main::registered->{parameters}->{properties}->{node}->{completion}->() eq 'pve';
'''], check=True)
