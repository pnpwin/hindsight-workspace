"""Adapter boundary tests; native lifecycle gets a synthetic I/O delegate."""
import contextvars
import copy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import importlib
import inspect
import json
import os
import shutil
import threading
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
        self.native_cwd = contextvars.ContextVar('provider-test-native-cwd', default='')
        self.native_policy = contextvars.ContextVar('provider-test-native-policy', default=None)
        self.native_route = contextvars.ContextVar('provider-test-native-route', default='')
        self.native_profile = contextvars.ContextVar('provider-test-native-profile', default='default')
        runtime_cwd = importlib.import_module('agent.runtime_cwd')
        session_policy = importlib.import_module('gateway.session_policy')
        session_context = importlib.import_module('gateway.session_context')
        profiles = importlib.import_module('hermes_cli.profiles')
        self.enterContext(patch.object(runtime_cwd, 'scoped_session_cwd', side_effect=self.native_cwd.get))
        self.enterContext(patch.object(session_policy, 'active_policy', side_effect=self.native_policy.get))
        self.enterContext(patch.object(session_context, 'bound_session_key', side_effect=self.native_route.get))
        self.enterContext(patch.object(profiles, 'get_active_profile_name', side_effect=self.native_profile.get))
        self.adapter = self.m.WorkspaceMemoryProvider()

    def initialize(self, **kwargs):
        values = dict(hermes_home=str(self.home), agent_identity='default', cwd=self.cwd,
                      agent_workspace='hermes', platform='cli')
        values.update(kwargs)
        self.adapter.initialize('native-session-a', **values)

    def initialize_without_cwd(self, **kwargs):
        values = dict(hermes_home=str(self.home), agent_identity='default',
                      agent_workspace='hermes', platform='cli', gateway_session_key='gateway-session-a')
        values.update(kwargs)
        self.adapter.initialize('native-session-a', **values)

    @contextmanager
    def native_context(self, *, cwd=None, policy_cwd=None, route='gateway-session-a', profile='default', home=None):
        cwd = self.cwd if cwd is None else cwd
        policy_cwd = cwd if policy_cwd is None else policy_cwd
        tokens = (
            (self.native_cwd, self.native_cwd.set(cwd)),
            (self.native_policy, self.native_policy.set(type('Policy', (), {'cwd': policy_cwd})())),
            (self.native_route, self.native_route.set(route)),
            (self.native_profile, self.native_profile.set(profile)),
        )
        home_token = set_hermes_home_override(self.home if home is None else home)
        try:
            yield
        finally:
            reset_hermes_home_override(home_token)
            for var, token in reversed(tokens):
                var.reset(token)

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

    def test_missing_cwd_uses_only_complete_native_session_context(self):
        with self.native_context():
            self.initialize_without_cwd()
            self.assertEqual(self.delegate.calls[0][2]['cwd'], self.cwd)
            self.assertEqual(self.adapter.workspace_identity['cwd'], self.cwd)
            self.adapter.validate_workspace_context('native-session-a', self.cwd)
            self.assertEqual(self.adapter.prefetch('native route', session_id='native-session-a'),
                             'native recalled data')

    def test_missing_cwd_refuses_absent_or_inconsistent_native_context_before_loading(self):
        with patch('os.getcwd', return_value=self.cwd), self.assertRaises(ValueError):
            self.initialize_without_cwd()
        cases = (
            {'cwd': '', 'policy_cwd': ''},
            {'cwd': 'relative/project', 'policy_cwd': 'relative/project'},
            {'cwd': self.cwd, 'policy_cwd': '/different-project'},
            {'cwd': self.cwd, 'route': ''},
            {'cwd': self.cwd, 'route': 'another-route'},
            {'cwd': self.cwd, 'profile': 'coder'},
            {'cwd': self.cwd, 'home': self.home / 'other-profile'},
        )
        for context in cases:
            with self.subTest(context=context), self.native_context(**context), self.assertRaises(ValueError):
                self.initialize_without_cwd()
        self.assertEqual(self.delegate.calls, [])
        self.loader.assert_not_called()

    def test_explicit_cwd_remains_supported_but_cannot_override_active_native_cwd(self):
        with self.native_context():
            with self.assertRaises(ValueError):
                self.initialize(cwd='/different-project')
        self.assertEqual(self.delegate.calls, [])
        self.initialize()
        self.assertEqual(self.delegate.calls[0][2]['cwd'], self.cwd)

    def test_fallback_binding_refuses_other_route_cwd_profile_and_tool_route(self):
        with self.native_context():
            self.initialize_without_cwd()
        before = copy.deepcopy(self.delegate.calls)
        invalid_contexts = (
            {'cwd': self.cwd, 'route': 'other-session'},
            {'cwd': '/different-project', 'route': 'gateway-session-a'},
            {'cwd': self.cwd, 'route': 'gateway-session-a', 'profile': 'coder'},
        )
        for context in invalid_contexts:
            with self.subTest(context=context), self.native_context(**context), self.assertRaises(ValueError):
                self.adapter.prefetch('must not cross native context', session_id='native-session-a')
        with self.native_context():
            with self.assertRaises(ValueError):
                self.adapter.handle_tool_call('hindsight_recall', {'query': 'x'}, caller_gsk='other-session')
            with self.assertRaises(ValueError):
                self.adapter.validate_workspace_context('other-session', self.cwd)
            with self.assertRaises(ValueError):
                self.adapter.validate_workspace_context('native-session-a', '/different-project')
        self.assertEqual(self.delegate.calls, before)

    def test_explicit_cwd_in_native_turn_keeps_route_binding(self):
        with self.native_context():
            self.initialize(gateway_session_key='gateway-session-a')
        with self.native_context(route='foreign-session'), self.assertRaises(ValueError):
            self.adapter.handle_tool_call('hindsight_recall', {'query': 'x'})
        self.assertEqual([entry[0] for entry in self.delegate.calls], ['initialize'])

    def test_content_session_end_override_still_requires_native_context(self):
        with self.native_context():
            self.initialize_without_cwd()
        def content_end(delegate, messages):
            delegate.calls.append(('end', messages))
        with patch.object(Delegate, 'on_session_end', content_end), self.assertRaises(ValueError):
            self.adapter.on_session_end([{'content': 'must not reach delegate'}])
        self.assertEqual([entry[0] for entry in self.delegate.calls], ['initialize'])

    def test_concurrent_native_routes_are_isolated(self):
        with self.native_context():
            self.initialize_without_cwd()
            valid = contextvars.copy_context()
            foreign = contextvars.copy_context()
        foreign.run(self.native_route.set, 'foreign-session')
        barrier = threading.Barrier(2)

        def call():
            barrier.wait(timeout=3)
            return self.adapter.prefetch('concurrent route', session_id='native-session-a')

        with ThreadPoolExecutor(max_workers=2) as pool:
            valid_call = pool.submit(valid.run, call)
            foreign_call = pool.submit(foreign.run, call)
            self.assertEqual(valid_call.result(timeout=5), 'native recalled data')
            with self.assertRaises(ValueError):
                foreign_call.result(timeout=5)
        self.assertEqual([entry[0] for entry in self.delegate.calls], ['initialize', 'prefetch'])

    def test_fallback_binding_preserves_compression_switch_and_shutdown(self):
        with self.native_context():
            self.initialize_without_cwd()
        with self.native_context():
            self.adapter.on_session_switch('native-session-b', parent_session_id='native-session-a', reset=True)
            self.assertEqual(self.adapter.workspace_identity['session_id'], 'native-session-b')
            self.assertEqual(self.adapter.prefetch('after compression', session_id='native-session-b'),
                             'native recalled data')
        self.adapter.shutdown()
        self.assertEqual(self.delegate.calls[-1], ('shutdown',))

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
