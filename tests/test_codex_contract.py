"""Cross-process hook contracts against an owned synthetic HTTP server."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import policy

ROOT = Path(__file__).resolve().parents[1]


class CodexContractTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'cli.py').exists(), 'Codex shared entrypoint is missing')
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.owner = self.home / 'hindsight/config.json'
        self.owner.parent.mkdir()
        self.cwd = self.home / 'project with spaces'
        self.cwd.mkdir()
        self.calls = []
        self.docs = {}
        self.authorization = []
        self.faults = {}
        faults = self.faults
        authorization = self.authorization
        calls, docs = self.calls, self.docs
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.reply(None)
            def do_POST(self):
                self.reply(json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))))
            def reply(self, body):
                authorization.append(self.headers.get('Authorization'))
                calls.append((self.command, self.path, body))
                parts = self.path.split('/')
                bank = parts[4] if len(parts) > 4 else ''
                code = 200
                if body and self.path.endswith('/memories') and faults.get('fail_bank') == bank:
                    code, result = 503, {'detail': 'synthetic unavailable'}
                elif self.path.endswith('/version'):
                    result = {'api_version': '1.1.0', 'features': {}}
                elif self.path.endswith('/memories/recall'):
                    result = {'results': [{'id': 'synthetic-fact', 'text': 'synthetic recalled fact', 'scores': {'final': 0.9}}]}
                elif self.path.endswith('/reflect'):
                    result = {'text': 'synthetic reflection'}
                elif self.path.endswith('/knowledge-base/pages') and body:
                    docs[('page', 'kp-1')] = body
                    result = {'page_id': 'kp-1'}
                elif '/knowledge-base/pages/' in self.path:
                    if ('page', parts[-1]) not in docs:
                        code, result = 404, {'detail': 'Synthetic missing page'}
                    else:
                        result = {'id': parts[-1], 'name': docs[('page', parts[-1])]['name'], 'markdown': 'No content yet.'}
                elif self.path.endswith('/knowledge-base/folders'):
                    docs[('tree', 'roots')] = [{'id': 'folder-1', 'name': body['name'], 'kind': 'folder'}]
                    result = {'id': 'folder-1'}
                elif self.path.endswith('/knowledge-base/tree'):
                    result = {'roots': docs.get(('tree', 'roots'), [])}
                elif self.path.endswith('/memories'):
                    operation = body.get('operation_id')
                    items = [] if operation and ('op', bank, operation) in docs else body['items']
                    for item in items:
                        document = item.get('document_id', body.get('document_id'))
                        if document:
                            old = docs.get((bank, document))
                            if old and item.get('update_mode') == 'append':
                                docs[(bank, document)] = json.dumps(json.loads(old) + json.loads(item['content']))
                            else:
                                docs[(bank, document)] = item['content']
                    if operation:
                        docs[('op', bank, operation)] = True
                    result = {'success': True, 'bank_id': bank, 'operation_id': body.get('operation_id'), 'items_count': len(body['items']), 'async': body.get('async', False)}
                elif '/documents/' in self.path:
                    document = parts[-1]
                    result = {'id': document, 'bank_id': bank, 'original_text': docs.get((bank, document), '')}
                elif '/operations/' in self.path:
                    code = 200 if ('op', bank, parts[-1]) in docs else 404
                    result = {'status': ('pending' if faults.get('pending') else 'completed') if code == 200 else 'not_found'}
                else:
                    result = {'roots': [], 'results': [], 'items': []}
                data = json.dumps(result).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.p = policy.normalize_policy({'api_url': f'http://127.0.0.1:{self.server.server_port}',
            'additional_banks': ['write-extra'], 'recall_additional_banks': ['read-only'],
            'recall_sync': True, 'recall_tags': ['topic:x'], 'recall_tags_match': 'all',
            'recall_min_scores': {'final': 0.5}, 'retain_tags': ['shared'], 'retain_strategy': 'approved'})
        self.owner.write_text(json.dumps(self.p))
        self.env = {**os.environ, 'HINDSIGHT_WORKSPACE_PYTHON': sys.executable,
                    'HINDSIGHT_WORKSPACE_POLICY': str(self.owner)}

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_cli(self, command, payload, ok=True):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'cli.py'), command, '--policy', str(self.owner)],
            input=json.dumps(payload), text=True, capture_output=True, env=self.env, timeout=20, cwd=self.cwd)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return json.loads(result.stdout)

    def event(self, name, sid='thread-a', **kw):
        return {'hook_event_name': name, 'session_id': sid, 'cwd': str(self.cwd),
                **({'turn_id': 'turn-1'} if name == 'SubagentStart' else {}), **kw}

    def bind(self, sid='thread-a'):
        return self.run_cli('hook', self.event('SessionStart', sid))

    def tool(self, name, args=None, sid='thread-a', ok=True):
        return self.run_cli('tool', {'_meta': {'threadId': sid, 'sessionId': 'not-the-thread'},
            'name': name, 'arguments': args or {}}, ok)

    def test_invalid_initiative_page_can_be_corrected_in_same_session(self):
        self.bind()
        args = {'title': 'Synthetic initiative', 'summary': 'Synthetic update', 'relates_to_page_id': 'missing-page'}
        self.tool('hindsight_capture_initiative', args, ok=False)
        self.assertTrue(any(path.endswith('/pages/missing-page') for _, path, _ in self.calls))
        self.assertFalse(any(path.endswith('/memories') for _, path, _ in self.calls))
        self.docs[('page', 'known-page')] = {'name': args['title']}
        self.tool('hindsight_capture_initiative', {**args, 'relates_to_page_id': 'known-page'})
        writes = [body for _, path, body in self.calls if path.endswith('/memories')]
        self.assertEqual(len(writes), 2)
        self.assertEqual(len({body['items'][0]['document_id'] for body in writes}), 1)
        self.assertEqual(self.tool('hindsight_sync_status')['result']['activeOps'], 0)

    def test_initiative_pending_retry_keeps_marker_and_confirmed_banks(self):
        self.bind()
        args = {'title': 'Synthetic initiative', 'summary': 'Synthetic update'}
        self.faults['fail_bank'] = 'write-extra'
        self.tool('hindsight_capture_initiative', args, ok=False)
        before = len(self.calls)
        self.tool('hindsight_capture_initiative', {**args, 'summary': 'Cannot replace pending work'}, ok=False)
        self.assertEqual(len(self.calls), before)
        self.faults.pop('fail_bank')
        self.faults['pending'] = True
        self.tool('hindsight_capture_initiative', args, ok=False)
        self.faults['pending'] = False
        self.tool('hindsight_capture_initiative', args)
        writes = [x for x in self.calls if x[1].endswith('/memories')]
        markers = {x[2]['items'][0]['document_id'] for x in writes}
        self.assertEqual(len(markers), 1)
        primary = self.tool('hindsight_diagnose')['result']['bank']
        self.assertEqual(sum('/banks/' + primary + '/' in x[1] for x in writes), 1)
        self.assertEqual(len({x[2]['operation_id'] for x in writes}), 2)
        self.assertEqual(self.tool('hindsight_sync_status')['result']['activeOps'], 0)
        self.tool('hindsight_capture_initiative', {**args, 'summary': 'A separate update'})
        self.assertEqual(len({x[2]['items'][0]['document_id'] for x in self.calls if x[1].endswith('/memories')}), 2)

    def test_subagent_native_start_shares_bank_not_conversation_cursor(self):
        self.bind()
        self.run_cli('hook', self.event('SubagentStart', agent_id='child-a', agent_type='worker'))
        root = self.tool('hindsight_diagnose')['result']
        child = self.tool('hindsight_diagnose', sid='child-a')['result']
        self.assertEqual(child['bank'], root['bank'])
        path = self.transcript('child-a')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        inherited = [json.loads(line) for line in self.transcript('thread-a').read_text().replace('turn-1', 'parent-turn').replace('remember this', 'parent-only-secret-context').splitlines()]
        path.write_text(''.join(json.dumps(row) + '\n' for row in [rows[0], *inherited[1:], *rows[1:]]))
        self.run_cli('hook', self.event('SubagentStop', agent_id='child-a', agent_transcript_path=str(path)))
        writes = [x for x in self.calls if x[1].endswith('/memories')]
        self.assertEqual(len(writes), 2)
        item = writes[0][2]['items'][0]
        self.assertIn('session:child-a', item['tags'])
        self.assertIn('parent:thread-a', item['tags'])
        self.assertNotIn('parent-only-secret-context', item['content'])
        self.assertEqual(self.tool('hindsight_sync_status')['result']['conversationsIngested'], 0)
        self.assertEqual(self.tool('hindsight_sync_status', sid='child-a')['result']['conversationsIngested'], 1)

    def test_subagent_own_workspace_and_foreign_root_refusal(self):
        self.bind()
        other = self.home / 'other-workspace'
        other.mkdir()
        self.run_cli('hook', self.event('SubagentStart', agent_id='child-b', cwd=str(other)))
        root = self.tool('hindsight_diagnose')['result']
        child = self.tool('hindsight_diagnose', sid='child-b')['result']
        self.assertNotEqual(child['bank'], root['bank'])
        self.run_cli('hook', self.event('SubagentStart', 'unbound-parent', agent_id='unbound-child'), False)
        self.bind('other-parent')
        self.run_cli('hook', self.event('SubagentStart', 'other-parent', agent_id='child-b', cwd=str(other)), False)
        self.assertEqual(self.calls, [])

    def test_missing_mismatched_and_stale_identity_make_no_requests(self):
        self.run_cli('hook', {'hook_event_name': 'UserPromptSubmit', 'prompt': 'x'}, False)
        self.bind()
        self.run_cli('hook', self.event('UserPromptSubmit', cwd='/different', prompt='x'), False)
        self.run_cli('tool', {'_meta': {'sessionId': 'thread-a'}, 'name': 'hindsight_recall', 'arguments': {'query': 'x'}}, False)
        self.run_cli('tool', {'_meta': {'threadId': 'thread-a'}, 'name': 'hindsight_recall', 'arguments': {'query': 'x', 'bank_id': 'evil'}}, False)
        self.owner.write_text(json.dumps({**self.p, 'recall_budget': 'high'}))
        self.tool('hindsight_recall', {'query': 'x'}, ok=False)
        self.assertEqual(self.calls, [])

    def test_prompt_and_explicit_tools_apply_policy_and_bank_sets(self):
        self.bind()
        result = self.run_cli('hook', self.event('UserPromptSubmit', prompt='x' * 2100))
        self.assertIn('synthetic recalled fact', result['hookSpecificOutput']['additionalContext'])
        expected = policy.resolve_identity(session_id='thread-a', cwd=str(self.cwd), selected_cwd=str(self.cwd), policy=self.p)
        requests = [x for x in self.calls if x[0] == 'POST']
        self.assertEqual({x[1].split('/')[4] for x in requests}, {expected['bank'], 'write-extra', 'read-only'})
        for _, _, body in requests:
            self.assertEqual(body, {'query': 'x' * 2000, 'budget': 'mid', 'types': self.p['recall_types'],
                'max_tokens': 4096, 'tags': ['topic:x'], 'tags_match': 'all', 'min_scores': {'final': 0.5}})
        self.calls.clear()
        self.tool('hindsight_reflect', {'query': 'why'})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], {'query': 'why', 'budget': 'mid'})

    def transcript(self, sid='thread-a', interrupted=False):
        path = self.home / (sid + '.jsonl')
        rows = [{'type': 'session_meta', 'payload': {'id': sid, 'cwd': str(self.cwd)}},
                {'type': 'turn_context', 'payload': {'turn_id': 'turn-1', 'cwd': str(self.cwd)}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'remember this'}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'completed answer'}]}},
                {'type': 'event_msg', 'payload': {'type': 'turn_aborted' if interrupted else 'task_complete', 'turn_id': 'turn-1'}}]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def test_stop_replay_and_resume_do_not_repeat_confirmed_turns(self):
        self.bind()
        transcript = self.transcript()
        event = self.event('Stop', transcript_path=str(transcript))
        self.run_cli('hook', event)
        writes = [x for x in self.calls if x[1].endswith('/memories')]
        self.assertEqual(len(writes), 2)
        self.assertNotIn('read-only', [x[1].split('/')[4] for x in writes])
        item = writes[0][2]['items'][0]
        self.assertEqual(item['context'], self.p['retain_context'])
        self.assertEqual(item['strategy'], 'approved')
        self.assertEqual(item['observation_scopes'], 'shared')
        self.assertIn('session:thread-a', item['tags'])
        self.bind()
        self.calls.clear()
        self.run_cli('hook', event)
        self.assertFalse([x for x in self.calls if x[0] == 'POST'])

    def test_interrupted_and_foreign_transcripts_are_not_retained(self):
        self.bind()
        self.run_cli('hook', self.event('Stop', transcript_path=str(self.transcript(interrupted=True))))
        self.run_cli('hook', self.event('Stop', transcript_path=str(self.transcript('foreign'))), False)
        self.assertEqual(self.calls, [])

    def test_two_chats_share_bank_not_cursor(self):
        self.bind('thread-a')
        self.bind('thread-b')
        for sid in ('thread-a', 'thread-b'):
            self.run_cli('hook', self.event('Stop', sid, transcript_path=str(self.transcript(sid))))
        writes = [x for x in self.calls if x[1].endswith('/memories')]
        self.assertEqual(len(writes), 4)
        self.assertEqual(len({x[2]['items'][0]['document_id'] for x in writes}), 2)

    def test_sync_status_reads_shared_collector_receipts(self):
        import hashlib
        self.bind()
        self.assertFalse(self.tool('hindsight_sync_status')['result']['synced'])
        identity = policy.resolve_identity(session_id='thread-a', cwd=str(self.cwd), selected_cwd=str(self.cwd), policy=self.p)
        key = hashlib.sha256(f'{identity["api_url"]}\0{identity["bank"]}\0{identity["cwd"]}'.encode()).hexdigest()
        root = self.home / 'plugin-data/hindsight-workspace' / key
        root.mkdir(parents=True)
        state = {k: identity[k] for k in ('cwd', 'bank', 'api_url')}
        state.update(survey_head='', survey_operation='synthetic-operation')
        (root / 'state.json').write_text(json.dumps(state))
        self.docs[(identity['bank'], 'hermes-workspace-survey')] = 'Synthetic verified map.'
        self.docs[('op', identity['bank'], 'synthetic-operation')] = True
        result = self.tool('hindsight_sync_status')['result']
        self.assertTrue(result['synced'])
        self.assertTrue(result['surveyPresent'])
        self.assertEqual(result['git'], 'not-git')

    def test_concurrent_stop_processes_do_not_duplicate_append(self):
        from concurrent.futures import ThreadPoolExecutor
        self.bind()
        event = self.event('Stop', transcript_path=str(self.transcript()))
        def run():
            return subprocess.run([sys.executable, '-B', str(ROOT / 'cli.py'), 'hook', '--policy', str(self.owner)],
                input=json.dumps(event), text=True, capture_output=True, env=self.env, timeout=20)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertIn(0, [r.returncode for r in results])
        self.run_cli('hook', event)
        self.assertEqual(len([x for x in self.calls if x[1].endswith('/memories')]), 2)

    def test_partial_multibank_retry_does_not_append_primary_twice(self):
        self.bind()
        path = self.transcript()
        event = self.event('Stop', transcript_path=str(path))
        self.faults['fail_bank'] = 'write-extra'
        self.run_cli('hook', event, False)
        self.faults.clear()
        self.run_cli('hook', event)
        writes = [x for x in self.calls if x[1].endswith('/memories')]
        self.assertEqual(len([x for x in writes if '/write-extra/' not in x[1]]), 1)
        retries = [x for x in writes if '/write-extra/' in x[1]]
        self.assertEqual(len(retries), 2)
        self.assertEqual(retries[0][2]['operation_id'], retries[1][2]['operation_id'])
        for key, content in self.docs.items():
            if len(key) == 2 and key[1].startswith('codex-session-'):
                self.assertEqual(len(json.loads(content)), 2)

    def test_credentials_follow_policy_owner_not_inherited_environment(self):
        (self.home / '.env').write_text('HINDSIGHT_API_KEY=synthetic-owner-key\n')
        self.env['HINDSIGHT_API_KEY'] = 'synthetic-wrong-profile'
        self.bind()
        self.tool('hindsight_recall', {'query': 'x'})
        self.assertEqual(self.authorization, ['Bearer synthetic-owner-key'] * 3)

    def test_pending_short_flush_is_recovered_before_resumed_batch(self):
        self.owner.write_text(json.dumps({**self.p, 'retain_every_n_turns': 2}))
        self.bind()
        path = self.transcript()
        first = path.read_text()
        self.faults['pending'] = True
        self.run_cli('hook', self.event('SessionEnd', transcript_path=str(path)), False)
        self.faults.clear()
        self.run_cli('hook', self.event('SessionStart', transcript_path=str(path)))
        self.assertEqual(self.tool('hindsight_sync_status')['result']['conversationsIngested'], 1)
        second = '\n'.join(first.splitlines()[1:]).replace('turn-1', 'turn-2').replace('remember this', 'second distinct turn') + '\n'
        path.write_text(first + second)
        self.run_cli('hook', self.event('Stop', transcript_path=str(path)))
        self.run_cli('hook', self.event('SessionEnd', transcript_path=str(path)))
        for key, content in self.docs.items():
            if len(key) == 2 and key[1].startswith('codex-session-'):
                self.assertEqual(len(json.loads(content)), 4)
        self.assertEqual(self.tool('hindsight_sync_status')['result']['activeOps'], 0)
        self.assertEqual(self.tool('hindsight_sync_status')['result']['conversationsIngested'], 2)

    def test_two_turn_cadence_is_one_append_batch_per_bank(self):
        self.owner.write_text(json.dumps({**self.p, 'retain_every_n_turns': 2}))
        self.bind()
        path = self.transcript()
        first = path.read_text()
        self.run_cli('hook', self.event('Stop', transcript_path=str(path)))
        self.assertEqual(self.calls, [])
        second = '\n'.join(first.splitlines()[1:]).replace('turn-1', 'turn-2') + '\n'
        path.write_text(first + second)
        self.run_cli('hook', self.event('Stop', transcript_path=str(path)))
        writes = [x for x in self.calls if x[1].endswith('/memories')]
        self.assertEqual(len(writes), 2)
        self.assertEqual(len(json.loads(writes[0][2]['items'][0]['content'])), 4)
        self.calls.clear()
        self.run_cli('hook', self.event('SessionEnd', transcript_path=str(path)))
        self.assertEqual(self.calls, [])

    def test_optional_initiative_keeps_manual_page_and_shared_retain_policy(self):
        self.bind()
        result = self.tool('hindsight_capture_initiative', {'title': 'Synthetic initiative', 'summary': 'Approved test scope.'})
        self.assertEqual(result['result']['page_id'], 'kp-1')
        pages = [x for x in self.calls if x[0] == 'POST' and x[1].endswith('/knowledge-base/pages')]
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0][2]['trigger'], {'type': 'manual'})
        self.assertTrue(any(x[0] == 'GET' and x[1].endswith('/knowledge-base/pages/kp-1') for x in self.calls))
        writes = [x for x in self.calls if x[1].endswith('/memories')]
        self.assertEqual(len(writes), 2)
        for _, _, body in writes:
            self.assertEqual(body['items'][0]['strategy'], 'approved')
            self.assertIn('[[page:kp-1]]', body['items'][0]['context'])
        self.calls.clear()
        self.tool('hindsight_capture_initiative', {'title': 'Synthetic initiative', 'summary': 'Approved changed scope.', 'relates_to_page_id': 'kp-1'})
        self.assertFalse([x for x in self.calls if x[0] == 'POST' and x[1].endswith('/knowledge-base/pages')])

    def test_async_stop_waits_for_real_completion_marker(self):
        self.bind()
        path = self.transcript()
        lines = path.read_text().splitlines()
        completion = lines.pop()
        path.write_text('\n'.join(lines) + '\n')
        def complete():
            import time
            time.sleep(0.35)
            with path.open('a') as stream:
                stream.write(completion + '\n')
        writer = threading.Thread(target=complete)
        writer.start()
        try:
            self.run_cli('hook', self.event('Stop', turn_id='turn-1', transcript_path=str(path)))
            self.assertEqual(len([x for x in self.calls if x[1].endswith('/memories')]), 2)
        finally:
            writer.join()

    def test_subagent_does_not_borrow_parent_pin(self):
        self.bind()
        self.run_cli('hook', self.event('UserPromptSubmit', agent_id='child', prompt='x'), False)
        self.assertEqual(self.calls, [])

    def test_session_end_recovers_completed_turn(self):
        self.bind()
        self.run_cli('hook', self.event('SessionEnd', transcript_path=str(self.transcript())))
        self.assertEqual(len([x for x in self.calls if x[1].endswith('/memories')]), 2)

    def test_real_mcp_stdio_metadata(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/test_codex_transport.mjs')],
            text=True, capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_node_hook_and_inventory(self):
        result = subprocess.run(['node', str(ROOT / 'codex/hooks.mjs')], input=json.dumps(self.event('SessionStart')),
            text=True, capture_output=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        tools = self.run_cli('tools', {})['tools']
        self.assertEqual({x['name'] for x in tools}, {'hindsight_sync_status', 'hindsight_diagnose',
            'hindsight_search_knowledge_pages', 'hindsight_list_knowledge_pages', 'hindsight_read_knowledge_page',
            'hindsight_reflect', 'hindsight_capture_initiative', 'hindsight_ingest_document', 'hindsight_recall'})


if __name__ == '__main__': unittest.main()
