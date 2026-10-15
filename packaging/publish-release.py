#!/usr/bin/env python3
"""Verify an existing draft against accepted CI bytes, then optionally publish it."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from dctlib.version import VERSION


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--release-id', required=True, type=int)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    repo, commit = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_SHA']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[0-9a-f]{40}', commit) or min(args.run_id, args.release_id) <= 0:
        raise ValueError('Invalid release identity')

    def api(path, method='GET', data=None):
        cmd = ['gh', 'api', 'repos/' + repo + ('/' + path if path else ''), '--method', method]
        if data is not None:
            cmd += ['--input', '-']
        return json.loads(command(cmd, input=json.dumps(data).encode() if data is not None else None))

    run = api('actions/runs/' + str(args.run_id))
    if (run['head_sha'], run['conclusion'], run['event'], run['head_branch'], run['path'], run['head_repository']['full_name']) != (
            commit, 'success', 'push', 'main', '.github/workflows/check.yml', repo):
        raise ValueError('Release requires successful Validate CI for this exact main commit')
    draft = api('releases/' + str(args.release_id))
    if not draft['draft'] or not draft['prerelease'] or draft['tag_name'] != 'v' + VERSION or draft['target_commitish'] != commit:
        raise ValueError('Expected a prerelease draft bound to this exact commit')
    names = {'install.sh', 'betterdockers-' + VERSION + '.tar.gz', 'proxmox-betterdockers_' + VERSION + '_all.deb',
             'RELEASE.json', 'RELEASE.sig', 'release-key.pem', 'SHA256SUMS'}
    if len(draft['assets']) != len(names) or {a['name'] for a in draft['assets']} != names:
        raise ValueError('Unexpected or missing draft assets')
    with tempfile.TemporaryDirectory(prefix='betterdocker-publish-') as folder:
        assets = Path(folder) / 'assets'; assets.mkdir()
        build = Path(folder) / 'build'
        command(['gh', 'run', 'download', str(args.run_id), '--repo', repo, '--name', 'betterdocker-deb', '--dir', str(build)])
        for asset in draft['assets']:
            if not 0 < asset['size'] <= 512 * 1024 * 1024:
                raise ValueError('Release asset exceeds its download bound')
            content = command(['gh', 'api', 'repos/' + repo + '/releases/assets/' + str(asset['id']), '-H', 'Accept: application/octet-stream'])
            if len(content) != asset['size']:
                raise ValueError('Incomplete release asset download')
            (assets / asset['name']).write_bytes(content)
        subprocess.run([sys.executable, str(ROOT / 'packaging/verify-release.py'), str(assets), '--build', str(build)], check=True)
    if not args.publish:
        print('Verified draft for ' + commit + '. No tag or release was published.')
        return
    if api('')['private']:
        raise ValueError('Public release publishing is disabled in private staging')
    environment = api('environments/release')
    if not any(rule['type'] == 'required_reviewers' and rule.get('reviewers') for rule in environment.get('protection_rules', [])):
        raise ValueError('Release environment must require reviewer approval')
    current = api('releases/' + str(args.release_id))
    stable = lambda value: (value['draft'], value['prerelease'], value['tag_name'], value['target_commitish'],
                            sorted((a['id'], a['name'], a['size'], a.get('digest')) for a in value['assets']))
    if stable(current) != stable(draft):
        raise ValueError('Draft changed after verification; review it again')
    tags = api('git/matching-refs/tags/v' + VERSION)
    exact = [tag for tag in tags if tag['ref'] == 'refs/tags/v' + VERSION]
    if exact:
        target = exact[0]['object']
        for _ in range(5):
            if target['type'] == 'commit': break
            if target['type'] != 'tag': raise ValueError('Unsupported release tag')
            target = api('git/tags/' + target['sha'])['object']
        if target['type'] != 'commit' or target['sha'] != commit:
            raise ValueError('Release tag does not identify the reviewed commit')
    else:
        api('git/refs', 'POST', {'ref': 'refs/tags/v' + VERSION, 'sha': commit})
    api('releases/' + str(args.release_id), 'PATCH', {'draft': False})
    print('Published existing verified assets for v' + VERSION + ' at ' + commit)


if __name__ == '__main__':
    main()
