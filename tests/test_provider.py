"""Adapter boundary tests; native lifecycle gets a synthetic I/O delegate."""
import copy
import inspect
import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_plugin as fixture
from agent.memory_provider import MemoryProvider
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
import policy


class Delegate(MemoryProvider):
    name = 'hindsight'
    def __init__(self):
        self.calls = []
    def is_available(self):
        return True
    def initialize(self, session_id, **kwargs):
        self.calls.append(('initialize', session_id, kwargs))
    def get_tool_schemas(self):
        return [{'name': 'hindsight_recall'}]
    def sync_turn(self, user, assistant, *, session_id=''):
        self.calls.append(('sync_turn', user, assistant, session_id))
    def prefetch(self, query, *, session_id=''):
        self.calls.append(('prefetch', query, session_id))
        return 'native recalled data'
    def handle_tool_call(self, name, args, **kwargs):
        self.calls.append(('tool', name, args, kwargs))
        return '{"ok":true}'
    def on_session_switch(self, session_id, **kwargs):
        self.calls.append(('switch', session_id, kwargs))
    def shutdown(self):
        self.calls.append(('shutdown',))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        path = fixture.ROOT / 'provider.py'
        self.assertTrue(path.exists(), 'workspace provider adapter is missing')
        fixture.load('workspace_adapter_fixture', fixture.ROOT / '__init__.py', True)
        self.m = fixture.load('workspace_adapter_fixture.provider', path)
        tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.owner = self.home / 'owner/hindsight/config.json'
        self.owner.parent.mkdir(parents=True)
        self.policy = policy.normalize_policy({})
        self.owner.write_text(json.dumps(self.policy))
        (self.home / 'hindsight-workspace').mkdir()
        (self.home / 'hindsight-workspace/config.json').write_text(json.dumps({'policy_path': str(self.owner)}))
        (self.home / 'hindsight').mkdir()
        self.native_config = self.home / 'hindsight/config.json'
        self.native_config.write_text(json.dumps(self.policy))
        self.cwd = str(self.home / 'selected project')
        self.delegate = Delegate()
        self.loader = self.enterContext(patch.object(self.m, 'load_memory_provider', return_value=self.delegate))
        self.native_root = self.home / 'plugins/hindsight'
        shutil.copytree(Path(os.environ['HINDSIGHT_UPSTREAM']), self.native_root)
        self.enterContext(patch.object(self.m, 'find_provider_dir', return_value=self.native_root, create=True))
        self.origin = self.enterContext(patch.object(self.m.inspect, 'getfile', return_value=str(self.native_root / '__init__.py')))
        self.enterContext(patch.object(self.m, 'get_secret', return_value=''))
        token = set_hermes_home_override(self.home)
        self.addCleanup(reset_hermes_home_override, token)
        self.adapter = self.m.WorkspaceMemoryProvider()

    def initialize(self, **kwargs):
        values = dict(hermes_home=str(self.home), agent_identity='default', cwd=self.cwd,
                      agent_workspace='hermes', platform='cli')
        values.update(kwargs)
        self.adapter.initialize('native-session-a', **values)

    def test_native_initialize_receives_identity_before_any_memory_call(self):
        self.initialize()
        identity = self.adapter.workspace_identity
        self.assertEqual(self.delegate.calls[0][2]['agent_workspace'], identity['workspace'])
        self.assertEqual(self.delegate.calls[0][2]['cwd'], self.cwd)
        self.assertEqual(identity['bank'], policy.resolve_workspace('', self.cwd))
        self.loader.assert_called_once_with('hindsight', register_skills=False)
        self.assertEqual(self.adapter.get_tool_schemas(), self.delegate.get_tool_schemas())

    def test_unapproved_source_overlay_and_wrong_origin_refuse_before_init(self):
        initial = (self.native_root / '__init__.py').read_bytes()
        for kind in ('source', 'overlay', 'origin'):
            with self.subTest(kind=kind):
                self.adapter = self.m.WorkspaceMemoryProvider()
                self.delegate.calls.clear()
                (self.native_root / '__init__.py').write_bytes(initial)
                (self.native_root / 'workspace_bank.py').unlink(missing_ok=True)
                self.loader.reset_mock()
                if kind == 'source':
                    (self.native_root / '__init__.py').write_bytes(initial + b'\n# unapproved revision')
                elif kind == 'overlay':
                    (self.native_root / 'workspace_bank.py').write_text('# old overlay')
                else:
                    self.origin.return_value = str(self.home / 'foreign/__init__.py')
                with self.assertRaises(ValueError):
                    self.initialize()
                self.assertEqual(self.delegate.calls, [])
                if kind != 'origin':
                    self.loader.assert_not_called()
                (self.native_root / '__init__.py').write_bytes(initial)
                (self.native_root / 'workspace_bank.py').unlink(missing_ok=True)

    def test_cached_delegate_origin_is_checked_before_each_call(self):
        self.initialize()
        self.origin.return_value = str(self.home / 'foreign/__init__.py')
        with self.assertRaises(ValueError):
            self.adapter.prefetch('must not read memory', session_id='native-session-a')
        self.assertEqual([call[0] for call in self.delegate.calls], ['initialize'])

    def test_invalid_cwd_rejected_before_delegate_loaded(self):
        for cwd in ('', '.', None):
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                self.initialize(cwd=cwd)
        self.assertEqual(self.delegate.calls, [])
        self.loader.assert_not_called()

    def test_selected_cwd_mismatch_refused(self):
        with self.assertRaises(ValueError):
            self.initialize(selected_cwd='/different-selected-project')
        self.assertEqual(self.delegate.calls, [])

    def test_incomplete_native_projection_refused_before_init(self):
        raw = self.policy.copy()
        del raw['recall_max_input_chars']
        self.native_config.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual(self.delegate.calls, [])

    def test_native_projection_cannot_disagree_with_owner(self):
        self.native_config.write_text(json.dumps({**self.policy, 'recall_budget': 'high'}))
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual(self.delegate.calls, [])

    def test_environment_cannot_redirect_bank_or_empty_retain_shaping(self):
        for key, value in [('HINDSIGHT_BANK_ID', 'other'), ('HINDSIGHT_RETAIN_TAGS', 'foreign'),
                           ('HINDSIGHT_RETAIN_STRATEGY', 'foreign')]:
            with self.subTest(key=key), patch.object(self.m, 'get_secret', side_effect=lambda name, default='': value if name == key else default):
                with self.assertRaises(ValueError):
                    self.initialize()
        self.assertEqual(self.delegate.calls, [])

    def test_policy_change_refuses_new_requests_but_keeps_cleanup(self):
        self.initialize()
        self.owner.write_text(json.dumps({**self.policy, 'recall_budget': 'high'}))
        before = copy.deepcopy(self.delegate.calls)
        with self.assertRaises(ValueError):
            self.adapter.prefetch('not admitted', session_id='native-session-a')
        self.assertEqual(self.delegate.calls, before)
        self.adapter.shutdown()
        self.assertEqual(self.delegate.calls[-1], ('shutdown',))

    def test_readonly_identity_and_cross_session_profile_refusal(self):
        self.initialize()
        returned = self.adapter.workspace_identity
        returned['bank'] = 'injected'
        self.assertNotEqual(self.adapter.workspace_identity['bank'], 'injected')
        for kwargs in ({'session_id': 'other'}, {'hermes_home': '/other'}, {'agent_identity': 'other'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.adapter.handle_tool_call('hindsight_recall', {'query': 'x'}, **kwargs)
        self.assertEqual(len(self.delegate.calls), 1)

    def test_memory_methods_delegate_with_native_signature(self):
        self.initialize()
        self.assertEqual(self.adapter.prefetch('x', session_id='native-session-a'), 'native recalled data')
        self.adapter.sync_turn('u', 'a', session_id='native-session-a', messages=[], turn_author={'id': 'x'})
        self.assertEqual(self.delegate.calls[-1], ('sync_turn', 'u', 'a', 'native-session-a'))
        self.assertEqual(self.adapter.handle_tool_call('hindsight_recall', {'query': 'x'}), '{"ok":true}')

    def test_all_current_abc_methods_have_adapter_implementations(self):
        methods = {name for name, member in vars(MemoryProvider).items() if not name.startswith('_') and inspect.isfunction(member)}
        self.assertFalse(methods - set(vars(type(self.adapter))))

    def test_switch_flushes_under_old_identity_then_rebinds_same_workspace(self):
        self.initialize()
        bank = self.adapter.workspace_identity['bank']
        self.adapter.on_session_switch('native-session-b', parent_session_id='native-session-a', reset=True)
        self.assertEqual(self.delegate.calls[-1], ('switch', 'native-session-b', {'parent_session_id': 'native-session-a', 'reset': True}))
        self.assertEqual(self.adapter.workspace_identity['session_id'], 'native-session-b')
        self.assertEqual(self.adapter.workspace_identity['bank'], bank)
        with self.assertRaises(ValueError):
            self.adapter.on_session_switch('native-session-c', cwd='/new-workspace')
        self.assertEqual(self.adapter.workspace_identity['session_id'], 'native-session-b')

    def test_initialized_instance_cannot_be_rebound(self):
        self.initialize()
        with self.assertRaises(ValueError):
            self.initialize(cwd='/different')
        self.assertEqual(len(self.delegate.calls), 1)

    def test_two_instances_have_distinct_native_delegates(self):
        other = Delegate()
        self.loader.side_effect = [self.delegate, other]
        self.initialize()
        second = self.m.WorkspaceMemoryProvider()
        second.initialize('native-session-b', cwd='/different', hermes_home=str(self.home), agent_identity='default')
        self.assertNotEqual(self.adapter.workspace_identity['bank'], second.workspace_identity['bank'])
        self.adapter.shutdown()
        self.assertEqual(len(other.calls), 1)
