"""Qualify the real native plugin context and task trust gate without model calls."""
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_plugin as fixture
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override


class SurveyRouteTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((fixture.ROOT / 'survey_route.py').exists(), 'shared native survey route missing')
        self.route = importlib.import_module('survey_route')
        temporary = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.owner = self.home / 'hindsight/config.json'
        self.owner.parent.mkdir()
        self.owner.write_text('{}')
        self.installed = self.home / 'plugins/hindsight-workspace'
        self.installed.mkdir(parents=True)
        for name in ('__init__.py', 'policy.py', 'provider.py', 'hindsight_workspace.py', 'survey_route.py', 'plugin.yaml'):
            shutil.copy2(fixture.ROOT / name, self.installed / name)
        self.aux = {'provider': 'openai', 'model': 'synthetic-survey', 'timeout': 300,
                    'extra_body': {'reasoning_effort': 'low'},
                    'fallback_chain': [{'provider': 'openai', 'model': 'synthetic-fallback'}]}
        self.config = {'model': {'provider': 'openai', 'default': 'wrong-main'},
            'plugins': {'enabled': ['hindsight-workspace'], 'disabled': []},
            'auxiliary': {'hindsight_workspace_survey': self.aux}}
        self.save_config()
        (self.home / '.env').write_text('SYNTHETIC_OWNER=owner\n')
        self.calls = []
        def completion(**kwargs):
            from agent.auxiliary_task_config import _get_auxiliary_task_config
            from agent.secret_scope import get_secret
            effective = _get_auxiliary_task_config(kwargs['task'])
            self.calls.append((kwargs, effective, get_hermes_home(), get_secret('SYNTHETIC_OWNER')))
            kwargs['route_info'].update(provider=effective['provider'], model=effective['model'])
            return SimpleNamespace(model=effective['model'], choices=[SimpleNamespace(message=SimpleNamespace(content='Synthetic bounded project map from the native facade.'))])
        self.enterContext(patch('agent.auxiliary_client.call_llm', side_effect=completion))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network forbidden')))

    def save_config(self):
        (self.home / 'config.yaml').write_text(json.dumps(self.config))

    def test_native_context_uses_owned_task_and_owner_route_without_overrides(self):
        token = set_hermes_home_override(self.home / 'foreign-profile')
        try:
            text = self.route.summarize(self.owner, 'bounded source marker')
            self.assertIn('native facade', text)
            self.assertEqual(get_hermes_home(), self.home / 'foreign-profile')
        finally:
            reset_hermes_home_override(token)
        kwargs, effective, home, secret = self.calls[0]
        self.assertEqual(home, self.home)
        self.assertEqual(secret, 'owner')
        self.assertEqual(kwargs['task'], 'hindsight_workspace_survey')
        self.assertEqual(kwargs['max_tokens'], 1600)
        self.assertEqual(kwargs['timeout'], 300)
        self.assertNotIn('tools', kwargs)
        for key in ('model', 'provider'):
            self.assertIsNone(kwargs.get(key))
            self.assertEqual(effective[key], self.aux[key])
        self.assertEqual(effective['extra_body'], self.aux['extra_body'])
        self.assertEqual(effective['fallback_chain'], self.aux['fallback_chain'])
        self.assertIn('bounded source marker', kwargs['messages'][1]['content'])

    def test_unconfigured_auxiliary_does_not_inherit_different_main_models(self):
        del self.config['auxiliary']
        self.save_config()
        with self.assertRaises(ValueError):
            self.route.summarize(self.owner, 'must not run')
        self.assertEqual(self.calls, [])

    def test_disabled_plugin_cannot_create_its_own_trust(self):
        self.config['plugins'] = {'enabled': [], 'disabled': ['hindsight-workspace']}
        self.save_config()
        with self.assertRaises((ValueError, PermissionError)):
            self.route.summarize(self.owner, 'must not run')
        self.assertEqual(self.calls, [])
