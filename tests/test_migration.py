"""Migration is a guarded file transaction, never a database/environment rollback."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class MigrationTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'maintenance/migrate_shared.py'
        self.assertTrue(path.exists(), 'migration dry-run/apply boundary missing')
        spec = importlib.util.spec_from_file_location('migration_test_module', path)
        assert spec is not None and spec.loader is not None
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.target = self.home / 'hindsight/config.json'
        self.target.parent.mkdir()
        self.target.write_text('{"old":true}')
        self.other = self.home / 'hindsight-workspace/config.json'
        self.protected = self.home / 'plugins/.install-metadata.json'
        self.protected.parent.mkdir()
        self.protected.write_text('{"revision":"reviewed"}')
        self.source = self.root / 'staged.json'
        self.source.write_text('{"new":true}')
        self.proposal = {'roots': [str(self.home)], 'backup_root': str(self.root / 'backup'),
            'protected': {str(self.protected): self.m.digest(self.protected)},
            'changes': [{'target': str(p), 'source': str(self.source), 'before': self.m.digest(p)}
                        for p in (self.target, self.other)]}

    def test_plan_is_readonly_and_apply_checks_exact_payload(self):
        before = self.target.read_bytes()
        plan = self.m.plan(self.proposal)
        self.assertFalse((self.root / 'backup').exists())
        self.assertEqual(self.target.read_bytes(), before)
        self.assertNotIn('"new":true', json.dumps(plan))
        receipt = self.m.apply(plan)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.other.read_bytes(), self.source.read_bytes())
        self.assertEqual(Path(receipt['backups'][str(self.target)]).read_bytes(), before)
        self.assertEqual(self.protected.read_text(), '{"revision":"reviewed"}')
        self.m.rollback(receipt)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(self.other.exists())

    def test_drift_before_or_after_backup_cannot_publish(self):
        plan = self.m.plan(self.proposal)
        self.protected.write_text('{"revision":"foreign"}')
        with self.assertRaises(ValueError):
            self.m.apply(plan)
        self.assertEqual(self.target.read_text(), '{"old":true}')
        self.assertFalse(self.other.exists())
        self.protected.write_text('{"revision":"reviewed"}')
        original = self.m.backup
        def concurrent(p):
            result = original(p)
            self.target.write_text('{"concurrent":true}')
            return result
        with patch.object(self.m, 'backup', side_effect=concurrent), self.assertRaises(ValueError):
            self.m.apply(plan)
        self.assertEqual(self.target.read_text(), '{"concurrent":true}')
        self.assertFalse(self.other.exists())

    def test_rollback_preserves_newer_writes_and_rejects_symlinks(self):
        receipt = self.m.apply(self.m.plan(self.proposal))
        self.target.write_text('{"newer-confirmed":true}')
        with self.assertRaises(ValueError):
            self.m.rollback(receipt)
        self.assertEqual(self.target.read_text(), '{"newer-confirmed":true}')
        self.assertTrue(self.other.exists())
        link = self.home / 'hindsight/foreign.json'
        link.symlink_to(self.source)
        self.proposal['changes'][0]['target'] = str(link)
        with self.assertRaises(ValueError):
            self.m.plan(self.proposal)

    def test_rollback_rechecks_target_scope_and_restores_original_permissions(self):
        import copy
        self.target.chmod(0o640)
        receipt = self.m.apply(self.m.plan(self.proposal))
        foreign = self.root / 'foreign.json'
        foreign.write_bytes(self.source.read_bytes())
        forged = copy.deepcopy(receipt)
        forged['changes'][0]['target'] = str(foreign)
        forged['backups'][str(foreign)] = receipt['backups'][str(self.target)]
        with self.assertRaises(ValueError):
            self.m.rollback(forged)
        self.assertEqual(foreign.read_bytes(), self.source.read_bytes())
        self.m.rollback(receipt)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)

    def test_rollback_missing_backup_or_mode_cannot_change_any_target(self):
        import copy
        receipt = self.m.apply(self.m.plan(self.proposal))
        for field in ('backup', 'mode'):
            broken = copy.deepcopy(receipt)
            if field == 'backup':
                del broken['backups'][str(self.target)]
            else:
                del broken['changes'][0]['before_mode']
            try:
                self.m.rollback(broken)
            except (KeyError, ValueError, TypeError):
                pass
            self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
            self.assertTrue(self.other.exists(), 'invalid restore plan deleted a target')
            self.assertEqual(self.other.read_bytes(), self.source.read_bytes())

    def test_rollback_rechecks_each_target_after_first_restore(self):
        self.other.parent.mkdir()
        self.other.write_text('{"other-old":true}')
        self.proposal['changes'][1]['before'] = self.m.digest(self.other)
        receipt = self.m.apply(self.m.plan(self.proposal))
        original = self.m.write
        def concurrent(path, *args, **kwargs):
            original(path, *args, **kwargs)
            if Path(path) == self.other:
                self.target.write_text('{"concurrent":true}')
        with patch.object(self.m, 'write', side_effect=concurrent):
            try:
                self.m.rollback(receipt)
            except ValueError:
                pass
        self.assertEqual(self.target.read_text(), '{"concurrent":true}')

    def test_apply_and_rollback_share_scope_lock(self):
        self.assertTrue(callable(getattr(self.m, 'mutation_scope', None)), 'migration mutation lock missing')
        plan = self.m.plan(self.proposal)
        with self.m.mutation_scope(plan), self.assertRaises(BlockingIOError):
            self.m.apply(plan)
        self.assertFalse(self.other.exists())

    def test_database_secret_and_outside_paths_are_not_migration_targets(self):
        for value in (self.home / 'state.db', self.home / '.env', self.root / 'foreign.json'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.m.plan({**self.proposal, 'changes': [{'target': str(value), 'source': str(self.source), 'before': None}]})
