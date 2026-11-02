"""Shared ingestion ownership and real inter-process locking with synthetic I/O."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from hindsight_workspace import policy
import test_workspace as fixture


class SharedIngestionTests(unittest.TestCase):
    def setUp(self):
        base = fixture.WorkspaceTests('test_non_git_survey')
        self.addCleanup(base.doCleanups)
        base.setUp()
        self.root, self.repo, self.api = base.root, base.repo, base.api
        self.route = importlib.import_module('hindsight_workspace.survey_route')
        self.assertTrue(callable(getattr(self.route, 'build_target', None)), 'common target builder missing')
        self.owner = self.root / 'owner/hindsight/config.json'
        self.owner.parent.mkdir(parents=True)
        self.p = policy.normalize_policy({'api_url': self.api.url, 'retain_strategy': 'approved', 'retain_tags': ['shared']})
        self.owner.write_text(json.dumps(self.p))

    def identity(self, sid):
        return policy.resolve_identity(session_id=sid, cwd=str(self.repo), selected_cwd=str(self.repo), policy=self.p)

    def test_hermes_then_codex_share_one_survey_and_policy(self):
        models = []
        first = self.route.build_target(self.owner, self.identity('hermes-a'))
        second = self.route.build_target(self.owner, self.identity('codex-b'))
        for target in (first, second):
            target.summarize = lambda source: models.append(source) or '# Synthetic bounded project map.'
        self.assertEqual(first.state_root, second.state_root)
        self.assertEqual(fixture.w.sync_workspace(first)['survey'], 'updated')
        self.assertEqual(fixture.w.sync_workspace(second)['survey'], 'unchanged')
        self.assertEqual(len(models), 1)
        self.assertEqual(len(self.api.calls), 1)
        item = self.api.calls[0][1]['items'][0]
        self.assertEqual(item['strategy'], 'approved')
        self.assertEqual(item['tags'], ['shared', 'hermes-workspace'])
        self.assertEqual(item['document_id'], 'hermes-workspace-survey')

    def test_codex_sync_uses_native_route_then_hermes_is_noop(self):
        import shutil
        from types import SimpleNamespace
        from unittest.mock import patch
        from hindsight_workspace import cli
        from hindsight_workspace.codex_memory import Session
        home = self.owner.parent.parent
        installed = home / 'plugins/hindsight-workspace'
        installed.mkdir(parents=True)
        for name in ('__init__.py', 'plugin.yaml'):
            shutil.copy2(Path(__file__).resolve().parents[1] / name, installed / name)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'src', installed / 'src')
        (home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['hindsight-workspace'], 'disabled': []},
            'auxiliary': {'hindsight_workspace_survey': {'provider': 'openai', 'model': 'synthetic-survey'}}}))
        with Session(self.owner, 'codex-c').locked() as session:
            session.bind(str(self.repo))
        def completion(**kw):
            self.assertEqual(kw['task'], 'hindsight_workspace_survey')
            self.assertNotIn('tools', kw)
            return SimpleNamespace(model='synthetic-survey', choices=[SimpleNamespace(message=SimpleNamespace(content='# Synthetic native map.'))])
        with patch('agent.auxiliary_client.call_llm', side_effect=completion) as model:
            result = cli.dispatch('sync', {'session_id': 'codex-c', 'cwd': str(self.repo)}, self.owner)
            self.assertEqual(result['survey'], 'updated')
            other = self.route.build_target(self.owner, self.identity('hermes-d'))
            self.assertEqual(fixture.w.sync_workspace(other)['survey'], 'unchanged')
            self.assertEqual(model.call_count, 1)
        self.assertEqual(len(self.api.calls), 1)

    def test_policy_change_refuses_before_model_or_memory_io(self):
        target = self.route.build_target(self.owner, self.identity('hermes-a'))
        models = []
        target.summarize = lambda source: models.append(source) or 'Synthetic map.'
        self.owner.write_text(json.dumps({**self.p, 'recall_budget': 'high'}))
        with self.assertRaises(ValueError):
            fixture.w.sync_workspace(target)
        self.assertEqual(models, [])
        self.assertEqual(self.api.calls, [])

    def test_processes_share_lock_and_only_one_model_call(self):
        counter = self.root / 'model-calls'
        code = '''import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['HERMES_SOURCE'])
sys.path.insert(0, sys.argv[1])
from hindsight_workspace.survey_route import build_target
from hindsight_workspace.policy import load_policy, resolve_identity
from hindsight_workspace.hindsight_workspace import sync_workspace
owner, cwd, counter = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
p = load_policy(owner)
target = build_target(owner, resolve_identity(session_id=sys.argv[5], cwd=cwd, selected_cwd=cwd, policy=p))
def summarize(source):
    import time
    with counter.open('a') as stream: stream.write('model\\n')
    time.sleep(.2)
    return '# Synthetic no-tools project map.'
target.summarize = summarize
print(json.dumps(sync_workspace(target)))
'''
        command = [sys.executable, '-B', '-c', code, str(Path(__file__).resolve().parents[1] / 'src'), str(self.owner), str(self.repo), str(counter)]
        children = [subprocess.Popen(command + [sid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for sid in ('a', 'b')]
        try:
            for child in children:
                stdout, stderr = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertIn(json.loads(stdout)['status'], {'ok', 'busy'})
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        self.assertEqual(counter.read_text().splitlines(), ['model'])
        self.assertEqual(len(self.api.calls), 1)
