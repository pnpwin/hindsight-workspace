"""The same fake HTTP endpoint sees native Hermes and actual Codex CLI requests."""
import copy
import json
import os
from pathlib import Path
import shutil
import unittest

import test_codex_contract as codex
import test_plugin  # native project path and package fixtures
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from plugins.memory import load_memory_provider


@unittest.skipUnless(os.environ.get('HINDSIGHT_UPSTREAM'), 'requires qualified clean upstream source')
class RequestParityTests(unittest.TestCase):
    def prepare(self, **overrides):
        f = codex.CodexContractTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.p.update(overrides)
        f.owner.write_text(json.dumps(f.p))
        native = f.home / 'plugins/hindsight'
        wrapper = f.home / 'plugins/hindsight-workspace'
        native.mkdir(parents=True)
        wrapper.mkdir()
        upstream = Path(os.environ['HINDSIGHT_UPSTREAM'])
        for name in ('__init__.py', 'settings.py', 'embedded.py', 'setup.py', 'templates.py', 'config_schema.py', 'plugin.yaml', 'pyproject.toml'):
            shutil.copy2(upstream / name, native / name)
        for name in ('__init__.py', 'plugin.yaml'):
            shutil.copy2(codex.ROOT / name, wrapper / name)
        shutil.copytree(codex.ROOT / 'src', wrapper / 'src')
        (f.home / 'hindsight-workspace').mkdir()
        (f.home / 'hindsight-workspace/config.json').write_text(json.dumps({'policy_path': str(f.owner)}))
        token = set_hermes_home_override(f.home)
        self.addCleanup(reset_hermes_home_override, token)
        adapter = load_memory_provider('hindsight-workspace', register_skills=False)
        assert adapter is not None
        adapter.initialize('synthetic-hermes-session', cwd=str(f.cwd), hermes_home=str(f.home), agent_identity='default')
        self.addCleanup(adapter.shutdown)
        f.bind()
        return f, adapter

    def test_native_sdk_and_codex_send_same_recall_and_reflect_fields(self):
        f, adapter = self.prepare()
        for name in ('hindsight_recall', 'hindsight_reflect'):
            f.calls.clear()
            result = json.loads(adapter.handle_tool_call(name, {'query': 'synthetic question'}))
            self.assertNotIn('error', result, result)
            self.assertIn('synthetic', result['result'])
            native_requests = copy.deepcopy([x for x in f.calls if x[0] == 'POST'])
            f.calls.clear()
            f.tool(name, {'query': 'synthetic question'})
            codex_requests = [x for x in f.calls if x[0] == 'POST']
            fields = {'query', 'budget', 'types', 'max_tokens', 'tags', 'tags_match', 'min_scores'}
            def functional(rows):
                normalized = []
                for _, path, body in rows:
                    body = {k: v for k, v in body.items() if k in fields}
                    if body.get('tags') is None and body.get('tags_match') == 'any':
                        body.pop('tags', None)
                        body.pop('tags_match', None)
                    if 'min_scores' in body:
                        # SDK serializes unset optional floors as null; HTTP omission is equivalent.
                        body['min_scores'] = {k: v for k, v in body['min_scores'].items() if v is not None}
                    normalized.append((path, json.dumps(body, sort_keys=True)))
                return sorted(normalized)
            self.maxDiff = None
            self.assertEqual(functional(native_requests), functional(codex_requests))
            self.assertEqual(len(native_requests), len(codex_requests))
        f.calls.clear()
        adapter.sync_turn('remember this', 'completed answer', session_id='synthetic-hermes-session')
        adapter.shutdown()
        native_writes = copy.deepcopy([x for x in f.calls if x[1].endswith('/memories')])
        self.assertEqual(len(native_writes), 2)
        f.calls.clear()
        f.run_cli('hook', f.event('Stop', transcript_path=str(f.transcript())))
        codex_writes = [x for x in f.calls if x[1].endswith('/memories')]
        self.assertEqual([x[1] for x in native_writes], [x[1] for x in codex_writes])
        for (_, _, native_body), (_, _, codex_body) in zip(native_writes, codex_writes):
            self.assertEqual(native_body['async'], codex_body['async'])
            native_item, codex_item = native_body['items'][0], codex_body['items'][0]
            for key in ('context', 'observation_scopes', 'strategy', 'update_mode'):
                self.assertEqual(native_item[key], codex_item[key])
            self.assertEqual([x for x in native_item['tags'] if not x.startswith('session:')],
                             [x for x in codex_item['tags'] if not x.startswith('session:')])
            native_content, codex_content = json.loads(native_item['content']), json.loads(codex_item['content'])
            self.assertEqual([(x['role'], x['content']) for x in native_content],
                             [(x['role'], x['content']) for x in codex_content])

    def test_context_mode_disables_content_tools_but_preserves_diagnostics(self):
        f, adapter = self.prepare(memory_mode='context')
        self.assertEqual(adapter.get_tool_schemas(), [])
        inventory = f.run_cli('tools', {})['tools']
        self.assertEqual({x['name'] for x in inventory}, {'hindsight_sync_status', 'hindsight_diagnose'})
        f.calls.clear()
        f.tool('hindsight_recall', {'query': 'must refuse'}, ok=False)
        f.tool('hindsight_capture_initiative', {'title': 'x', 'summary': 'must refuse'}, ok=False)
        self.assertEqual(f.calls, [])

    def test_prefetch_preamble_matches_native_consumer(self):
        for preamble in ('Use only these synthetic memories.', ''):
            with self.subTest(preamble=preamble):
                f, adapter = self.prepare(recall_prompt_preamble=preamble)
                native = adapter.prefetch('synthetic query', session_id='synthetic-hermes-session')
                got = f.run_cli('hook', f.event('UserPromptSubmit', prompt='synthetic query'))['hookSpecificOutput']['additionalContext']
                self.assertEqual(got.split('\n\n', 1)[0], native.split('\n\n', 1)[0])
                self.assertIn('synthetic recalled fact', got)
