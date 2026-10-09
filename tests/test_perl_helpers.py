"""Authenticated PVE requests execute helper arguments under Perl taint mode."""
import re
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PerlHelperTests(unittest.TestCase):
    def test_ha_and_replication_helper_arguments_are_validated_and_untainted(self):
        for filename, call in (
            ('backend/PVE/HA/Resources/Docker.pm', "helper({node=>$ARGV[1]},'enroll',$ARGV[0],$ARGV[1]);"),
            ('backend/PVE/BetterDocker/Replication.pm', "helper($ARGV[1],'prepare',$ARGV[0],$ARGV[1],'--lock-fd',3);"),
        ):
            source = (ROOT / filename).read_text()
            helper = re.search(r'^sub helper \{.*?^\}', source, re.M | re.S).group()
            program = r'''
use strict; use warnings; use Scalar::Util;
package PVE::INotify; sub nodename { 'pve' }
package PVE::Tools;
sub run_command {
 die 'tainted helper command' if grep { Scalar::Util::tainted($_) } @{$_[0]};
 $main::executed++;
}
package main;
our $executed=0; my $helper='/usr/local/lib/betterdocker/replica.py';
''' + helper + call + "die 'helper not executed' unless $executed;"
            with self.subTest(filename=filename):
                subprocess.run(['perl', '-T', '-e', program, 'a' * 32, 'pve'], check=True)
                failed = subprocess.run(['perl', '-T', '-e', program, 'unsafe; argument', 'pve'], capture_output=True, text=True)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('Invalid DCT helper argument', failed.stderr)
