"""The same clean-source gate serves monitors, manual updates and fleet dispatch."""
import importlib
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
        path = ROOT / 'src/hindsight_workspace/maintenance/clean_gate.py'
        self.assertTrue(path.exists(), 'clean maintenance gate missing')
        self.m = importlib.import_module('hindsight_workspace.maintenance.clean_gate')
        tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.provider = self.home / 'plugins/hindsight'
        shutil.copytree(Path(os.environ['HINDSIGHT_UPSTREAM']), self.provider)
        self.adapter = self.home / 'plugins/hindsight-workspace'
        self.adapter.mkdir()
        for name in ('__init__.py', 'plugin.yaml'):
            shutil.copyfile(ROOT / name, self.adapter / name)
        shutil.copytree(ROOT / 'src', self.adapter / 'src')
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
                     (self.adapter / 'src/hindsight_workspace/provider.py', b'\n# drift'),
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

    @unittest.skipUnless(os.environ.get('HINDSIGHT_PREVIOUS_UPSTREAM'), 'previous clean provider not supplied')
    def test_previous_clean_revision_remains_accepted(self):
        shutil.rmtree(self.provider)
        shutil.copytree(os.environ['HINDSIGHT_PREVIOUS_UPSTREAM'], self.provider,
                        ignore=shutil.ignore_patterns('__pycache__', '.git', '.pytest_cache'))
        path = self.home / 'plugins/.install-metadata.json'
        metadata = json.loads(path.read_text())
        metadata['hindsight']['revision'] = 'a5a619b5ca59c5c39e0f96be6f7a31fabd136bd5'
        path.write_text(json.dumps(metadata))
        self.m.snapshot(self.home)
        py_compile.compile(str(self.provider / '__init__.py'), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.m.snapshot(self.home)
        for revision in ('34a138684a899b6ec34175f7a2b633860466d289',
                         '2c00b59ae6bb07cad45c44ac19e14ddc369fbbeb',
                         '9486680b078420397ad1b4d5835c013808ac97f6'):
            with self.subTest(revision=revision):
                metadata['hindsight']['revision'] = revision
                path.write_text(json.dumps(metadata))
                with self.assertRaisesRegex(ValueError, 'Unknown upstream source'):
                    self.m.snapshot(self.home)

    def test_equal_source_main_revisions_remain_accepted(self):
        path = self.home / 'plugins/.install-metadata.json'
        metadata = json.loads(path.read_text())
        for revision in ('34a138684a899b6ec34175f7a2b633860466d289',
                         '2c00b59ae6bb07cad45c44ac19e14ddc369fbbeb',
                         '9486680b078420397ad1b4d5835c013808ac97f6'):
            with self.subTest(revision=revision):
                metadata['hindsight']['revision'] = revision
                path.write_text(json.dumps(metadata))
                try:
                    approved = self.m.snapshot(self.home)
                except ValueError as exc:
                    self.fail(f'Reviewed equal-source revision was refused: {exc}')
                self.assertEqual(approved['revision'], revision)
                self.assertEqual(self.m.monitor(self.home, approved)['status'], 'ok')

    def test_equal_sources_do_not_allow_unknown_revision(self):
        revision = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'Unknown upstream source'):
            self.m.verify_provider(self.provider, revision=revision)
        path = self.home / 'plugins/.install-metadata.json'
        metadata = json.loads(path.read_text())
        metadata['hindsight']['revision'] = revision
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'Unknown upstream source/pin'):
            self.m.snapshot(self.home)

    def test_latest_source_cannot_claim_previous_revision(self):
        path = self.home / 'plugins/.install-metadata.json'
        metadata = json.loads(path.read_text())
        metadata['hindsight']['revision'] = 'a5a619b5ca59c5c39e0f96be6f7a31fabd136bd5'
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'Unknown upstream source'):
            self.m.snapshot(self.home)

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

    def test_legitimate_cache_survives_reference_lifetime_changes(self):
        import gc
        import inspect
        verify = self.m.verify_bytecode
        lines, start = inspect.getsourcelines(verify)
        after_compile = start + next(i for i, line in enumerate(lines) if 'code = compile(' in line) + 1
        path = py_compile.compile(str(self.provider / 'settings.py'), doraise=True,
                                  optimize=1, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        assert path is not None
        for collect in (False, True):
            with self.subTest(collect=collect):
                fired = []
                def trace(frame, event, arg):
                    if frame.f_code is verify.__code__ and event == 'line' and frame.f_lineno >= after_compile and not fired:
                        fired.append(True)
                        if collect:
                            gc.collect()
                    return trace
                previous = sys.gettrace()
                try:
                    sys.settrace(trace)
                    try:
                        verify(self.provider, Path(path))
                    except ValueError as exc:
                        self.fail(f'Legitimate cache rejected after reference-lifetime change: {exc}')
                finally:
                    sys.settrace(previous)
                self.assertEqual(fired, [True])

    def test_marshal_references_do_not_change_fingerprint(self):
        compare = getattr(self.m, '_bytecode_fingerprint', None)
        assert callable(compare), 'bounded non-executing bytecode comparator missing'
        plain = b')\x02z\x01xz\x01x'
        shared = b')\x02\xfa\x01xr\x00\x00\x00\x00'
        extra_slot = b'\xa9\x02\xfa\x01xr\x01\x00\x00\x00'
        for left, right in ((shared, extra_slot), (extra_slot, shared), (plain, b'\xa9' + plain[1:])):
            self.assertEqual(compare(left), compare(right))
        self.assertNotEqual(compare(plain), compare(b')\x02z\x01xz\x01y'))
        code = compile("values=(None, True, False, ..., -1208925819614629174706176, 1.25, 2j, b'bytes', 'строка')\ndef outer(x):\n def inner():\n  try: return x in {'a','b'}\n  except Exception: return None\n return inner\n", '<synthetic>', 'exec')
        with patch.object(self.m.marshal, 'loads', side_effect=AssertionError('Never deserialize unknown cache')):
            altered = code.replace(co_consts=code.co_consts + ('changed',))
            self.assertNotEqual(compare(self.m.marshal.dumps(code)), compare(self.m.marshal.dumps(altered)))

    def test_cache_alias_graph_stays_bound(self):
        # Equal immutable values can still have observable identity differences.
        plain = b')\x02z\x01xz\x01x'
        aliased = b')\x02\xfa\x01xr\x00\x00\x00\x00'
        self.assertNotEqual(self.m._bytecode_fingerprint(plain), self.m._bytecode_fingerprint(aliased))

    def test_malformed_cache_encoding_is_bounded_and_refused(self):
        compare = getattr(self.m, '_bytecode_fingerprint', None)
        assert callable(compare), 'bounded non-executing bytecode comparator missing'
        invalid = [b'', b'Ntrailing', b'r\x00\x00\x00\x00', b'r\xff\xff\xff\xff',
                   b'\xa9\x01r\x00\x00\x00\x00', b's\xff\xff\xff\xff',
                   b'u\xff\xff\xff\x7f', b'(\xff\xff\xff\x7f', b'l\x00\x00\x00\x80',
                   b'{', b')\x01' * 201 + b'N',
                   b'a\x02\x00\x00\x00\xc3\xa9', b'z\x02\xc3\xa9',
                   b'u\x01\x00\x00\x00\xff', b't\x02\x00\x00\x00\xc0\x80']
        invalid += [b')\x02' + bytes([ord(tag) | 128]) + b'r\x00\x00\x00\x00' for tag in 'NFTS.']
        for data in invalid:
            with self.subTest(data=data[:20]):
                with self.assertRaises(ValueError):
                    compare(data)

    def test_cache_header_trailing_data_and_unknown_optimization_refuse(self):
        compiled = py_compile.compile(str(self.provider / 'settings.py'), doraise=True)
        assert compiled is not None
        path = Path(compiled)
        data = path.read_bytes()
        for invalid in (data + b'x', b'BAD!' + data[4:], data[:4] + b'\x02\x00\x00\x00' + data[8:], data[:16] + b'N'):
            with self.subTest(header=invalid[:16]):
                path.write_bytes(invalid)
                with self.assertRaises(ValueError):
                    self.m.verify_bytecode(self.provider, path)
        path.write_bytes(data)
        other = path.with_name(path.stem + '.opt-7.pyc')
        other.write_bytes(data)
        with self.assertRaises(ValueError):
            self.m.verify_bytecode(self.provider, other)

    def test_every_code_field_remains_bound(self):
        import types
        code = next(c for c in compile('def f(x): return x + 1', '<synthetic>', 'exec').co_consts
                    if isinstance(c, types.CodeType))
        fingerprint = self.m._bytecode_fingerprint
        baseline = fingerprint(self.m.marshal.dumps(code))
        changes = {'co_argcount': 0, 'co_posonlyargcount': 1, 'co_stacksize': code.co_stacksize + 1,
                   'co_flags': code.co_flags ^ 0x100, 'co_code': b'\x09\x00' + code.co_code[2:],
                   'co_consts': code.co_consts + ('different',), 'co_names': ('different',),
                   'co_varnames': ('different',), 'co_filename': '<different>', 'co_name': 'different',
                   'co_qualname': 'different', 'co_firstlineno': code.co_firstlineno + 1,
                   'co_linetable': b'', 'co_exceptiontable': b'\x00',
                   'co_freevars': ('different',), 'co_cellvars': ('different',)}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(baseline, fingerprint(self.m.marshal.dumps(code.replace(**{field: value}))))
        changed = code.replace(co_kwonlyargcount=1, co_nlocals=2, co_varnames=('x', 'kw'))
        self.assertNotEqual(baseline, fingerprint(self.m.marshal.dumps(changed)))

    def test_all_approved_modules_accept_native_cache(self):
        for name in self.m.UPSTREAM:
            if not name.endswith('.py'):
                continue
            for optimize in (0, 1, 2):
                with self.subTest(name=name, optimize=optimize):
                    compiled = py_compile.compile(str(self.provider / name), doraise=True, optimize=optimize,
                                                  invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
                    assert compiled is not None
                    self.m.verify_bytecode(self.provider, Path(compiled))
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
