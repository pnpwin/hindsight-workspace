"""Adopt only read-back-confirmed cursors; never delete legacy receipts."""
import dataclasses
import importlib
import json
from pathlib import Path
import unittest
import uuid
import test_workspace as fixture


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        base = fixture.WorkspaceTests('test_non_git_survey')
        self.addCleanup(base.doCleanups)
        base.setUp()
        self.base = base
        self.assertTrue((Path(__file__).resolve().parents[1] / 'adoption.py').exists(), 'safe adoption missing')
        self.adopt = importlib.import_module('adoption').adopt
        fixture.w.sync_workspace(base.target)
        self.target = dataclasses.replace(base.target, data_dir=base.root / 'shared')
        self.roots = [base.target.state_root]

    def files(self):
        return {str(p): p.read_bytes() for p in self.base.root.rglob('*') if p.is_file()}

    def test_dry_run_then_adoption_is_noop_for_next_ingestion(self):
        before = self.files()
        plan = self.adopt(self.target, self.roots)
        self.assertTrue(plan['ready'])
        self.assertEqual(self.files(), before)
        result = self.adopt(self.target, self.roots, apply=True, approved_plan=plan)
        self.assertTrue(result['applied'])
        for path, content in before.items():
            self.assertEqual(Path(path).read_bytes(), content)
        self.target.summarize = lambda _: self.fail('adoption repeated a completed survey')
        self.assertEqual(fixture.w.sync_workspace(self.target)['survey'], 'unchanged')
        self.assertEqual(len(self.base.api.calls), 1)

    def test_pending_unknown_operation_is_preserved_and_blocks_apply(self):
        pending = self.roots[0] / 'hermes-workspace-survey-pending.json'
        pending.write_text(json.dumps({'operation_id': str(uuid.uuid4()), 'content': 'must survive'}))
        before = self.files()
        plan = self.adopt(self.target, self.roots)
        self.assertFalse(plan['ready'])
        self.assertTrue(plan['blockers'])
        with self.assertRaises(ValueError):
            self.adopt(self.target, self.roots, apply=True, approved_plan=plan)
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.base.api.calls), 1)

    def test_readback_and_concurrent_source_change_refuse(self):
        plan = self.adopt(self.target, self.roots)
        path = self.roots[0] / 'state.json'
        state = json.loads(path.read_text())
        state['checked_at'] = 'concurrent-writer'
        path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            self.adopt(self.target, self.roots, apply=True, approved_plan=plan)
        self.base.api.bad_readback = True
        self.assertFalse(self.adopt(self.target, self.roots)['ready'])
        self.assertFalse((self.target.state_root / 'state.json').exists())
        self.assertEqual(len(self.base.api.calls), 1)
