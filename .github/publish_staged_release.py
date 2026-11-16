"""Verify transport chunks and publish only the approved release asset set."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import quote
from urllib.request import Request, urlopen
from zipfile import ZipFile

REPO = os.environ['GITHUB_REPOSITORY']
assert REPO == 'dkenldlqfur/CDS95_Windows'
BASE = 'repos/' + REPO
ROOT = Path('_release-staging')
meta = json.loads((ROOT / 'manifest.json').read_text())
version = os.environ['RELEASE_VERSION']
assert re.fullmatch(r'\d+\.\d+\.\d+', version) and meta['version'] == version
assert meta['previous'] == '1.3.0' and meta['installer'] == '1.1.8'
out = Path(os.environ['RUNNER_TEMP']) / 'verified-release'
out.mkdir(exist_ok=True)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def api(path, method='GET', payload=None):
    command = ['gh', 'api', path, '--method', method]
    if payload is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('GitHub API request failed: ' + path)
    return json.loads(result.stdout) if result.stdout.strip() else None


expected = {f'CDS95_{platform}_v{base}_to_v{version}.patch.zip{suffix}'
            for platform in ('Windows', 'macOS') for base in ('1.3.0', '1.1.8')
            for suffix in ('', '.sha256')}
assert {item['name'] for item in meta['assets']} == expected
for item in meta['assets']:
    assert '/' not in item['name'] and '\\' not in item['name']
    target = out / item['name']
    with target.open('wb') as destination:
        for part in item['parts']:
            assert re.fullmatch(r'part-\d+\.bin', part['name'])
            source = ROOT / part['name']
            assert source.stat().st_size == part['size'] and sha(source) == part['sha256']
            with source.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    destination.write(block)
    assert target.stat().st_size == item['size'] and sha(target) == item['sha256']
    if item['name'].endswith('.zip'):
        with ZipFile(target) as archive:
            manifest_name = 'update-manifest.json' if '_macOS_' in item['name'] else 'release-manifest.json'
            manifest = json.loads(archive.read(manifest_name))
            assert manifest['version'] == version
            records = {record['path']: record for record in manifest['files']}
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                assert not path.is_absolute() and '..' not in path.parts
                if entry.filename == manifest_name:
                    continue
                record = records[entry.filename]
                assert entry.file_size == record['size']
                with archive.open(entry) as stream:
                    assert hashlib.file_digest(stream, 'sha256').hexdigest() == record['sha256']
    print('Verified:', item['name'], flush=True)
for item in meta['assets']:
    if item['name'].endswith('.sha256'):
        digest, name = (out / item['name']).read_text().split()
        assert name == item['name'].removesuffix('.sha256') and digest == sha(out / name)

latest = api(BASE + '/releases/latest')
assert latest['tag_name'] == 'v1.1.8'
releases = api(BASE + '/releases?per_page=100')
matching = [release for release in releases if release['tag_name'] == 'v' + version]
assert len(matching) <= 1
release = matching[0] if matching else api(BASE + '/releases', 'POST', {
    'tag_name': 'v' + version, 'name': 'CDS95 v' + version, 'draft': True,
    'prerelease': False, 'body': meta['notes']})
existing = {asset['name']: asset for asset in release['assets']}
assert not (existing.keys() - expected)
for item in meta['assets']:
    name = item['name']
    if name in existing:
        asset = existing[name]
        assert asset['size'] == item['size'] and asset['digest'] == 'sha256:' + item['sha256']
        continue
    path = out / name
    url = f"https://uploads.github.com/{BASE}/releases/{release['id']}/assets?name={quote(name)}"
    # Upload host uses the actual repository-scoped Actions token, without forwarding user credentials.
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'User-Agent': 'CDS95-release',
               'Content-Type': 'application/octet-stream', 'Content-Length': str(item['size'])}
    with path.open('rb') as stream:
        with urlopen(Request(url, data=stream, headers=headers, method='POST'), timeout=300) as response:
            asset = json.load(response)
    assert asset['size'] == item['size'] and asset['digest'] == 'sha256:' + item['sha256']
    print('Uploaded:', name, flush=True)
release = api(BASE + '/releases/' + str(release['id']), 'PATCH', {
    'draft': False, 'prerelease': False, 'make_latest': 'false', 'body': meta['notes']})
assert not release['draft'] and release['tag_name'] == 'v' + version
public = api(BASE + '/releases/' + str(release['id']))
assert {asset['name'] for asset in public['assets']} == expected
for item in meta['assets']:
    asset = next(asset for asset in public['assets'] if asset['name'] == item['name'])
    assert asset['size'] == item['size'] and asset['digest'] == 'sha256:' + item['sha256']
assert api(BASE + '/releases/latest')['tag_name'] == 'v1.1.8'
print('Published:', public['html_url'], flush=True)
