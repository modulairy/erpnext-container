"""Create an idempotent GitHub release for an already verified GHCR publication."""
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request


def api(method, path, payload=None):
    request = urllib.request.Request('https://api.github.com/'+path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method, headers={'Authorization': 'Bearer '+os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
            'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == 'GET':
            return None
        raise RuntimeError(f'GitHub release API failed: {method} {path}, HTTP {error.code}') from None


def publish(lock, digest, repository, revision, run_id, run_number):
    tag = lock['erpnext_tag']
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('Invalid ERPNext tag')
    if repository != 'modulairy/erpnext-container' or not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Unexpected release source')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Invalid published digest')
    if not str(run_id).isdigit() or not str(run_number).isdigit():
        raise ValueError('Invalid Actions run')
    base = f'repos/{repository}'
    # Keep existing source tags immutable when rebuilding the same ERPNext release.
    existing = api('GET', base+'/git/ref/tags/'+tag)
    release_tag = tag if not existing or existing['object']['sha'] == revision else f'{tag}-{revision[:7]}'
    ref = existing if release_tag == tag else api('GET', base+'/git/ref/tags/'+release_tag)
    if ref and (ref['object']['type'] != 'commit' or ref['object']['sha'] != revision):
        raise ValueError('Release tag already identifies another source; refusing to move it')
    if not ref:
        api('POST', base+'/git/refs', {'ref':'refs/tags/'+release_tag, 'sha':revision})
    image = 'ghcr.io/modulairy/erpnext:'+tag
    versions = '\n'.join(f"- {a['name']}: {a['ref']} (`{a['sha']}`)" for a in lock['apps'])
    backports = '\n'.join(f"- {name}: {entry['upstream_pr']} (patch SHA-256 `{entry['sha256']}`)"
                         for name, entry in lock.get('community_patches', {}).items())
    body = (f'Community ERPNext image: `{image}`\n\n'
        f'Immutable image: `{image.split(":")[0]}@{digest}`\n\n'
        f'Source: `{revision}`\n\n'
        f'[Build, test and publication evidence](https://github.com/{repository}/actions/runs/{run_id})\n\n'
        'Application installation/migrations, API/queues/realtime, Chromium PDFs and Redis cache checks passed. '
        'The security gate uses the repository\'s owner-approved exact OS exceptions; external MariaDB findings are advisory. '
        'Passing the gate does not mean the image has no known vulnerabilities. '
        'SBOM and provenance attestations are published with this digest.\n\n'
        'Locked application sources:\n'+versions+
        ('\n\nReviewed Frappe 16 backports:\n'+backports if backports else ''))
    release = api('GET', base+'/releases/tags/'+release_tag)
    payload = {'tag_name':release_tag,'target_commitish':revision,'name':release_tag,
               'body':body,'draft':False,'prerelease':False,'make_latest':'true'}
    if release:
        if release.get('immutable'):
            if digest not in release.get('body',''):
                raise ValueError('Existing immutable release describes another image')
        else:
            release = api('PATCH', base+'/releases/'+str(release['id']), payload)
    else:
        release = api('POST', base+'/releases', payload)
    # Read back both source ref and saved release rather than treating a POST as proof.
    saved = api('GET', base+'/releases/tags/'+release_tag)
    saved_ref = api('GET', base+'/git/ref/tags/'+release_tag)
    if not saved or digest not in saved.get('body','') or saved.get('draft') or saved_ref['object']['sha'] != revision:
        raise RuntimeError('GitHub release verification failed')
    return saved['html_url']


if __name__ == '__main__':
    url = publish(json.loads(Path('verified/lock.json').read_text()), os.environ['IMAGE_DIGEST'],
        os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_SHA'],
        os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_NUMBER'])
    print(url)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write('GitHub Release: '+url+'\n')
