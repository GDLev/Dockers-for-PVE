#!/usr/bin/env python3
"""Run unit fixtures with disposable node-control state.

Mock admission dependencies outside their dedicated policy suites. Real Engine,
network traffic and node lifecycle verification belong to the live tests.
"""
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from dctlib import operation_capacity, retention, space, filesystem_policy, projects, network_security


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def run_suite():
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TestResult()
    with tempfile.TemporaryDirectory(prefix='bd-unit-controls-') as folder, contextlib.ExitStack() as controls:
        directory = Path(folder)
        for module, name in ((operation_capacity, 'slots'), (retention, 'retention'), (space, 'space')):
            controls.enter_context(patch.object(module, 'ROOT', directory / name))
            controls.enter_context(patch.object(module, 'POLICY', directory / (name + '-policy.json')))
        controls.enter_context(patch.object(filesystem_policy, 'POLICY', directory / 'filesystem-policy.json'))
        for case in cases(suite):
            module = case.id().split('.')[0]
            with contextlib.ExitStack() as dependencies:
                if module != 'test_network_isolation':
                    dependencies.enter_context(patch.object(projects, 'firewall_ready'))
                    dependencies.enter_context(patch.object(network_security, 'block_address'))
                if module != 'test_retention':
                    dependencies.enter_context(patch.object(retention, 'preservation'))
                if module not in ('test_peak_space', 'test_abandoned_operations'):
                    for name in ('claim', 'source_targets', 'replacement', 'snapshot', 'restore', 'data_disk', 'storage'):
                        dependencies.enter_context(patch.object(space, name, return_value=[]))
                outcome = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite([case]))
                result.testsRun += outcome.testsRun
                result.failures.extend(outcome.failures); result.errors.extend(outcome.errors)
                result.skipped.extend(outcome.skipped)
    for case, reason in result.skipped:
        print('Unit skip:', case.id(), reason)
    print('Unit total:', result.testsRun, 'tests;', len(result.failures), 'failures;', len(result.errors), 'errors;', len(result.skipped), 'skipped')
    return result


if __name__ == '__main__':
    outcome = run_suite()
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
