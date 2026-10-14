#!/usr/bin/env python3
"""Publish and download-verify a disposable TEST release on git.example.org."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release


def curl(url, token=None, method='GET', body=None, upload=None, output=None):
    args = ['curl', '--silent', '--show-error', '--proto', '=https', '--connect-timeout', '15', '--max-time', '180', '--write-out', '%{http_code}', '--request', method]
    config = '' if token is None else 'header = ' + json.dumps('JOB-TOKEN: ' + token) + '\n'
    args += ['--config', '-']
    if token is None: args += ['--location', '--proto-redir', '=https']
    with tempfile.TemporaryDirectory(prefix='bd-test-http-') as folder:
        target = Path(folder) / 'response' if output is None else output
        args += ['--output', str(target)]
        if body is not None:
            data = Path(folder) / 'body'; data.write_text(json.dumps(body))
            args += ['--header', 'Content-Type: application/json', '--data-binary', '@' + str(data)]
        if upload is not None: args += ['--upload-file', str(upload)]
        args.append(url)
        result = subprocess.run(args, input=config, text=True, capture_output=True, check=True)
        status = int(result.stdout)
        if output is not None: return status, None
        text = target.read_text()
        try: return status, json.loads(text)
        except json.JSONDecodeError: return status, text[:200]


def main():
    tag = os.environ.get('CI_COMMIT_TAG', '')
    api = os.environ.get('CI_API_V4_URL', '')
    project = os.environ.get('CI_PROJECT_ID', '')
    commit = os.environ.get('CI_COMMIT_SHA', '')
    token = os.environ.get('CI_JOB_TOKEN', '')
    if os.environ.get('CI_SERVER_HOST') != 'git.example.org' or api != 'https://git.example.org/api/v4' or project != '2':
        raise SystemExit('Test publication is restricted to git.example.org project 2')
    if not re.fullmatch(r'test-acceptance-[A-Za-z0-9.-]{1,80}', tag) or not re.fullmatch(r'[0-9a-f]{40}', commit) or not token:
        raise SystemExit('Requires a TEST tag, exact commit and CI job authentication')
    project_url = 'https://github.com/GDLev/Proxmox-BetterDockers'
    base = api + '/projects/' + project
    encoded = urllib.parse.quote(tag, safe='')
    status, _ = curl(base + '/releases/' + encoded, token)
    if status != 404: raise SystemExit('Refusing to overwrite an existing release or continue after an API error')
    dist = ROOT / 'dist'
    with tempfile.TemporaryDirectory(prefix='bd-test-signing-') as folder:
        key = Path(folder) / 'key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([sys.executable, str(ROOT / 'packaging/sign-release.py'), '--key', str(key), '--output', str(dist)], check=True)
    version = subprocess.check_output([sys.executable, str(ROOT / 'scripts/dctlib/version.py')], text=True).strip()
    fingerprint = release.fingerprint(dist / 'release-key.pem')
    metadata = release.read_manifest(dist / 'RELEASE.json', dist / 'RELEASE.sig', dist / 'release-key.pem', fingerprint, version)
    names = sorted([*metadata['artifacts'], 'RELEASE.json', 'RELEASE.sig', 'release-key.pem'])
    links = []
    for name in names:
        url = base + '/packages/generic/betterdockers-test/' + encoded + '/' + urllib.parse.quote(name, safe='')
        status, _ = curl(url, token, 'PUT', upload=dist / name)
        if status != 201: raise SystemExit('Test package upload failed: ' + name + ' HTTP ' + str(status))
        links.append(dict(name=name, url=url, direct_asset_path='/' + name, link_type='package'))
    description = ('TEST ONLY. Not a production release. Uses a disposable signing key that must not be installed as production trust.\n\n'
        + 'Commit: `' + commit + '`\nPipeline: ' + os.environ.get('CI_PIPELINE_URL', '') + '\nTest public-key fingerprint: `' + fingerprint + '`\n'
        + 'Production signing and remaining release acceptance are still open.')
    status, _ = curl(base + '/releases', token, 'POST', body=dict(name='TEST acceptance ' + tag, tag_name=tag, ref=commit, description=description, assets=dict(links=links)))
    if status != 201: raise SystemExit('Test release creation failed HTTP ' + str(status))
    with tempfile.TemporaryDirectory(prefix='bd-test-download-') as folder:
        download = Path(folder)
        for name in names:
            url = project_url + '/-/releases/' + encoded + '/downloads/' + urllib.parse.quote(name, safe='')
            status, _ = curl(url, output=download / name)
            if status != 200 or release.digest(download / name) != release.digest(dist / name):
                raise SystemExit('Published download differs from built bytes: ' + name)
        verified = release.read_manifest(download / 'RELEASE.json', download / 'RELEASE.sig', download / 'release-key.pem', fingerprint, version)
        for name in verified['artifacts']: release.verify_artifact(verified, download / name, name)
    result = dict(test_only=True, tag=tag, commit=commit, release_url=project_url + '/-/releases/' + encoded, fingerprint=fingerprint,
        downloaded_hashes={name: release.digest(dist / name) for name in names})
    (dist / 'TEST_PUBLICATION.json').write_text(json.dumps(result, indent=2) + '\n')
    print('PASS test publication, anonymous direct downloads, exact byte hashes and signatures:', result['release_url'])


if __name__ == '__main__': main()
