"""Exercise the real Compose parser only. No daemon access or container creation."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_worker import worker, BASE


def available():
    return bool(shutil.which('docker')) and subprocess.run(['docker', 'compose', 'version'], capture_output=True).returncode == 0


@unittest.skipUnless(available(), 'Docker Compose CLI is not installed')
class ComposeIntegrationTests(unittest.TestCase):
    def test_relative_bind_mounts_and_env_stay_in_permanent_directory_on_redeploy(self):
        yaml = 'services:\n  web:\n    image: nginx:stable\n    ports: ["8080:80"]\n    volumes: ["./data:/data"]\n    env_file: .env\n'
        # The local developer user may store CLI plugins under their own HOME.
        # Production subprocesses run as root; this parser-only test uses the test user's CLI.
        def run(args, **kwargs):
            return subprocess.run(args, check=True, text=True, **kwargs)
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'run', side_effect=run):
            directory, staging = Path(folder) / 'project', Path(folder) / 'staging'
            data = dict(BASE, compose=yaml, environment='VALUE=old')
            config = worker.prepare(data, 'a' * 32, directory)
            self.assertEqual(config['services']['web']['volumes'][0]['source'], str(directory / 'data'))
            config = worker.prepare(dict(data, environment='VALUE=new'), 'a' * 32, staging, project_directory=directory)
            self.assertEqual(config['services']['web']['volumes'][0]['source'], str(directory / 'data'))
            self.assertEqual(config['services']['web']['env_file'][0]['path'], str(directory / '.env'))
            for filename in ('compose.source.yaml', '.env', 'compose.runtime.json'):
                (staging / filename).replace(directory / filename)
            result = subprocess.run(worker.compose_base(directory, 'a' * 32) + ['config', '--format', 'json'],
                                    check=True, text=True, capture_output=True)
            resolved = json.loads(result.stdout)['services']['web']
            self.assertEqual(resolved['environment']['VALUE'], 'new')
            self.assertEqual(resolved['ports'][0]['host_ip'], BASE['address'])

    def test_service_overrides_survive_redeployment_and_parse_with_compose(self):
        def run(args, **kwargs):
            return subprocess.run(args, check=True, text=True, **kwargs)
        with tempfile.TemporaryDirectory() as folder, patch.object(worker, 'run', side_effect=run):
            directory = Path(folder)
            worker.write_json(directory / 'settings.json', {'web': {
                'cpus': 1.5, 'memory': 512, 'swap': 256, 'cpuunits': 1024, 'pids': 200,
                'dns': ['1.1.1.1'], 'dns_search': ['example.test'], 'restart': 'always'}})
            source = 'services:\n  web:\n    image: nginx:stable\n    deploy:\n      resources:\n        limits:\n          cpus: "4"\n          memory: 2G\n'
            worker.prepare(dict(BASE, compose=source), 'a' * 32, directory)
            result = subprocess.run(worker.compose_base(directory, 'a' * 32) + ['config', '--format', 'json'],
                                    check=True, text=True, capture_output=True)
            config = json.loads(result.stdout)['services']['web']
            self.assertEqual(float(config['cpus']), 1.5)
            self.assertEqual(int(config['mem_limit']), 512 * 1048576)
            self.assertEqual(int(config['memswap_limit']), 768 * 1048576)
            self.assertEqual(config['restart'], 'always')
            self.assertEqual(config['dns'], ['1.1.1.1'])
