"""Bounded, read-only workspace collection and Hindsight ingestion."""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from types import SimpleNamespace

TAG = 'hermes-workspace'
DOC_GIT = 'hermes-workspace-git-messages'
DOC_SURVEY = 'hermes-workspace-survey'
SOURCE_DIRS = {'src', 'lib', 'app', 'apps', 'packages', 'tests', 'test', 'docs', 'include', 'cmd'}
SKIP_DIRS = {'node_modules', 'vendor', 'dist', 'build', 'target', 'venv', '__pycache__'}
SECRET = re.compile(r'(?i)(secret|credential|password|private|token|(^|[-_])id_rsa|\.pem$|\.key$|\.p12$|\.pfx$)')
KEY_NAMES = {'readme.md', 'readme.rst', 'readme', 'package.json', 'pyproject.toml', 'cargo.toml', 'go.mod', 'makefile', 'dockerfile', 'requirements.txt'}
SOURCE_EXTS = {'.py', '.js', '.ts', '.tsx', '.go', '.rs', '.java', '.md', '.toml'}


def clean_excerpt(text: str) -> str:
    # Cropped PEM blocks still contain secrets; URLs may hide credentials in any component.
    text = re.sub(r'-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END[A-Z ]*PRIVATE KEY-----|\Z)', '[REDACTED PRIVATE KEY]', text)
    return re.sub(r'''[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"'`]+''', '[REDACTED URL]', text)


@dataclasses.dataclass
class Target:
    cwd: Path
    bank: str
    api_url: str
    data_dir: Path
    api_key: str = dataclasses.field(default='', repr=False)
    redact: Callable[[str], str] = dataclasses.field(default=lambda s: s, repr=False)
    summarize: Callable[[str], str] | None = dataclasses.field(default=None, repr=False)
    session_id: str = ''
    retain_policy: dict | None = dataclasses.field(default=None, repr=False)
    validate: Callable[[], None] | None = dataclasses.field(default=None, repr=False)

    def __post_init__(self):
        self.cwd, self.data_dir = Path(self.cwd), Path(self.data_dir)
        url = urllib.parse.urlsplit(self.api_url)
        if (not self.cwd.is_absolute() or not self.cwd.is_dir()
                or self.cwd.resolve() != self.cwd or str(self.cwd) == self.cwd.anchor
                or any(ord(c) < 32 or ord(c) == 127 for c in str(self.cwd))
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', self.bank)
                or not self.data_dir.is_absolute()
                or url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}):
            raise ValueError('unsafe or ambiguous workspace target')
        self.api_url = self.api_url.rstrip('/')

    def clean(self, text: str) -> str:
        return self.redact(clean_excerpt(text))

    @property
    def state_root(self) -> Path:
        key = hashlib.sha256(f'{self.api_url}\0{self.bank}\0{self.cwd}'.encode()).hexdigest()
        return self.data_dir / key


def git(cwd: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
    command = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
               '-c', 'log.showSignature=false', '-c', 'core.pager=cat', '-C', str(cwd), *args]
    data = memoryview(input.encode() if input is not None else b'')
    output = bytearray()
    with subprocess.Popen(command, stdin=subprocess.PIPE if data else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if data:
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                deadline = time.monotonic() + 20
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Git command timed out')
                    for key, event in selector.select(remaining):
                        if event & selectors.EVENT_WRITE:
                            data = data[os.write(key.fd, data[:4096]):]
                            if not data:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            output.extend(chunk)
                            if len(output) > 2_000_000:
                                raise RuntimeError('Git output exceeds safety budget')
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
    return subprocess.CompletedProcess(command, process.returncode, output.decode('utf-8', errors='replace'), '')


def has_git(cwd: Path) -> bool:
    marker = cwd / '.git'
    return not marker.is_symlink() and (marker.is_dir() or marker.is_file())


def git_snapshot(cwd: Path) -> dict | None:
    if not has_git(cwd):
        return None
    top = git(cwd, 'rev-parse', '--show-toplevel')
    if top.returncode or Path(top.stdout.strip()) != cwd:
        raise ValueError('Git root does not match workspace')
    head = git(cwd, 'rev-parse', '--verify', 'HEAD')
    if head.returncode:
        return {'head': '', 'content': '', 'count': 0}
    result = git(cwd, '--no-pager', 'log', '-300', '--no-show-signature', '--no-use-mailmap',
                 '--format=%H%x00%cI%x00%B%x00', head.stdout.strip(), '--')
    if result.returncode:
        raise RuntimeError('git log failed')
    fields = result.stdout.split('\0')
    rows = []
    for n in range(0, len(fields) - 2, 3):
        commit, date, message = fields[n:n + 3]
        if not re.fullmatch(r'[0-9a-f]{40,64}', commit.strip()):
            raise ValueError('invalid Git commit identity')
        rows.append(f'{commit.strip()} {date}\n{clean_excerpt(message.strip())[:2000]}')
    return {'head': head.stdout.strip(), 'content': '\n\n'.join(rows), 'count': len(rows)}


def survey_due(cwd: Path, baseline: str | None, head: str) -> bool:
    if baseline is None:
        return True
    if not head or head == baseline:
        return False
    if not baseline or git(cwd, 'merge-base', '--is-ancestor', baseline, head).returncode:
        return True  # A rewritten history cannot be counted against the old baseline.
    count = git(cwd, 'rev-list', '--count', f'{baseline}..{head}')
    if count.returncode:
        raise RuntimeError('cannot count survey refresh commits')
    return int(count.stdout) >= 20


def safe_name(name: str) -> bool:
    return (not name.startswith('.') and name not in SKIP_DIRS and not SECRET.search(name)
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


@contextlib.contextmanager
def open_directory(root: Path, rel: Path):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in rel.parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_relative(root: Path, rel: Path) -> str:
    """Open every component without following links, including concurrent replacements."""
    with open_directory(root, rel.parent) as fd:
        file_fd = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 131072:
                return ''
            data = os.read(file_fd, 4000)
            return '' if b'\0' in data else clean_excerpt(data.decode('utf-8', errors='replace'))
        finally:
            os.close(file_fd)


def collect_survey(cwd: Path) -> str:
    entries, candidates = [], []
    queue = [(cwd, 0)]
    while queue and len(entries) < 200:
        directory, depth = queue.pop(0)
        # Never descend arbitrary home/Documents trees just because cwd has no Git.
        try:
            with open_directory(cwd, directory.relative_to(cwd)) as fd, os.scandir(fd) as scan:
                children = [(entry.name, entry.is_symlink(), entry.is_dir(follow_symlinks=False))
                            for entry in itertools.islice(scan, 400)]
                children.sort()
        except OSError:
            continue
        for name, is_link, is_dir in children:
            if not safe_name(name) or is_link:
                continue
            rel = directory.relative_to(cwd) / name
            entries.append(str(rel) + ('/' if is_dir else ''))
            if is_dir and depth < 2 and (depth > 0 or name in SOURCE_DIRS):
                queue.append((cwd / rel, depth + 1))
            elif not is_dir and (name.lower() in KEY_NAMES or rel.suffix in SOURCE_EXTS):
                candidates.append(rel)
            if len(entries) >= 200:
                break
    if has_git(cwd) and entries:
        result = git(cwd, 'check-ignore', '--no-index', '-z', '--stdin', input='\0'.join(entries) + '\0')
        if result.returncode not in (0, 1):
            raise RuntimeError('cannot enforce repository ignore rules')
        ignored = set(result.stdout.split('\0'))
        entries = [name for name in entries if name not in ignored]
        candidates = [p for p in candidates if str(p) in entries]
    candidates.sort(key=lambda p: (p.name.lower() not in KEY_NAMES, len(p.parts), str(p)))
    chunks = [f'Workspace: {cwd}\nBounded directory map (max 200 entries, depth 3):\n' + '\n'.join(entries)]
    for rel in candidates[:8]:
        try:
            text = read_relative(cwd, rel)
        except OSError:
            continue
        if text:
            chunks.append(f'\n--- {rel} (excerpt, at most 4000 bytes) ---\n{text}')
    return '\n'.join(chunks)[:24000]


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{uuid.uuid4().hex}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def workspace_lock(target: Target):
    target.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target.state_root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, target: Target | SimpleNamespace, stop: threading.Event):
        self.target, self.stop = target, stop
        self.base = target.api_url + '/v1/default/banks/' + urllib.parse.quote(target.bank, safe='')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        if self.stop.is_set():
            raise InterruptedError('workspace worker stopped')
        validate = getattr(self.target, 'validate', None)
        if validate is not None:
            validate()
        if timeout is None:
            timeout = (getattr(self.target, 'retain_policy', None) or {}).get('timeout', 30)
        headers = {'Content-Type': 'application/json'}
        if self.target.api_key:
            headers['Authorization'] = 'Bearer ' + self.target.api_key
        request = urllib.request.Request(self.base + path, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise RuntimeError('Hindsight response exceeds size limit')
            return json.loads(raw)

    def retain(self, document_id: str, content: str):
        pending_path = self.target.state_root / (document_id + '-pending.json')
        pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
        if pending and pending['content'] != content:
            # A missing operation may still be committing: replay its journaled payload first.
            self.finish_retain(document_id, pending)
            pending_path.unlink()
            pending = {}
        if not pending:
            pending = {'content': content, 'operation_id': str(uuid.uuid4())}
            atomic_json(pending_path, pending)
        operation = self.finish_retain(document_id, pending)
        pending_path.unlink(missing_ok=True)
        return operation

    def finish_retain(self, document_id: str, pending: dict):
        operation, content = pending['operation_id'], pending['content']
        path = '/operations/' + operation
        try:
            previous = self.request(path)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            exc.close()
            previous = {'status': 'not_found'}
        if previous.get('status') in {'failed', 'cancelled'}:
            self.request(path + '/retry', {})
        else:
            policy = self.target.retain_policy or {}
            shaping = {'strategy': policy['retain_strategy']} if policy.get('retain_strategy') else {}
            response = self.request('/memories', {'async': True, 'operation_id': operation, 'items': [{
                'content': content, 'document_id': document_id, 'context': 'Hermes workspace read-only survey/history',
                'metadata': {'source': policy.get('retain_source') or TAG, 'workspace': str(self.target.cwd)},
                'tags': policy.get('retain_tags', []) + [TAG], 'observation_scopes': policy.get('observation_scopes', 'shared'),
                'update_mode': 'replace', **shaping}]})
            if not response.get('success') or response.get('bank_id') != self.target.bank or response.get('operation_id') != operation:
                raise RuntimeError('Hindsight retain acknowledgement mismatch')
        self.wait_operation(path, require_success=True)
        doc = self.request('/documents/' + urllib.parse.quote(document_id, safe=''))
        if doc.get('bank_id') != self.target.bank or doc.get('id') != document_id:
            raise RuntimeError('Hindsight document readback mismatch')
        if doc.get('original_text') != content:
            # A completed UUID cannot restore a document replaced by another profile.
            (self.target.state_root / (document_id + '-pending.json')).unlink(missing_ok=True)
            raise RuntimeError('Hindsight document readback mismatch')
        return operation

    def wait_operation(self, path: str, require_success: bool = False):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            try:
                status = self.request(path).get('status')
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                exc.close()
                status = 'not_found'
            if status == 'completed' or (not require_success and status in {'failed', 'cancelled', 'not_found'}):
                return
            if status not in {'pending', 'processing'}:
                raise RuntimeError(f'Hindsight retain {status}')
            if self.stop.wait(2):
                raise InterruptedError('workspace worker stopped')
        else:
            raise TimeoutError('Hindsight retain still pending; retry on next turn')


def sync_workspace(target: Target, *, stop: threading.Event | None = None) -> dict:
    stop = stop if stop is not None else threading.Event()
    if target.validate is not None:
        target.validate()
    if stop.is_set():
        raise InterruptedError('workspace worker stopped')
    with workspace_lock(target) as acquired:
        if not acquired:
            return {'status': 'busy'}
        path = target.state_root / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        identity = {'cwd': str(target.cwd), 'bank': target.bank, 'api_url': target.api_url}
        if state and any(state.get(k) != v for k, v in identity.items()):
            raise ValueError('state identity mismatch')
        state.update(identity)
        client = Client(target, stop)
        snap = git_snapshot(target.cwd)
        head = snap['head'] if snap else ''
        result = {'status': 'ok', 'git': 'not-git' if snap is None else 'empty' if not head else 'unchanged', 'survey': 'unchanged', **identity}
        if head and head != state.get('git_head'):
            text = target.clean(f'Git commit messages for workspace {target.cwd}\nLatest {snap["count"]} commits (maximum 300); messages capped at 2000 characters, no diffs.\n\n' + snap['content'])
            state['git_operation'] = client.retain(DOC_GIT, text)
            state['git_head'] = head
            atomic_json(path, state)
            result['git'] = 'updated'
        if survey_due(target.cwd, state.get('survey_head'), head):
            pending_path = target.state_root / 'survey-pending.json'
            pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
            if pending.get('head') != head or not pending.get('text'):
                source = target.clean(collect_survey(target.cwd))
                if not callable(target.summarize):
                    raise RuntimeError('host survey summarizer unavailable')
                if stop.is_set():
                    raise InterruptedError('workspace worker stopped')
                text = str(target.summarize(source) or '').strip()
                if len(text) < 20:
                    raise RuntimeError('empty Hindsight survey')
                pending = {'head': head, 'text': target.clean(f'Workspace survey: {target.cwd}\nBaseline HEAD: {head or "non-Git/unborn"}\n\n' + text)}
                atomic_json(pending_path, pending)
            state['survey_operation'] = client.retain(DOC_SURVEY, pending['text'])
            state['survey_head'] = head
            atomic_json(path, state)
            pending_path.unlink(missing_ok=True)
            result['survey'] = 'updated'
        state['last_result'] = result
        state['checked_at'] = time.time()
        atomic_json(path, state)
        return result
