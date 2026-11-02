import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import os
import policy

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'tests/fixtures/legacy_provider'
if os.environ.get('HERMES_SOURCE'):
    sys.path.insert(0, os.environ['HERMES_SOURCE'])


def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)] if package else None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / '__init__.py').exists(), 'native adapter is missing')
        self.p = load('workspace_plugin_fixture', ROOT / '__init__.py', True)
        scratch = Path(os.environ.get('TMPDIR', str(ROOT / 'test-tmp')))
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cwd = self.home / 'project'
        self.cwd.mkdir()
        native = types.ModuleType('fixture_native')
        native.__path__ = [str(NATIVE)]
        sys.modules['fixture_native'] = native
        self.resolver = load('fixture_native.workspace_bank', NATIVE / 'workspace_bank.py')
        load('fixture_native.settings', NATIVE / 'settings.py')
        self.settings = policy.normalize_policy({})
        identity = policy.resolve_identity(session_id='session-a', cwd=str(self.cwd), selected_cwd=str(self.cwd), policy=self.settings)
        (self.home / 'hindsight').mkdir()
        self.policy_path = self.home / 'hindsight/config.json'
        self.policy_path.write_text(__import__('json').dumps(self.settings))
        self.provider = types.SimpleNamespace(workspace_identity=identity, workspace_policy=self.settings.copy(), bound_profile='default', bound_home=self.home, policy_path=self.policy_path)
        self.provider.validate_workspace_context = lambda **kw: None
        self.agent = types.SimpleNamespace(session_id='session-a', session_cwd=str(self.cwd), _memory_manager=types.SimpleNamespace(get_provider=lambda name: self.provider if name == 'hindsight-workspace' else None))
        self.ctx = types.SimpleNamespace(session_cwd=lambda: str(self.cwd), _manager=types.SimpleNamespace(home_path=str(self.home)))
        (self.home / 'config.yaml').write_text(__import__('json').dumps({'auxiliary': {'hindsight_workspace_survey': {'provider': 'openai', 'model': 'synthetic-survey'}}}))
    def capture(self, **kwargs):
        params = dict(ctx=self.ctx, agent=self.agent, session_id='session-a', profile='default', home=self.home, settings=self.settings, redact=lambda s:s)
        params.update(kwargs)
        return self.p.capture_target(**params)
    def test_survey_uses_native_llm_without_overrides(self):
        calls = []
        self.ctx.llm = types.SimpleNamespace(complete=lambda messages, **kwargs: calls.append((messages, kwargs)) or types.SimpleNamespace(text='Project map from host'))
        target = self.capture()
        self.assertIsNotNone(target)
        self.assertTrue(callable(target.summarize))
        self.assertEqual(target.summarize('source marker'), 'Project map from host')
        self.assertIn('source marker', str(calls[0][0]))
        self.assertEqual(calls[0][1].get('task'), 'hindsight_workspace_survey')
        for key in ('model', 'provider', 'profile', 'agent_id', 'tools'):
            self.assertNotIn(key, calls[0][1])

    def test_actual_native_resolver_bank_parity(self):
        target = self.capture()
        self.assertIsNotNone(target)
        self.assertEqual(target.bank, self.resolver.resolve_workspace('', str(self.cwd)))
        self.assertEqual(target.data_dir, self.home / 'plugin-data/hindsight-workspace')
    def test_cwd_change_cannot_reuse_pinned_bank(self):
        other = self.home / 'other'
        other.mkdir()
        self.ctx.session_cwd = lambda: str(other)
        self.assertIsNone(self.capture())
    def test_missing_agent_cwd_uses_validated_provider_binding(self):
        self.agent.session_cwd = None
        calls = []
        self.provider.validate_workspace_context = lambda **kw: calls.append(kw)
        self.assertIsNotNone(self.capture())
        self.assertEqual(calls, [{'session_id': 'session-a', 'cwd': str(self.cwd)}])
    def test_missing_agent_cwd_without_valid_context_skips(self):
        self.agent.session_cwd = None
        del self.provider.validate_workspace_context
        self.assertIsNone(self.capture())
        def refused(**kwargs):
            raise ValueError('foreign native route')
        self.provider.validate_workspace_context = refused
        self.assertIsNone(self.capture())
        self.agent.session_cwd = str(self.cwd)
        self.assertIsNone(self.capture())
    def test_explicit_conflicting_agent_cwd_skips(self):
        self.agent.session_cwd = str(self.home / 'other')
        self.provider.validate_workspace_context = lambda **kw: None
        self.assertIsNone(self.capture())
    def test_same_basename_different_path_isolated(self):
        other = self.home / 'other/project'
        other.mkdir(parents=True)
        self.assertNotEqual(self.resolver.resolve_workspace('', str(other)), self.provider.workspace_identity['bank'])
    def test_absent_and_relative_cwd_skip(self):
        for value in ('', '.', '~/project'):
            self.ctx.session_cwd = lambda: value
            self.assertIsNone(self.capture())
    def test_other_provider_profile_skipped(self):
        self.assertIsNone(self.capture(profile='coder'))
    def test_session_identity_mismatch_skips(self):
        self.assertIsNone(self.capture(session_id='wrong'))
        self.provider.workspace_identity['session_id'] = 'wrong'
        self.assertIsNone(self.capture())
    def test_provider_disabled_or_absent_skips(self):
        self.provider.workspace_identity = None
        self.assertIsNone(self.capture())
        self.agent._memory_manager = None
        self.assertIsNone(self.capture())
    def test_settings_change_skips(self):
        for key, value in [('bank_id_template','shared'), ('auto_recall',False), ('api_url','http://127.0.0.1:9077')]:
            altered = {**self.settings, key:value}
            self.assertIsNone(self.capture(settings=altered))
    def test_bank_mismatch_skips(self):
        self.provider.workspace_identity['bank'] = 'hermes'
        self.assertIsNone(self.capture())
    def test_queue_is_bounded_deduplicated_and_stoppable(self):
        entered, release = threading.Event(), threading.Event()
        def sync(target, *, stop):
            entered.set()
            release.wait(5)
            return {'status':'ok'}
        worker = self.p.Worker(sync=sync)
        self.addCleanup(worker.close)
        with patch.object(self.p, 'atomic_json'):
            target = self.capture()
            self.assertIsNotNone(target)
            self.assertTrue(worker.submit(target))
            self.assertTrue(entered.wait(2))
            self.assertFalse(worker.submit(target))
            worker.close()
            self.assertFalse(worker.submit(target))
            release.set()
            self.assertTrue(worker.wait(2))
    def test_exclusive_provider_is_runtime_gated_not_plugin_dependency(self):
        self.assertNotIn('requires_plugins:', (ROOT / 'plugin.yaml').read_text())

    def test_registration_has_one_provider_one_hook_and_unloads(self):
        hooks, unload, providers, tasks = [], [], [], []
        ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.append((name,fn)), on_unload=unload.append, register_memory_provider=providers.append,
                                   register_auxiliary_task=lambda name, **kw: tasks.append((name, kw)))
        self.p.register(ctx)
        self.assertEqual([p.name for p in providers], ['hindsight-workspace'])
        self.assertEqual([name for name,_ in hooks], ['pre_llm_call'])
        self.assertEqual([name for name, _ in tasks], ['hindsight_workspace_survey'])
        self.assertEqual(len(unload), 1)
        unload[0]()
        self.assertIsNone(hooks[0][1](session_id='test', parent_session_id='parent'))

if __name__ == '__main__':
    unittest.main()
