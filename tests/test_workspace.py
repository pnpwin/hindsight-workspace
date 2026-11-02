import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from hindsight_workspace import hindsight_workspace as w
except ModuleNotFoundError:
    w = None

SCRATCH = Path(os.environ.get('TMPDIR', str(Path(__file__).resolve().parents[1] / 'test-tmp')))


class FakeAPI:
    def __init__(self):
        self.calls, self.docs, self.ops = [], {}, {}
        self.payloads = {}
        self.fail = False
        self.bad_readback = False
        api = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                api.calls.append((self.path, body))
                if self.path.endswith('/retry'):
                    api.complete(self.path.split('/')[-2])
                    result = {'success': True}
                elif self.path.endswith('/reflect'):
                    result = {'text': '# Project map\nA small Python fixture with README and tests.'}
                else:
                    bank = self.path.split('/')[4]
                    op = body['operation_id']
                    if op not in api.ops:
                        api.payloads[op] = (bank, body)
                        api.complete(op)
                    result = {'success': True, 'bank_id': bank, 'items_count': len(body['items']), 'async': True, 'operation_id': op}
                self.respond(result)
            def do_GET(self):
                from urllib.parse import unquote
                bits = self.path.split('/')
                if 'operations' in bits:
                    self.respond({'status': api.ops.get(bits[-1], 'not_found')}, 200 if bits[-1] in api.ops else 404)
                else:
                    text = api.docs.get((bits[4], unquote(bits[-1])), '')
                    self.respond({'id': unquote(bits[-1]), 'bank_id': bits[4], 'original_text': 'WRONG' if api.bad_readback else text})
            def respond(self, result, status=200):
                data = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
    def complete(self, op):
        self.ops[op] = 'failed' if self.fail else 'completed'
        if not self.fail:
            bank, body = self.payloads[op]
            for item in body['items']:
                self.docs[(bank, item['document_id'])] = item['content']

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(w, 'workspace implementation is missing')
        SCRATCH.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'project'
        self.repo.mkdir()
        (self.repo / 'README.md').write_text('# Fixture\nSmall Python service. Run unittest.')
        self.api = FakeAPI()
        self.addCleanup(self.api.close)
        self.target = w.Target(cwd=self.repo, bank='fixture-a', api_url=self.api.url, data_dir=self.root / 'state')
        self.target.summarize = lambda source: '# Synthetic test project map\n' + source
    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', HOME=str(self.root))
        return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-C', str(self.repo), *args], env=env, stderr=subprocess.DEVNULL, text=True).strip()
    def init_git(self):
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', 'README.md')
        self.git('commit', '-qm', 'Initial fixture history')
    def test_non_git_survey(self):
        self.assertIsNone(w.git_snapshot(self.repo))
        text = w.collect_survey(self.repo)
        self.assertIn('README.md', text)
        self.assertIn('Small Python service', text)
        result = w.sync_workspace(self.target)
        self.assertEqual(result['survey'], 'updated')
        self.assertEqual(result['git'], 'not-git')
        self.assertEqual(len(self.api.docs), 1)
    def test_repeat_is_noop(self):
        w.sync_workspace(self.target)
        before = len(self.api.calls)
        result = w.sync_workspace(self.target)
        self.assertEqual(result['survey'], 'unchanged')
        self.assertEqual(before, len(self.api.calls))
    def test_git_message_not_diff(self):
        self.init_git()
        (self.repo / 'data.txt').write_text('DIFF_ONLY_MARKER')
        self.git('add', '.')
        self.git('commit', '-qm', 'Add fixture feature\n\nExplain why.')
        snap = w.git_snapshot(self.repo)
        self.assertIn('Add fixture feature', snap['content'])
        self.assertIn('Explain why.', snap['content'])
        self.assertNotIn('DIFF_ONLY_MARKER', snap['content'])
        result = w.sync_workspace(self.target)
        self.assertEqual(result['git'], 'updated')
        self.assertEqual(len(self.api.docs), 2)
    def test_nested_cwd_is_not_git(self):
        self.init_git()
        child = self.repo / 'src'
        child.mkdir()
        self.assertIsNone(w.git_snapshot(child))
    def test_empty_repo_still_surveyed(self):
        self.git('init', '-q')
        result = w.sync_workspace(self.target)
        self.assertEqual(result['survey'], 'updated')
        self.assertEqual(result['git'], 'empty')
    def test_twenty_commit_refresh(self):
        self.init_git()
        w.sync_workspace(self.target)
        for n in range(19):
            self.git('commit', '--allow-empty', '-qm', f'change {n}')
        result = w.sync_workspace(self.target)
        self.assertEqual(result['survey'], 'unchanged')
        self.git('commit', '--allow-empty', '-qm', 'twentieth change')
        result = w.sync_workspace(self.target)
        self.assertEqual(result['survey'], 'updated')
    def test_hidden_ignored_secrets_symlinks_excluded(self):
        self.init_git()
        (self.repo / '.env').write_text('SECRET_ENV_MARKER')
        (self.repo / 'credentials.json').write_text('SECRET_JSON_MARKER')
        (self.repo / 'ignored.py').write_text('IGNORED_MARKER')
        (self.repo / '.gitignore').write_text('ignored.py\n')
        self.git('add', '.env', 'credentials.json', '.gitignore')
        self.git('commit', '-qm', 'Synthetic secrets for exclusion test')
        outside = self.root / 'outside.py'
        outside.write_text('OUTSIDE_MARKER')
        (self.repo / 'main.py').symlink_to(outside)
        text = w.collect_survey(self.repo)
        for forbidden in ('SECRET_ENV_MARKER', 'SECRET_JSON_MARKER', 'IGNORED_MARKER', 'OUTSIDE_MARKER', '.env', 'credentials.json'):
            self.assertNotIn(forbidden, text)
    def test_non_git_hidden_and_nested_private_files_excluded(self):
        (self.repo / '.ssh').mkdir()
        (self.repo / '.ssh' / 'key.py').write_text('PRIVATE_MARKER')
        (self.repo / 'Documents').mkdir()
        (self.repo / 'Documents' / 'private.py').write_text('DOCUMENT_MARKER')
        text = w.collect_survey(self.repo)
        self.assertNotIn('PRIVATE_MARKER', text)
        self.assertNotIn('DOCUMENT_MARKER', text)
    def test_failed_retain_retries(self):
        self.api.fail = True
        with self.assertRaises(RuntimeError):
            w.sync_workspace(self.target)
        self.api.fail = False
        result = w.sync_workspace(self.target)
        self.assertEqual(result['survey'], 'updated')
    def test_readback_mismatch_does_not_advance(self):
        self.api.bad_readback = True
        with self.assertRaises(RuntimeError):
            w.sync_workspace(self.target)
        self.api.bad_readback = False
        self.assertEqual(w.sync_workspace(self.target)['survey'], 'updated')
    def test_two_bank_isolation(self):
        other = self.root / 'other' / 'project'
        other.mkdir(parents=True)
        (other / 'README.md').write_text('# Different fixture')
        w.sync_workspace(self.target)
        target2 = w.Target(cwd=other, bank='fixture-b', api_url=self.api.url, data_dir=self.root / 'state', summarize=self.target.summarize)
        w.sync_workspace(target2)
        self.assertEqual({bank for bank, _ in self.api.docs}, {'fixture-a', 'fixture-b'})
        self.assertEqual(len(self.api.docs), 2)
    def test_lock_contention_skips(self):
        import fcntl
        with w.workspace_lock(self.target) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(w.sync_workspace(self.target)['status'], 'busy')
    def test_cancelled_no_network(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            w.sync_workspace(self.target, stop=stop)
        self.assertEqual(self.api.calls, [])
    def test_redaction_applied_before_network(self):
        (self.repo / 'README.md').write_text('SENSITIVE_FIXTURE_TOKEN')
        self.target.redact = lambda s: s.replace('SENSITIVE_FIXTURE_TOKEN', '[REDACTED]')
        w.sync_workspace(self.target)
        self.assertNotIn('SENSITIVE_FIXTURE_TOKEN', json.dumps(self.api.calls))
    def test_invalid_target_refused(self):
        for cwd, bank, url in [(Path('.'), 'x', self.api.url), (self.repo, '', self.api.url), (self.repo, '../other', self.api.url), (self.repo, 'x', 'https://example.com')]:
            with self.subTest(cwd=cwd, bank=bank, url=url), self.assertRaises(ValueError):
                w.Target(cwd=cwd, bank=bank, api_url=url, data_dir=self.root / 'state')

if __name__ == '__main__':
    unittest.main()
