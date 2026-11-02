"""Real Hermes loader + unpatched upstream; no network or live profile writes."""
import hashlib
from contextlib import ExitStack
import importlib.machinery
import json
import os
import py_compile
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_plugin as fixture
from hindsight_workspace import policy
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from plugins.memory import find_provider_dir, load_memory_provider
from hermes_cli.plugins import get_plugin_manager
from agent.memory_manager import MemoryManager


@unittest.skipUnless(os.environ.get('HINDSIGHT_UPSTREAM'), 'set HINDSIGHT_UPSTREAM to a clean official plugin checkout')
class NativeProviderTests(unittest.TestCase):
    def test_gateway_context_supplies_omitted_constructor_cwd(self):
        self.exercise_native(native_context=True)

    def test_gateway_context_guards_explicit_constructor_cwd(self):
        self.exercise_native(native_context=True, explicit_cwd=True)

    def test_native_loader_initializes_adapter_over_clean_upstream(self):
        self.exercise_native()

    def test_native_loader_rejects_unapproved_bytecode_before_execution(self):
        self.exercise_native(tampered_cache=True)

    def test_normal_native_cache_does_not_break_subsequent_guard(self):
        self.exercise_native(create_cache=True)

    def exercise_native(self, *, tampered_cache=False, create_cache=False, native_context=False, explicit_cwd=False):
        temp_root = Path(os.environ['TMPDIR'])
        if native_context:
            temp_root = Path.home() / '.hermes/profiles'
            temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root, prefix='acceptance-' if native_context else 'acceptance with spaces-') as directory:
            home = Path(directory)
            upstream = Path(os.environ['HINDSIGHT_UPSTREAM'])
            native = home / 'plugins/hindsight'
            wrapper = home / 'plugins/hindsight-workspace'
            native.mkdir(parents=True)
            wrapper.mkdir()
            for name in ('__init__.py', 'settings.py', 'embedded.py', 'setup.py', 'templates.py', 'config_schema.py', 'plugin.yaml', 'pyproject.toml'):
                shutil.copy2(upstream / name, native / name)
            for name in ('__init__.py', 'plugin.yaml'):
                shutil.copy2(fixture.ROOT / name, wrapper / name)
            shutil.copytree(fixture.ROOT / 'src', wrapper / 'src')
            self.assertNotIn('from .workspace_bank import', (native / '__init__.py').read_text())
            self.assertFalse((native / 'workspace_bank.py').exists())
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in native.iterdir()}
            marker = home / 'unapproved-code-ran'
            if tampered_cache:
                source = native / '__init__.py'
                approved = source.read_bytes()
                source.write_bytes(approved + f'\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n'.encode())
                py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
                source.write_bytes(approved)
            settings = policy.normalize_policy({'additional_banks': ['writable'], 'recall_additional_banks': ['readonly']})
            (home / 'hindsight').mkdir()
            owner = home / 'hindsight/config.json'
            owner.write_text(json.dumps(settings))
            (home / 'config.yaml').write_text(json.dumps({'memory': {'provider': 'hindsight-workspace'}, 'plugins': {'enabled': ['hindsight-workspace'], 'disabled': []}}))
            (home / 'hindsight-workspace').mkdir()
            (home / 'hindsight-workspace/config.json').write_text(json.dumps({'policy_path': str(owner)}))
            token = set_hermes_home_override(home)
            scope = ExitStack()
            try:
                with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network forbidden')):
                    with self.assertNoLogs('plugins.memory', level='WARNING'):
                        adapter = load_memory_provider('hindsight-workspace', register_skills=False)
                    self.assertIsNotNone(adapter, 'real native loader did not discover the adapter')
                    assert adapter is not None
                    self.assertEqual(adapter.name, 'hindsight-workspace')
                    manager = get_plugin_manager()
                    manager.discover_and_load()
                    hooks = manager._hooks.get('pre_llm_call', [])
                    self.assertTrue(any(Path(__import__('sys').modules[hook.__module__].__file__).parent == wrapper / 'src/hindsight_workspace' for hook in hooks), 'general native discovery did not register the project hook')
                    self.assertEqual(find_provider_dir('hindsight'), native)
                    if tampered_cache:
                        try:
                            with self.assertRaisesRegex(ValueError, 'bytecode'):
                                adapter.initialize('synthetic-native-session', cwd='/synthetic/workspace', hermes_home=str(home), agent_identity='default')
                        finally:
                            manager.unload()
                        self.assertFalse(marker.exists(), 'unapproved bytecode executed before refusal')
                        self.assertEqual(before, {name: hashlib.sha256((native / name).read_bytes()).hexdigest() for name in before})
                        return
                    memory = MemoryManager()
                    memory.add_provider(adapter)
                    cwd = '/synthetic/workspace'
                    if native_context:
                        from agent.agent_init import _memory_provider_init_kwargs
                        from agent.agent_init_params import _GATEWAY_IDENTITY_PARAMS
                        from gateway.session_context import scoped_session_key
                        from gateway.session_policy import LocalSessionPolicy, policy_scope
                        cwd = str(home / 'selected project')
                        Path(cwd).mkdir()
                        route = 'agent:main:local:synthetic-native-session'
                        launch = LocalSessionPolicy(source='desktop', platform='local', cwd=cwd,
                            model=None, toolsets=(), config_json='{}', request_json='{}',
                            terminal_json='{"TERMINAL_ENV":"local"}')
                        scope.enter_context(policy_scope(launch))
                        scope.enter_context(scoped_session_key(route))
                        agent = SimpleNamespace(session_id='synthetic-native-session', session_cwd=cwd if explicit_cwd else None,
                            _session_db=None, _emit_warning=lambda *a: None, _emit_status=lambda *a: None,
                            **{'_' + key: route if key == 'gateway_session_key' else ''
                               for key in _GATEWAY_IDENTITY_PARAMS})
                        kwargs = _memory_provider_init_kwargs(agent, 'cli')
                        if explicit_cwd:
                            self.assertEqual(kwargs['cwd'], cwd)
                        else:
                            self.assertNotIn('cwd', kwargs, 'native constructor boundary no longer reproduces the defect')
                        memory.initialize_all(**kwargs)
                        self.assertEqual(agent.session_cwd, cwd if explicit_cwd else None,
                                         'adapter must not mutate the Hermes agent')
                    else:
                        memory.initialize_all('synthetic-native-session', cwd=cwd, hermes_home=str(home), agent_identity='default', platform='cli')
                    self.assertIs(memory.get_provider('hindsight-workspace'), adapter)
                    identity = adapter.workspace_identity
                    self.assertIsNotNone(identity, 'native MemoryManager swallowed an initialization refusal')
                    self.assertEqual(adapter._delegate._bank_id, identity['bank'])
                    self.assertEqual(adapter._delegate._cwd, cwd)
                    self.assertEqual(adapter._delegate._write_bank_ids, [identity['bank'], 'writable'])
                    self.assertEqual(adapter._delegate._build_recall_bank_ids(), [identity['bank'], 'writable', 'readonly'])
                    self.assertEqual(adapter._delegate._recall_max_input_chars, 2000)
                    if native_context:
                        from hermes_cli.plugins import PluginContext
                        loaded = manager._plugins['hindsight-workspace']
                        ctx = PluginContext(loaded.manifest, manager)
                        agent._memory_manager = memory
                        def capture():
                            runtime = sys.modules[loaded.module.register.__module__]
                            return runtime.capture_target(ctx=ctx, agent=agent,
                                session_id=agent.session_id, profile=kwargs['agent_identity'],
                                home=home, settings=settings, redact=lambda text: text)
                        target = capture()
                        self.assertIsNotNone(target, 'native project hook skipped the context-bound provider')
                        self.assertEqual(target.cwd, Path(cwd))
                        self.assertEqual(target.bank, identity['bank'])
                        with scoped_session_key('agent:main:local:another-session'):
                            self.assertIsNone(capture(), 'another route reused the bound collector')
                    if create_cache:
                        # get_code uses the real loader's cache writer without executing another module.
                        with patch.object(sys, 'dont_write_bytecode', False):
                            loader = importlib.machinery.SourceFileLoader('synthetic_cache', str(native / 'settings.py'))
                            self.assertIsNotNone(loader.get_code('synthetic_cache'))
                        self.assertTrue(list((native / '__pycache__').glob('*.pyc')))
                    self.assertEqual(adapter.get_tool_schemas(), adapter._delegate.get_tool_schemas())
                    self.assertEqual(Path(__import__('sys').modules[type(adapter._delegate).__module__].__file__).parent, native)
                    if native_context:
                        scope.close()
                        with self.assertNoLogs('agent.memory_manager', level='WARNING'):
                            memory.on_session_end([{'role': 'user', 'content': 'synthetic evicted session'}])
                    memory.shutdown_all()
                    manager.unload()
                after = {name: hashlib.sha256((native / name).read_bytes()).hexdigest() for name in before}
                self.assertEqual(before, after)
            finally:
                scope.close()
                reset_hermes_home_override(token)
