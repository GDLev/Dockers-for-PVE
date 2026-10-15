"""Uninstall must reject DCT HA sections without relying on plugin registration."""
import ast
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class InstallerTests(unittest.TestCase):
    def test_installed_runtime_has_all_local_python_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'start.sh').read_text()
        installed = set(re.findall(r'install -m [0-7]+ "\$PROJECT_DIR/scripts/([\w-]+\.py)"', source))
        available = {path.stem for path in (root / 'scripts').glob('*.py')}
        modules = [(root / 'scripts' / name) for name in installed]
        modules += list((root / 'scripts/dctlib').glob('*.py'))
        for path in modules:
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                        and isinstance(node.left, ast.Attribute) and node.left.attr == 'HELPERS'
                        and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
                        and node.right.value.endswith('.py')):
                    self.assertIn(node.right.value, installed, str(path.relative_to(root)) + ' executes missing runtime helper ' + node.right.value)
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name.split('.')[0] for item in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    names = [node.module.split('.')[0]]
                for name in set(names) & available:
                    self.assertIn(name + '.py', installed, str(path.relative_to(root)) + ' imports missing runtime module ' + name)
        for name in installed:
            self.assertIn('/usr/local/lib/betterdocker/' + name, source.split('for index in "${!TARGETS[@]}"', 1)[0], name + ' missing from rollback inventory')

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

    def test_repair_rejects_missing_ipv6_duplicate_address_probe(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'start.sh').read_text()
        body = source.split('check_repair_dependencies() {', 1)[1].split('\n}', 1)[0]
        mock = 'command() { [[ "$2" != ndisc6 ]]; }; python3() { return 0; };'
        result = subprocess.run(['bash', '-c', mock + '\ncheck() {' + body + '\n}; check'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing ndisc6', result.stderr)
        depends = (root / 'packaging/build-deb.sh').read_text().split('Depends: ', 1)[1].split('\n', 1)[0]
        self.assertIn('ndisc6', [part.strip() for part in depends.split(',')])
