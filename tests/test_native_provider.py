"""Real Hermes loader + unpatched upstream; no network or live profile writes."""
import hashlib
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
from unittest.mock import patch
import test_plugin as fixture
import policy
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from plugins.memory import find_provider_dir, load_memory_provider
from hermes_cli.plugins import get_plugin_manager
from agent.memory_manager import MemoryManager


@unittest.skipUnless(os.environ.get('HINDSIGHT_UPSTREAM'), 'set HINDSIGHT_UPSTREAM to a clean official plugin checkout')
class NativeProviderTests(unittest.TestCase):
    def test_native_loader_initializes_adapter_over_clean_upstream(self):
        self.exercise_native()

    def test_native_loader_rejects_unapproved_bytecode_before_execution(self):
        self.exercise_native(tampered_cache=True)

    def test_normal_native_cache_does_not_break_subsequent_guard(self):
        self.exercise_native(create_cache=True)

    def exercise_native(self, *, tampered_cache=False, create_cache=False):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            home = Path(directory)
            upstream = Path(os.environ['HINDSIGHT_UPSTREAM'])
            native = home / 'plugins/hindsight'
            wrapper = home / 'plugins/hindsight-workspace'
            native.mkdir(parents=True)
            wrapper.mkdir()
            for name in ('__init__.py', 'settings.py', 'embedded.py', 'setup.py', 'templates.py', 'config_schema.py', 'plugin.yaml', 'pyproject.toml'):
                shutil.copy2(upstream / name, native / name)
            for name in ('__init__.py', 'policy.py', 'provider.py', 'hindsight_workspace.py', 'survey_route.py', 'plugin.yaml'):
                shutil.copy2(fixture.ROOT / name, wrapper / name)
            shutil.copytree(fixture.ROOT / 'maintenance', wrapper / 'maintenance')
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
                    self.assertTrue(any(Path(__import__('sys').modules[hook.__module__].__file__).parent == wrapper for hook in hooks), 'general native discovery did not register the project hook')
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
                    memory.initialize_all('synthetic-native-session', cwd='/synthetic/workspace', hermes_home=str(home), agent_identity='default', platform='cli')
                    self.assertIs(memory.get_provider('hindsight-workspace'), adapter)
                    identity = adapter.workspace_identity
                    self.assertEqual(adapter._delegate._bank_id, identity['bank'])
                    self.assertEqual(adapter._delegate._cwd, '/synthetic/workspace')
                    self.assertEqual(adapter._delegate._write_bank_ids, [identity['bank'], 'writable'])
                    self.assertEqual(adapter._delegate._build_recall_bank_ids(), [identity['bank'], 'writable', 'readonly'])
                    self.assertEqual(adapter._delegate._recall_max_input_chars, 2000)
                    if create_cache:
                        # get_code uses the real loader's cache writer without executing another module.
                        with patch.object(sys, 'dont_write_bytecode', False):
                            loader = importlib.machinery.SourceFileLoader('synthetic_cache', str(native / 'settings.py'))
                            self.assertIsNotNone(loader.get_code('synthetic_cache'))
                        self.assertTrue(list((native / '__pycache__').glob('*.pyc')))
                    self.assertEqual(adapter.get_tool_schemas(), adapter._delegate.get_tool_schemas())
                    self.assertEqual(Path(__import__('sys').modules[type(adapter._delegate).__module__].__file__).parent, native)
                    memory.shutdown_all()
                    manager.unload()
                after = {name: hashlib.sha256((native / name).read_bytes()).hexdigest() for name in before}
                self.assertEqual(before, after)
            finally:
                reset_hermes_home_override(token)
