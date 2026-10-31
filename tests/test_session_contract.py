"""Native Hermes cwd qualification, offline and without a gateway or provider.

Set HERMES_SOURCE to the inspected Hermes source tree. Run under its managed
Python with TMPDIR pointing to owned scratch. Dependency fixtures isolate host
configuration; the cwd resolver bodies and native rebind seam remain unchanged.
Outside a project the user-approved native default is valid; the adapter
must still reject an absent cwd rather than guessing one itself.
"""
from __future__ import annotations

import contextlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


@unittest.skipUnless(os.environ.get('HERMES_SOURCE'), 'native qualification requires HERMES_SOURCE')
class NativeSessionContractTests(unittest.TestCase):
    def setUp(self):
        source = Path(os.environ['HERMES_SOURCE']).resolve()
        sys.path.insert(0, str(source))
        self.addCleanup(lambda: sys.path.remove(str(source)))
        native = importlib.import_module('tui_gateway.session_workdir')
        assert isinstance(native.__file__, str)
        self.assertEqual(Path(native.__file__).resolve(), source / 'tui_gateway/session_workdir.py')
        rebind = importlib.import_module('tui_gateway.method_ctx').rebind
        scratch = Path(os.environ['TMPDIR'])
        self.assertTrue(scratch.is_absolute() and scratch.is_dir())
        tmp = tempfile.TemporaryDirectory(prefix='workspace-session-contract-', dir=scratch)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.launch = self.root / 'gateway-launch'
        self.project = self.root / 'selected-project'
        self.chat = self.root / 'chat-folder'
        for path in (self.launch, self.project, self.chat):
            path.mkdir()
        self.sessions = {}
        # Only host/config boundaries are fixtures. No global chdir/env patch,
        # database, provider construction, plugin discovery or network is used.
        g = {
            '__builtins__': __builtins__,
            'os': SimpleNamespace(path=os.path, name=os.name, environ={}, getcwd=lambda: str(self.launch)),
            'contextlib': contextlib,
            '_sessions': self.sessions,
            '_profile_home': lambda profile: None,
            'ProfileUnavailableError': RuntimeError,
            '_profile_workspace_cwd': lambda home: '',
            '_profile_default_cwd': lambda home: '',
            '_launch_configured_cwd': lambda: '',
            '_sandbox_workspace_cwd': lambda home: '',
            '_cwd_is_remote': lambda home: False,
            '_is_remote_cwd_shape': lambda raw: False,
            '_is_container_path': lambda raw: False,
            '_bound_terminal_backend': lambda home: 'local',
            '_LAUNCH_CWD_NOT_A_WORKSPACE': {'desktop'},
            '_session_source': lambda session: session.get('source', ''),
            '_is_remote_launch_cwd': lambda session: False,
        }
        for name in ('_completion_cwd', '_resolve_create_cwd', '_session_cwd', '_persisted_session_cwd'):
            g[name] = rebind(getattr(native, name), g)
        self.create = g['_resolve_create_cwd']
        self.complete = g['_completion_cwd']
        self.persisted = g['_persisted_session_cwd']

    def test_selected_project_wins_over_launch_cwd(self):
        explicit, cwd, remote = self.create({'cwd': str(self.project), 'cwd_explicit': True}, 'desktop', None)
        self.assertTrue(explicit)
        self.assertFalse(remote)
        self.assertEqual(cwd, str(self.project))
        self.assertNotEqual(cwd, str(self.launch))

    def test_chat_directory_is_session_cwd_when_client_supplies_it(self):
        explicit, cwd, _ = self.create({'cwd': str(self.chat), 'cwd_explicit': True}, 'desktop', None)
        self.assertTrue(explicit)
        self.assertEqual(cwd, str(self.chat))
        self.assertEqual(self.persisted({'cwd': cwd, 'explicit_cwd': explicit, 'source': 'desktop'}), str(self.chat))

    def test_no_project_uses_native_default_directory(self):
        _explicit, cwd, _remote = self.create({}, 'desktop', None)
        # The user explicitly approved the native default outside projects;
        # its HOME case maps to the existing home bank in the shared resolver.
        self.assertEqual(cwd, str(self.launch))

    def test_resume_keeps_workspace(self):
        self.sessions['chat-a'] = {'cwd': str(self.project), 'explicit_cwd': True}
        self.assertEqual(self.complete({'session_id': 'chat-a'}), str(self.project))

    def test_two_chats_do_not_share_session_binding(self):
        self.sessions['chat-a'] = {'cwd': str(self.project), 'explicit_cwd': True}
        self.sessions['chat-b'] = {'cwd': str(self.chat), 'explicit_cwd': True}
        self.assertEqual(self.complete({'session_id': 'chat-a'}), str(self.project))
        self.assertEqual(self.complete({'session_id': 'chat-b'}), str(self.chat))
        self.assertNotEqual(self.complete({'session_id': 'chat-a'}), self.complete({'session_id': 'chat-b'}))


if __name__ == '__main__':
    unittest.main()
