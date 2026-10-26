import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from community_overlay import apply_community, selected_patches
from release import publish


class CommunityTests(unittest.TestCase):
    def test_version_drift_blocks_before_build(self):
        with self.assertRaisesRegex(ValueError, 'Review compatibility patch'):
            selected_patches([{'name':'frappe','version':'17.0.0'}])

    def test_reviewed_patch_hashes_and_retry_contract(self):
        root = Path(__file__).resolve().parents[1]/'patches'
        entries = selected_patches([{'name':'frappe','version':'16.53.0'},
                                   {'name':'helpdesk','version':'1.31.0'}], root)
        self.assertEqual(entries['helpdesk']['sha256'], hashlib.sha256((root/'helpdesk.patch').read_bytes()).hexdigest())
        self.assertNotIn('rq_retry=Retry', (root/'helpdesk.patch').read_text())
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory)
            with patch('community_overlay.policy', return_value=(root, entries)):
                with self.assertRaisesRegex(ValueError, 'changed upstream'):
                    apply_community(app, {'name':'frappe','version':'16.53.0'}, entries)


class ReleaseTests(unittest.TestCase):
    def test_idempotent_release_and_immutable_source_tags(self):
        state = {}
        calls = []
        def api(method, path, payload=None):
            calls.append((method,path,payload))
            prefix='repos/modulairy/erpnext-container'
            if method=='POST' and path.endswith('/git/refs'):
                key=prefix+'/git/ref/'+payload['ref'].removeprefix('refs/')
                state[key]={'object':{'type':'commit','sha':payload['sha']}}
                return state[key]
            if method=='POST' and path.endswith('/releases'):
                value=dict(payload,id=1,html_url='https://github.com/modulairy/erpnext-container/releases/tag/'+payload['tag_name'])
                state[prefix+'/releases/tags/'+payload['tag_name']]=value
                return value
            if method=='PATCH':
                value=dict(payload,id=1,html_url='https://github.com/modulairy/erpnext-container/releases/tag/'+payload['tag_name'])
                state[prefix+'/releases/tags/'+payload['tag_name']]=value
                return value
            return state.get(path)
        lock={'erpnext_tag':'v16.52.1','apps':[]}
        args=(lock,'sha256:'+'a'*64,'modulairy/erpnext-container','b'*40,'123','24')
        with patch('release.api',side_effect=api):
            self.assertTrue(publish(*args).endswith('/v16.52.1'))
            self.assertTrue(publish(*args).endswith('/v16.52.1'))
            self.assertEqual(sum(m=='POST' and p.endswith('/git/refs') for m,p,_ in calls),1)
            changed=(*args[:3],'c'*40,'124','25')
            self.assertTrue(publish(*changed).endswith('/v16.52.1-build.25'))
        self.assertEqual(state['repos/modulairy/erpnext-container/git/ref/tags/v16.52.1']['object']['sha'],'b'*40)

    def test_invalid_publication_identifiers_never_write(self):
        with patch('release.api') as api:
            with self.assertRaises(ValueError):
                publish({'erpnext_tag':'latest','apps':[]},'bad','modulairy/erpnext-container','x','1','2')
            api.assert_not_called()
