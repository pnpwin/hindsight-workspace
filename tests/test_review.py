"""Independent review regressions; no real API/model calls."""
import json
import sys
import threading
import urllib.error
from pathlib import Path
import unittest
from unittest.mock import patch
import test_workspace as fixture
import test_plugin as plugin_fixture

import os
if os.environ.get('HERMES_SOURCE'):
    sys.path.insert(0, os.environ['HERMES_SOURCE'])
from agent.redact import redact_for_egress
from tools.terminal_scope import set_terminal_scope, reset_terminal_scope, TerminalPolicyRefusal
w = fixture.w


class PrivacyTests(unittest.TestCase):
    setUp = fixture.WorkspaceTests.setUp
    git = fixture.WorkspaceTests.git
    init_git = fixture.WorkspaceTests.init_git

    def test_partial_pem_never_reaches_host_model(self):
        marker = 'SYNTHETIC_KEY_MATERIAL_'
        (self.repo / 'README.md').write_text('-----BEGIN PRIVATE KEY-----\n' + marker * 300 + '\n-----END PRIVATE KEY-----')
        self.target.redact = redact_for_egress
        sources = []
        self.target.summarize = lambda source: sources.append(source) or '# Safe synthetic project map'
        w.sync_workspace(self.target)
        self.assertNotIn(marker, '\n'.join(sources))
        self.assertNotIn(marker, json.dumps(self.api.calls))

    def test_credential_urls_never_leave_workspace(self):
        (self.repo / 'README.md').write_text('URL https://synthetic-user:SYNTHETIC_PASSWORD@host.invalid/?token=SYNTHETIC_QUERY_TOKEN')
        self.target.redact = redact_for_egress
        sources = []
        self.target.summarize = lambda source: sources.append(source) or '# Safe synthetic project map'
        w.sync_workspace(self.target)
        for value in ('SYNTHETIC_PASSWORD', 'SYNTHETIC_QUERY_TOKEN'):
            self.assertNotIn(value, '\n'.join(sources))

    def test_partial_pem_in_git_messages(self):
        self.init_git()
        message = self.root / 'message.txt'
        message.write_text('-----BEGIN PRIVATE KEY-----\n' + 'SYNTHETIC_KEY_MATERIAL_' * 200 + '\n-----END PRIVATE KEY-----')
        self.git('commit', '--allow-empty', '-q', '-F', str(message))
        self.target.redact = redact_for_egress
        w.sync_workspace(self.target)
        self.assertNotIn('SYNTHETIC_KEY_MATERIAL_', json.dumps(self.api.calls))

    def test_missing_operation_response_closed(self):
        import io
        response = io.BytesIO(b'{}')
        error = urllib.error.HTTPError('http://fixture.invalid', 404, 'not found', {}, response)
        client = w.Client(self.target, threading.Event())
        replies = [error, {'success':True, 'bank_id':self.target.bank, 'operation_id':'op'}, {'status':'completed'}, {'bank_id':self.target.bank, 'id':w.DOC_GIT, 'original_text':'text'}]
        with patch.object(client, 'request', side_effect=replies):
            client.finish_retain(w.DOC_GIT, {'operation_id':'op', 'content':'text'})
        self.assertTrue(response.closed)

    def test_lost_ack_and_temporarily_invisible_operation_drained(self):
        client = w.Client(self.target, threading.Event())
        delayed, completed, docs, submissions = {}, set(), {}, []
        lose_ack = True
        def request(path, body=None, timeout=30):
            nonlocal lose_ack
            if path == '/memories':
                op = body['operation_id']
                item = body['items'][0]
                submissions.append(item['content'])
                if lose_ack:
                    delayed[op] = item
                    lose_ack = False
                    raise TimeoutError('accepted request; acknowledgement lost before transaction is visible')
                # The real idempotency contract: replaying the same pending ID joins its work.
                if op not in completed:
                    docs[item['document_id']] = item['content']
                    completed.add(op)
                    delayed.pop(op, None)
                return {'success':True, 'bank_id':self.target.bank, 'operation_id':op}
            if path.startswith('/operations/'):
                op = path.split('/')[-1]
                return {'status':'completed' if op in completed else 'not_found'}
            return {'bank_id':self.target.bank, 'id':w.DOC_GIT, 'original_text':docs[w.DOC_GIT]}
        with patch.object(client, 'request', side_effect=request):
            with self.assertRaises(TimeoutError):
                client.retain(w.DOC_GIT, 'older A')
            client.retain(w.DOC_GIT, 'newer B')
            for op, item in delayed.items():
                docs[item['document_id']] = item['content']
            self.assertEqual(docs[w.DOC_GIT], 'newer B')
            self.assertEqual(submissions, ['older A', 'older A', 'newer B'])


class LocalityTests(unittest.TestCase):
    setUp = plugin_fixture.PluginTests.setUp
    capture = plugin_fixture.PluginTests.capture

    def test_remote_and_refused_scope_skip(self):
        for scope in ({'TERMINAL_ENV':'ssh'}, {'TERMINAL_ENV':'docker'}, {'TERMINAL_ENV':'modal'}, TerminalPolicyRefusal('test refusal')):
            token = set_terminal_scope(scope)
            try:
                self.assertIsNone(self.capture())
            finally:
                reset_terminal_scope(token)

    def test_local_scope_does_not_inherit_remote_environment(self):
        with patch.dict('os.environ', {'TERMINAL_ENV':'ssh'}):
            token = set_terminal_scope({'TERMINAL_ENV':'local'})
            try:
                self.assertIsNotNone(self.capture())
            finally:
                reset_terminal_scope(token)
            self.assertIsNone(self.capture())
