"""Shared-bank writes from distinct profile homes must not poison retries."""
import json
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
import test_workspace as fixture

w = fixture.w


class ProfileConflictTests(unittest.TestCase):
    root: Path
    repo: Path
    target: Any
    api: fixture.FakeAPI
    setUp = fixture.WorkspaceTests.setUp

    def collide(self):
        a = w.Client(self.target, threading.Event())
        other = w.Target(cwd=self.repo, bank=self.target.bank, api_url=self.api.url,
                         data_dir=self.root/'profile-b')
        b = w.Client(other, threading.Event())
        request = a.request
        injected = False
        def interleave(path, body=None, timeout=30):
            nonlocal injected
            if path.startswith('/documents/') and not injected:
                injected = True
                b.retain(w.DOC_GIT, 'profile B')
            return request(path, body, timeout)
        with patch.object(a, 'request', side_effect=interleave):
            with self.assertRaisesRegex(RuntimeError, 'readback mismatch'):
                a.retain(w.DOC_GIT, 'profile A')
        original = self.api.calls[0][1]['operation_id']
        self.assertEqual(self.api.ops[original], 'completed')
        self.assertEqual(self.api.docs[(self.target.bank, w.DOC_GIT)], 'profile B')
        return a, original

    def test_completed_conflict_retries_same_content_with_fresh_operation(self):
        a, original = self.collide()
        operation = a.retain(w.DOC_GIT, 'profile A')
        self.assertNotEqual(operation, original)
        self.assertEqual(self.api.docs[(self.target.bank, w.DOC_GIT)], 'profile A')
        self.assertFalse((self.target.state_root/(w.DOC_GIT+'-pending.json')).exists())

    def test_completed_conflict_does_not_block_new_content(self):
        a, original = self.collide()
        operation = a.retain(w.DOC_GIT, 'newer A')
        self.assertNotEqual(operation, original)
        self.assertEqual(self.api.docs[(self.target.bank, w.DOC_GIT)], 'newer A')

    def test_survey_conflict_does_not_advance_cursor_and_recovers(self):
        other = w.Target(cwd=self.repo, bank=self.target.bank, api_url=self.api.url,
                         data_dir=self.root/'profile-b', summarize=lambda source: '# Map from profile B, independently generated')
        request = w.Client.request
        injected = False
        def interleave(client, path, body=None, timeout=30):
            nonlocal injected
            if client.target is self.target and path.startswith('/documents/') and not injected:
                injected = True
                w.sync_workspace(other)
            return request(client, path, body, timeout)
        with patch.object(w.Client, 'request', new=interleave):
            with self.assertRaisesRegex(RuntimeError, 'readback mismatch'):
                w.sync_workspace(self.target)
        self.assertFalse((self.target.state_root/'state.json').exists())
        self.assertEqual(w.sync_workspace(self.target)['survey'], 'updated')
        self.assertEqual(w.sync_workspace(self.target)['survey'], 'unchanged')

    def test_wrong_document_identity_keeps_original_pending_receipt(self):
        client = w.Client(self.target, threading.Event())
        request = client.request
        def wrong_identity(path, body=None, timeout=30):
            reply = request(path, body, timeout)
            if path.startswith('/documents/'):
                reply['bank_id'] = 'wrong-bank'
            return reply
        with patch.object(client, 'request', side_effect=wrong_identity):
            with self.assertRaisesRegex(RuntimeError, 'readback mismatch'):
                client.retain(w.DOC_GIT, 'profile A')
        pending_path = self.target.state_root/(w.DOC_GIT+'-pending.json')
        operation = json.loads(pending_path.read_text())['operation_id']
        self.assertEqual(client.retain(w.DOC_GIT, 'profile A'), operation)
