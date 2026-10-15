#!/usr/bin/env python3
"""Required, non-invasive release checks. Any skipped required test fails CI."""
import sys
import contextlib
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_security_noninvasive import run_suite

result = run_suite()
required = unittest.defaultTestLoader.loadTestsFromNames([
    'test_native_fixtures', 'test_compose_integration', 'test_release_auth', 'test_bootstrap_ownership',
    'test_installer', 'test_terminal_installer', 'test_acceptance_report', 'test_docker_install', 'test_release_publication',
])
from dctlib import operation_capacity, retention, space, filesystem_policy
with tempfile.TemporaryDirectory(prefix='bd-ci-controls-') as folder, contextlib.ExitStack() as controls:
    directory = Path(folder)
    for module, name in ((operation_capacity, 'slots'), (retention, 'retention'), (space, 'space')):
        controls.enter_context(patch.object(module, 'ROOT', directory / name))
        controls.enter_context(patch.object(module, 'POLICY', directory / (name + '-policy.json')))
    controls.enter_context(patch.object(filesystem_policy, 'POLICY', directory / 'filesystem-policy.json'))
    extra = unittest.TextTestRunner(verbosity=2).run(required)
if result.skipped or extra.skipped:
    print('Required tests were skipped:', result.skipped + extra.skipped, file=sys.stderr)
raise SystemExit(0 if result.wasSuccessful() and extra.wasSuccessful() and not result.skipped and not extra.skipped else 1)
