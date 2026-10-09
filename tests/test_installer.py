"""Uninstall must reject DCT HA sections without relying on plugin registration."""
from pathlib import Path
import subprocess
import tempfile
import unittest


class InstallerTests(unittest.TestCase):
    def test_ha_guard_reads_unknown_resource_types_before_any_mutation(self):
        source=(Path(__file__).resolve().parents[1]/'start.sh').read_text()
        code=source.split("        perl -e '\n",1)[1].split("\n        '\n",1)[0]
        self.assertLess(source.index(code),source.index('systemctl disable --now betterdocker-integration.timer'))
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'resources.cfg'
            for text,blocked in [('docker: 115\n\tstate started\n',True),
                                 ('ct: 103\n\tstate started\ndocker:115\n\tstate ignored\n',True),
                                 ('vm: 100\n\tstate started\n',False),
                                 ('# docker: 115\n',False)]:
                with self.subTest(text=text):
                    config.write_text(text)
                    result=subprocess.run(['perl','-e',code.replace('/etc/pve/ha/resources.cfg',str(config))],capture_output=True,text=True)
                    self.assertEqual(bool(result.returncode),blocked,result.stderr)
                    if blocked:self.assertIn('Remove DCT HA resources',result.stderr)
