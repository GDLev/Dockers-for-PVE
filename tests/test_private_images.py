"""Private filesystem images need guest permission or actual archive provenance."""
import json
import subprocess
import unittest
from unittest.mock import patch
from test_worker import worker as w
from dctlib import image_access

ID = 'a' * 32
FOREIGN = 'b' * 32
IMAGE = 'sha256:' + 'c' * 64


class PrivateImages(unittest.TestCase):
    def result(self, owner=FOREIGN):
        return subprocess.CompletedProcess([], 0, json.dumps([{'Id': IMAGE, 'Config': {'Labels': {'betterdocker.id': owner}}}]), '')

    def test_known_foreign_image_is_not_a_clone_permission(self):
        with patch.object(w, 'trusted_management', return_value=False), patch.object(w, 'run', return_value=self.result()), patch.object(w, 'container_records', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'Private images'):
                image_access.reference(IMAGE, ID)

    def test_delegated_config_checks_owner_for_every_image_reference(self):
        names = [IMAGE, 'c' * 12, 'ordinary-name:tag']
        for kind in ('snapshot', 'export', 'settings'):
            tag = 'betterdocker-' + kind + '/' + FOREIGN + ':fixture'
            names.extend((tag, 'docker.io/' + tag, 'index.docker.io/' + tag))
        for name in names:
            with self.subTest(image=name), patch.object(w, 'trusted_management', return_value=False), patch.object(w, 'run', return_value=self.result()) as inspect, patch.object(w, 'container_records', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'Private images'):
                    w.validate_delegated_config({'services': {'app': {'image': name, 'pull_policy': 'never'}}}, ID)
                inspect.assert_called_once()

    def test_public_or_missing_images_remain_available_and_errors_fail_closed(self):
        with patch.object(w, 'trusted_management', return_value=False), patch.object(w, 'run', return_value=self.result(None)):
            image_access.reference('docker.io/library/busybox:1.37', ID)
        missing = subprocess.CompletedProcess([], 1, '', 'Error: No such image: busybox:1.37')
        with patch.object(w, 'trusted_management', return_value=False), patch.object(w, 'run', return_value=missing):
            image_access.reference('busybox:1.37', ID)
        unavailable = subprocess.CompletedProcess([], 1, '', 'Cannot connect to Docker daemon')
        with patch.object(w, 'trusted_management', return_value=False), patch.object(w, 'run', return_value=unavailable), self.assertRaisesRegex(ValueError, 'Cannot verify'):
            image_access.reference('busybox:1.37', ID)

    def test_own_image_and_an_existing_authorized_clone_remain_available(self):
        with patch.object(w, 'trusted_management', return_value=False), patch.object(w, 'run', return_value=self.result(ID)):
            image_access.reference(IMAGE, ID)
        rows = [{'Image': IMAGE, 'Config': {'Labels': {'betterdocker.id': ID}}}]
        with patch.object(w, 'trusted_management', return_value=False), patch.object(w, 'run', return_value=self.result()), patch.object(w, 'container_records', return_value=[{'id': 'own'}]), patch.object(w, 'capture', return_value=json.dumps(rows)):
            image_access.reference(IMAGE, ID)

    def test_archive_cannot_reference_an_existing_image_that_it_did_not_load(self):
        containers = [{'SnapshotTag': 'betterdocker-export/fixture:tag', 'SnapshotImage': IMAGE}]
        with self.assertRaisesRegex(ValueError, 'not provided'):
            image_access.loaded(subprocess.CompletedProcess([], 0, 'Loaded image: busybox:latest\n', ''), containers)
        result = subprocess.CompletedProcess([], 0, 'Loaded image: betterdocker-export/fixture:tag\n', '')
        with patch.object(w, 'capture', return_value='sha256:' + 'd' * 64), self.assertRaisesRegex(ValueError, 'identity'):
            image_access.loaded(result, containers)
        with patch.object(w, 'capture', return_value=IMAGE):
            image_access.loaded(result, containers)
