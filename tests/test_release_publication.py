"""Publishing must reject unrelated builds, mutable drafts and missing approval gates."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_publication', ROOT / 'packaging/publish-release.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class ReleasePublication(unittest.TestCase):
    def attempt(self, *, sha='a' * 40, private=False, protected=True, changed=False):
        calls = []
        names = ['install.sh', 'betterdockers-0.20.0.tar.gz', 'proxmox-betterdockers_0.20.0_all.deb',
                 'RELEASE.json', 'RELEASE.sig', 'release-key.pem', 'SHA256SUMS']
        draft = dict(draft=True, prerelease=True, tag_name='v0.20.0', target_commitish='a' * 40,
                     assets=[dict(id=i + 1, name=n, size=1) for i, n in enumerate(names)])
        reads = 0

        def command(args, **kwargs):
            nonlocal reads
            calls.append(args)
            if args[:3] == ['gh', 'run', 'download']: return b''
            path = args[2].removeprefix('repos/GDLev/Proxmox-BetterDockers').lstrip('/')
            if path == 'actions/runs/1':
                value = dict(head_sha=sha, conclusion='success', event='push', head_branch='main',
                             path='.github/workflows/check.yml', head_repository={'full_name':'GDLev/Proxmox-BetterDockers'})
            elif path == 'releases/2':
                reads += 1
                value = dict(draft, target_commitish='b' * 40) if changed and reads > 1 else draft
            elif path.startswith('releases/assets/'): return b'x'
            elif path == '': value = {'private':private}
            elif path == 'environments/release':
                value = {'protection_rules':[{'type':'required_reviewers','reviewers':[{'type':'User'}]}] if protected else []}
            else: raise AssertionError('Unexpected API operation: ' + path)
            return json.dumps(value).encode()

        with patch.dict(os.environ, GITHUB_REPOSITORY='GDLev/Proxmox-BetterDockers', GITHUB_SHA='a' * 40), \
             patch.object(publication.sys, 'argv', ['publish-release.py','--run-id','1','--release-id','2','--publish']), \
             patch.object(publication, 'command', side_effect=command), patch.object(publication.subprocess, 'run'):
            try:
                publication.main()
            finally:
                self.assertFalse(any('POST' in call or 'PATCH' in call for call in calls), 'Rejected release performed a write')

    def test_other_commit_cannot_supply_release_artifacts(self):
        with self.assertRaisesRegex(ValueError, 'exact main commit'): self.attempt(sha='b' * 40)

    def test_private_staging_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, 'private staging'): self.attempt(private=True)

    def test_missing_reviewers_refuses_publication(self):
        with self.assertRaisesRegex(ValueError, 'reviewer approval'): self.attempt(protected=False)

    def test_draft_changed_after_verification_refuses_publication(self):
        with self.assertRaisesRegex(ValueError, 'Draft changed'): self.attempt(changed=True)
