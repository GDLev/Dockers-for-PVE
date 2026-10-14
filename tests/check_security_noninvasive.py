#!/usr/bin/env python3
"""Run selected local policy contracts, using mocks and temporary files only.

Does not connect to PVE/Docker, execute payloads, race paths, exhaust resources,
or change services, profiles or guests. Live acceptance remains separate.
"""
import unittest
import contextlib
import tempfile
from pathlib import Path
from unittest.mock import patch


CASES = (
    'test_security_policy.HardenedPolicy',
    'test_security_policy.ControlFiles.test_environment_read_group_is_only_the_selected_runtime',
    'test_private_images.PrivateImages',
    'test_security_contracts.SecurityContracts',
    'test_apparmor_start.AppArmorStart',
    'test_recovery_availability.RecoveryAvailability',
    'test_archive_readers.ArchiveReaders',
    'test_hardened_mutations.HardenedMutations',
    'test_policy_conversion.PolicyConversion',
    'test_resource_defaults.ResourceDefaults',
    'test_resource_enforcement.ResourceEnforcement',
    'test_operation_capacity.OperationCapacity',
    'test_retention.Retention',
    'test_log_oom.LogOomTests.test_defaults_are_bounded_idempotent_and_preserve_explicit_driver_options',
    'test_log_oom.LogOomTests.test_delegated_limits_reject_disabled_rotation_and_excessive_values',
    'test_log_oom.LogOomTests.test_oom_protection_checks_config_and_saved_containers',
    'test_peak_space.PeakSpace',
    'test_filesystem_policy.FilesystemPolicy',
    'test_abandoned_operations.AbandonedOperations',
    'test_network_isolation.NetworkIsolation',
    'test_network_isolation.FirewallReload',
    'test_security_failure_models.FailureModels',
    'test_shutdown_cancel.ShutdownCancellationTests',
)


def run_suite(verbosity=2):
    # Admission decorators use private fixture locks, never node reservations.
    import sys
    import importlib.util
    if importlib.util.find_spec('dctlib') is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from dctlib import operation_capacity as capacity
    from dctlib import retention
    from dctlib import space, filesystem_policy
    with tempfile.TemporaryDirectory() as folder, patch.object(capacity, 'ROOT', Path(folder) / 'slots'), patch.object(capacity, 'POLICY', Path(folder) / 'policy.json'), patch.object(retention, 'ROOT', Path(folder) / 'retention'), patch.object(retention, 'POLICY', Path(folder) / 'retention-policy.json'), patch.object(space, 'ROOT', Path(folder) / 'space'), patch.object(space, 'POLICY', Path(folder) / 'space-policy.json'):
        policy_fixture = patch.object(filesystem_policy, 'POLICY', Path(folder) / 'filesystem-policy.json')
        policy_fixture.start()
        try:
            return _run_cases(verbosity, retention, space)
        finally:
            policy_fixture.stop()


def _run_cases(verbosity, retention, space):
    result = unittest.TestResult()
    runner = unittest.TextTestRunner(verbosity=verbosity)
    for case in CASES:
        # Mutation fixtures isolate Engine behavior from retention checks.
        # Retention has its own image-accounting contracts below.
        with contextlib.ExitStack() as fixtures:
            if not case.startswith('test_network_isolation.'):
                from dctlib import projects
                fixtures.enter_context(patch.object(projects, 'firewall_ready'))
                from dctlib import network_security
                fixtures.enter_context(patch.object(network_security, 'block_address'))
            if not case.startswith('test_retention.'):
                fixtures.enter_context(patch.object(retention, 'preservation'))
            if not case.startswith(('test_peak_space.', 'test_abandoned_operations.')):
                for name in ('claim', 'source_targets', 'replacement', 'snapshot', 'restore', 'data_disk', 'storage'):
                    fixtures.enter_context(patch.object(space, name, return_value=[]))
            suite = unittest.defaultTestLoader.loadTestsFromNames([case])
            case_result = runner.run(suite)
        result.testsRun += case_result.testsRun
        result.failures.extend(case_result.failures); result.errors.extend(case_result.errors)
        result.skipped.extend(case_result.skipped)
    print('Non-invasive total:', result.testsRun, 'tests;', len(result.skipped), 'skipped')
    return result


if __name__ == '__main__':
    result = run_suite()
    raise SystemExit(0 if result.wasSuccessful() else 1)
