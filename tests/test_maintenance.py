"""The same clean-source gate serves monitors, manual updates and fleet dispatch."""
import importlib.util
import json
import os
import py_compile
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'maintenance/clean_gate.py'
        self.assertTrue(path.exists(), 'clean maintenance gate missing')
        spec = importlib.util.spec_from_file_location('clean_gate_probe', path)
        assert spec is not None and spec.loader is not None
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.provider = self.home / 'plugins/hindsight'
        shutil.copytree(Path(os.environ['HINDSIGHT_UPSTREAM']), self.provider)
        self.adapter = self.home / 'plugins/hindsight-workspace'
        self.adapter.mkdir()
        for name in ('__init__.py', 'provider.py', 'policy.py', 'hindsight_workspace.py', 'survey_route.py', 'cli.py', 'codex_memory.py', 'plugin.yaml'):
            shutil.copyfile(ROOT / name, self.adapter / name)
        (self.home / 'hindsight').mkdir()
        (self.home / 'hindsight/config.json').write_text('{}')
        (self.home / 'hindsight-workspace').mkdir()
        (self.home / 'hindsight-workspace/config.json').write_text(json.dumps({'policy_path': str(self.home / 'hindsight/config.json')}))
        (self.home / 'config.yaml').write_text('{}')
        metadata = {'hindsight': {'source': self.m.SOURCE, 'revision': self.m.REVISIONS[0], 'pinned': True}}
        (self.home / 'plugins/.install-metadata.json').write_text(json.dumps(metadata))
        self.approved = self.m.snapshot(self.home)

    def test_monitor_fleet_and_manual_share_purity_gate(self):
        calls = []
        self.assertEqual(self.m.monitor(self.home, self.approved)['status'], 'ok')
        result = self.m.dispatch(self.home, self.approved, self.provider, publisher=lambda p: calls.append(p), scheduled=True)
        self.assertEqual(result['status'], 'held-pinned')
        self.assertEqual(calls, [])
        result = self.m.dispatch(self.home, self.approved, self.provider, publisher=lambda p: calls.append(p), scheduled=False)
        self.assertEqual(calls, [self.provider])
        self.assertFalse(result['changed'], 'a no-op publisher did not change approved state')
        self.assertFalse((self.provider / 'workspace_bank.py').exists())

    def test_unknown_pin_source_policy_adapter_and_overlay_refuse_before_publisher(self):
        mutations = [(self.provider / '__init__.py', b'\n# drift'),
                     (self.provider / 'workspace_bank.py', b'legacy'),
                     (self.adapter / 'provider.py', b'\n# drift'),
                     (self.home / 'hindsight/config.json', b' '),
                     (self.home / 'plugins/.install-metadata.json', b' ')]
        for path, suffix in mutations:
            with self.subTest(path=path):
                old = path.read_bytes() if path.exists() else None
                path.write_bytes((old or b'') + suffix)
                calls = []
                try:
                    with self.assertRaises(ValueError):
                        self.m.dispatch(self.home, self.approved, self.provider, publisher=lambda p: calls.append(p), scheduled=False)
                    self.assertEqual(calls, [])
                finally:
                    if old is None:
                        path.unlink()
                    else:
                        path.write_bytes(old)

    def test_unchecked_bytecode_cannot_replace_approved_source(self):
        source = self.provider / 'settings.py'
        approved = source.read_bytes()
        source.write_text('SYNTHETIC_UNAPPROVED_CODE = True\n')
        py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        source.write_bytes(approved)
        with self.assertRaisesRegex(ValueError, 'bytecode'):
            self.m.monitor(self.home, self.approved)

    def test_legitimate_bytecode_keeps_existing_approval_valid(self):
        subprocess.run([sys.executable, '-I', '-B', '-c',
                        'import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)',
                        str(self.provider / 'settings.py')], check=True, timeout=20)
        self.assertEqual(self.m.monitor(self.home, self.approved)['status'], 'ok')
        for mode in py_compile.PycInvalidationMode:
            for optimize in (0, 1, 2):
                with self.subTest(mode=mode, optimize=optimize):
                    py_compile.compile(str(self.provider / 'settings.py'), doraise=True,
                                       optimize=optimize, invalidation_mode=mode)
                    self.assertEqual(self.m.monitor(self.home, self.approved)['status'], 'ok')

    def test_external_cache_prefix_refuses_uninspected_executable_inputs(self):
        with patch.object(sys, 'pycache_prefix', str(self.home / 'external-cache')):
            with self.assertRaisesRegex(ValueError, 'cache prefix'):
                self.m.verify_provider(self.provider)

    def test_explicit_external_consumer_roster_is_bound(self):
        import inspect
        self.assertIn('consumers', inspect.signature(self.m.snapshot).parameters)
        consumer = self.home / 'codex/config.toml'
        consumer.parent.mkdir()
        consumer.write_text('# synthetic consumer')
        approved = self.m.snapshot(self.home, consumers=[consumer])
        self.m.monitor(self.home, approved)
        consumer.write_text('# changed consumer')
        with self.assertRaises(ValueError):
            self.m.monitor(self.home, approved)

    def test_nested_shadow_package_is_not_approved(self):
        shadow = self.provider / 'settings/__init__.py'
        shadow.parent.mkdir()
        shadow.write_text('raise RuntimeError("unreviewed shadow")')
        calls = []
        with self.assertRaises(ValueError):
            self.m.dispatch(self.home, self.approved, self.provider, publisher=lambda p: calls.append(p), scheduled=False)
        self.assertEqual(calls, [])

    def test_owner_pointer_and_auxiliary_route_are_approval_inputs(self):
        selector = self.home / 'hindsight-workspace/config.json'
        original = selector.read_bytes()
        other = self.home / 'other/hindsight/config.json'
        other.parent.mkdir(parents=True)
        other.write_text('{}')
        selector.write_text(json.dumps({'policy_path': str(other)}))
        with self.assertRaises(ValueError):
            self.m.monitor(self.home, self.approved)
        selector.write_bytes(original)
        (self.home / 'config.yaml').write_text('{"auxiliary":{"hindsight_workspace_survey":{"model":"different"}}}')
        with self.assertRaises(ValueError):
            self.m.monitor(self.home, self.approved)
