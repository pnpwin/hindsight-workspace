"""Profile-gate regressions: no live API calls or production-state writes."""
import json
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
import test_plugin as fixture
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from agent.subagent_lifecycle import bind_subagent_parent


class ProfileTests(unittest.TestCase):
    provider: Any
    agent: Any
    ctx: Any
    p: Any
    home: Path
    settings: dict
    setUp = fixture.PluginTests.setUp
    capture = fixture.PluginTests.capture

    def test_named_profile_matches_native_provider(self):
        for name in ('designer', 'planner', 'primary', 'researcher', 'reviewer', 'verifier', 'worker', 'future-profile'):
            with self.subTest(profile=name):
                self.provider.bound_profile = name
                target = self.capture(profile=name)
                self.assertIsNotNone(target)
                self.assertEqual(target.bank, self.provider.workspace_identity['bank'])

    def test_missing_or_custom_profile_fails_closed(self):
        for name in ('', None, 'custom'):
            self.provider.bound_profile = name
            self.assertIsNone(self.capture(profile=name))

    def test_cross_profile_provider_rejected(self):
        self.provider.bound_profile = 'reviewer'
        self.assertIsNone(self.capture(profile='worker'))

    def test_same_workspace_and_policy_owner_share_ingestion_state(self):
        targets = []
        for name in ('worker', 'reviewer'):
            self.provider.bound_profile = name
            self.provider.bound_home = self.home / name
            (self.provider.bound_home / 'hindsight').mkdir(parents=True)
            (self.provider.bound_home / 'hindsight/config.json').write_text(json.dumps(self.settings))
            target = self.capture(profile=name, home=self.home / name)
            self.assertIsNotNone(target)
            targets.append(target)
        self.assertEqual(targets[0].bank, targets[1].bank)
        self.assertEqual(targets[0].state_root, targets[1].state_root)

    def test_hook_routes_named_profile_and_rejects_other_home(self):
        hooks, unload, captured, receipts = [], [], [], []
        home = self.home / 'worker'
        (home / 'hindsight').mkdir(parents=True)
        (home / 'hindsight/config.json').write_text(json.dumps(self.settings))
        self.provider.bound_profile = 'worker'
        self.provider.bound_home = home
        self.ctx._manager = types.SimpleNamespace(home_path=str(home))
        self.ctx.on_unload = unload.append
        self.ctx.register_hook = lambda name, fn: hooks.append(fn)
        self.ctx.register_memory_provider = lambda provider: None
        self.ctx.register_auxiliary_task = lambda *args, **kwargs: None
        config = types.ModuleType('hermes_cli.config')
        setattr(config, 'load_config_readonly', lambda: {'memory': {'provider': 'hindsight-workspace'}})
        profiles = types.ModuleType('hermes_cli.profiles')
        setattr(profiles, 'get_active_profile_name', lambda: 'worker')
        token = set_hermes_home_override(home)
        try:
            with patch.dict(sys.modules, {'hermes_cli.config': config, 'hermes_cli.profiles': profiles}), patch.object(self.p.Worker, 'submit', side_effect=lambda target: captured.append(target) or True), patch.object(self.p, 'atomic_json', side_effect=lambda path, value: receipts.append((path, value))):
                self.p.register(self.ctx)
                with bind_subagent_parent(self.agent):
                    hooks[0](session_id=self.agent.session_id)
                    self.assertEqual(len(captured), 1)
                    self.assertEqual(captured[0].data_dir, self.home / 'plugin-data/hindsight-workspace')
                    self.assertEqual(receipts[0][0].parent, home / 'plugin-data/hindsight-workspace')
                    self.ctx._manager.home_path = str(self.home / 'reviewer')
                    hooks[0](session_id=self.agent.session_id)
                    self.assertEqual(len(captured), 1)
        finally:
            reset_hermes_home_override(token)
            for close in unload:
                close()

    def test_worker_inherits_captured_profile_home(self):
        seen = []
        worker = self.p.Worker(sync=lambda target, **kwargs: seen.append(get_hermes_home()) or {'status': 'ok'})
        self.addCleanup(worker.close)
        target = self.capture()
        token = set_hermes_home_override(self.home / 'worker')
        try:
            self.assertTrue(worker.submit(target))
        finally:
            reset_hermes_home_override(token)
        self.assertTrue(worker.wait(5))
        self.assertEqual(seen, [self.home / 'worker'])
