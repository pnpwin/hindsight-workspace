"""Codex identity pins and policy-backed requests; no process-cwd fallback."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
import uuid

from .hindsight_workspace import Client, atomic_json
from .policy import bank_sets, canonical_path, load_policy, resolve_identity, validate_binding

TOOL_ARGS = {
    'hindsight_sync_status': (), 'hindsight_diagnose': (),
    'hindsight_search_knowledge_pages': ('query',), 'hindsight_list_knowledge_pages': (),
    'hindsight_read_knowledge_page': ('page_id',), 'hindsight_reflect': ('query',),
    'hindsight_capture_initiative': ('title', 'summary', 'relates_to_page_id?'),
    'hindsight_ingest_document': ('title', 'content'), 'hindsight_recall': ('query',),
}


DIAGNOSTICS = {'hindsight_sync_status', 'hindsight_diagnose'}


def tools(policy):
    return [{'name': name, 'description': name.replace('hindsight_', '').replace('_', ' ') + ' in the bound workspace.',
             'inputSchema': {'type': 'object', 'properties': {k.rstrip('?'): {'type': 'string'} for k in keys},
                             'required': [k for k in keys if not k.endswith('?')], 'additionalProperties': False}}
            for name, keys in TOOL_ARGS.items() if policy['memory_mode'] != 'context' or name in DIAGNOSTICS]


def read_json(path, limit=2_000_000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Expected regular state file')
        text = stream.read(limit + 1)
    if len(text) > limit:
        raise ValueError('State exceeds safety budget')
    return json.loads(text)


class Session:
    def __init__(self, owner, thread_id):
        self.owner = Path(owner)
        self.policy = load_policy(self.owner)
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 512 or any(ord(c) < 33 or ord(c) == 127 for c in thread_id):
            raise ValueError('Trusted threadId is required')
        self.thread_id = thread_id
        self.root = self.owner.parent.parent / 'plugin-data/hindsight-workspace/sessions' / hashlib.sha256(thread_id.encode()).hexdigest()
        self.path = self.root / 'state.json'
        self.state = {}

    @contextmanager
    def locked(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.resolve() != self.root:
            raise ValueError('Symlinked session state is refused')
        fd = os.open(self.root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state = read_json(self.path) if self.path.exists() else {}
            yield self
        finally:
            os.close(fd)

    def save(self):
        atomic_json(self.path, self.state)

    def bind(self, cwd):
        candidate = resolve_identity(session_id=self.thread_id, cwd=cwd, selected_cwd=cwd, policy=self.policy)
        if self.state:
            self.check(cwd)
        else:
            self.state = {'identity': candidate, 'completed': [], 'pending': {}, 'prefetch_query': ''}
            self.save()
        return candidate

    def check(self, cwd=None):
        current = load_policy(self.owner)
        if current != self.policy or not self.state:
            raise ValueError('Missing or stale session binding')
        identity = self.state['identity']
        validate_binding(identity, current, session_id=self.thread_id, cwd=identity['cwd'] if cwd is None else cwd)
        return identity

    def request(self, bank, path, body=None):
        self.check()
        raw = read_json(self.owner, 131072)
        key = raw.get('apiKey') or raw.get('api_key')
        if not key:
            from agent.secret_scope import build_profile_secret_scope
            key = build_profile_secret_scope(self.owner.parent.parent).get('HINDSIGHT_API_KEY', '')
        if not isinstance(key, str):
            raise ValueError('Invalid credential type')
        target = SimpleNamespace(api_url=self.policy['api_url'], bank=bank, api_key=key)
        timeout = self.policy['timeout'] * (1 if bank == self.state['identity']['bank'] else .8)
        return Client(target, threading.Event()).request(path, body, timeout=timeout)

    def recall(self, query, *, reflect=False):
        primary = self.check()['bank']
        p = self.policy
        body = {'query': query, 'budget': p['recall_budget']}
        prefix = 'reflect' if reflect else 'recall'
        if p[prefix + '_tags']:
            body.update(tags=p[prefix + '_tags'], tags_match=p[prefix + '_tags_match'])
        if reflect:
            return self.request(primary, '/reflect', body).get('text', '')
        body.update(max_tokens=p['recall_max_tokens'], types=p['recall_types'])
        if p['recall_min_scores']:
            body['min_scores'] = p['recall_min_scores']
        _, banks = bank_sets(p, primary)
        results, seen = [], set()
        with ThreadPoolExecutor(max_workers=len(banks)) as pool:
            pending = [pool.submit(self.request, bank, '/memories/recall', body) for bank in banks]
            for bank, future in zip(banks, pending):
                try:
                    rows = future.result().get('results', [])
                except HTTPError as exc:
                    if exc.code != 404 and bank == primary:
                        raise
                    continue
                except Exception:
                    if bank == primary:
                        raise
                    continue
                kept = [row for row in rows if row.get('text') not in seen and all(
                    isinstance(row.get('scores', {}).get(k), (int, float)) and row['scores'][k] >= v
                    for k, v in p['recall_min_scores'].items())]
                results.extend(kept)
                seen.update(row.get('text') for row in kept if row.get('text'))
        return '\n'.join(row['text'] for row in results if row.get('text'))

    def retain(self, document, content, *, context=None, tags=(), metadata=None, update_mode='replace', transaction=''):
        identity = self.check()
        p = self.policy
        writes, _ = bank_sets(p, identity['bank'])
        for bank in writes:
            key = hashlib.sha256((bank + '\0' + document + '\0' + transaction).encode()).hexdigest()
            item = {'content': content, 'document_id': document, 'update_mode': update_mode,
                    'context': p['retain_context'] if context is None else context,
                    'tags': p['retain_tags'] + list(tags), 'observation_scopes': p['observation_scopes'],
                    'metadata': {'session_id': self.thread_id, 'harness': 'codex', **(metadata or {})}}
            if p['retain_source']:
                item['metadata']['source'] = p['retain_source']
            if p['retain_strategy']:
                item['strategy'] = p['retain_strategy']
            digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
            if transaction and self.state.setdefault('confirmed', {}).get(key) == digest:
                continue
            previous = self.state['pending'].get(key)
            if previous and previous['digest'] != digest:
                raise ValueError('Unresolved prior retain must finish before newer content')
            document_path = '/documents/' + quote(document, safe='')
            if previous:
                pending = previous
            else:
                expected = content
                if update_mode == 'append':
                    try:
                        current = self.request(bank, document_path)
                        old = current.get('original_text') or '[]'
                    except HTTPError as exc:
                        if exc.code != 404:
                            raise
                        old = '[]'
                    old_messages, new_messages = json.loads(old), json.loads(content)
                    if not isinstance(old_messages, list) or not isinstance(new_messages, list):
                        raise ValueError('Conversation document is not a message array')
                    expected = json.dumps(old_messages + new_messages, ensure_ascii=False)
                pending = {'digest': digest, 'operation_id': str(uuid.uuid4()), 'item': item, 'expected': expected}
                self.state['pending'][key] = pending
                self.save()
            already_complete = False
            if previous and p['retain_async']:
                try:
                    status = self.request(bank, '/operations/' + pending['operation_id']).get('status')
                except HTTPError as exc:
                    if exc.code != 404:
                        raise
                    status = 'not_found'
                if status in {'pending', 'processing'}:
                    raise RuntimeError('Retain remains pending; replay this turn later')
                if status in {'failed', 'cancelled'}:
                    self.request(bank, '/operations/' + pending['operation_id'] + '/retry', {})
                    raise RuntimeError('Retain retry queued; replay this turn later')
                already_complete = status == 'completed'
            if not already_complete:
                response = self.request(bank, '/memories', {'async': p['retain_async'],
                    'operation_id': pending['operation_id'], 'items': [pending['item']]})
                if response.get('success') is not True or response.get('bank_id') != bank:
                    raise RuntimeError('Retain acknowledgement mismatch')
                if p['retain_async']:
                    if response.get('operation_id') != pending['operation_id']:
                        raise RuntimeError('Operation receipt mismatch')
                    status = self.request(bank, '/operations/' + pending['operation_id']).get('status')
                    if status != 'completed':
                        raise RuntimeError('Retain remains pending; replay this turn later')
            doc = self.request(bank, document_path)
            actual, expected = doc.get('original_text'), pending['expected']
            if update_mode == 'append' and isinstance(actual, str):
                actual, expected = json.loads(actual), json.loads(expected)
            if doc.get('id') != document or doc.get('bank_id') != bank or actual != expected:
                raise RuntimeError('Retain readback mismatch')
            if transaction:
                self.state['confirmed'][key] = digest
            del self.state['pending'][key]
            self.save()

    def tool(self, name, args):
        self.check()
        if self.policy['memory_mode'] == 'context' and name not in DIAGNOSTICS:
            raise ValueError('Content tools are disabled by context-only memory policy')
        if name not in TOOL_ARGS or not isinstance(args, dict):
            raise ValueError('Unknown tool')
        keys = TOOL_ARGS[name]
        if set(args) - {k.rstrip('?') for k in keys} or any(not isinstance(v, str) or not v for v in args.values()) or any(k not in args for k in keys if not k.endswith('?')):
            raise ValueError('Invalid tool arguments; identity and policy are not tool inputs')
        primary = self.state['identity']['bank']
        if name in {'hindsight_recall', 'hindsight_reflect'}:
            return self.recall(args['query'], reflect=name == 'hindsight_reflect')
        if name == 'hindsight_diagnose':
            return self.state['identity']
        if name == 'hindsight_sync_status':
            from .hindsight_workspace import Target, DOC_GIT, DOC_SURVEY, git_snapshot, survey_due
            identity = self.state['identity']
            target = Target(cwd=Path(identity['cwd']), bank=primary, api_url=identity['api_url'],
                            data_dir=self.owner.parent.parent / 'plugin-data/hindsight-workspace')
            path = target.state_root / 'state.json'
            state = read_json(path) if path.exists() else {}
            if state and any(state.get(k) != identity[k] for k in ('cwd', 'bank', 'api_url')):
                raise ValueError('Collector state identity mismatch')
            snapshot = git_snapshot(target.cwd)
            head = snapshot['head'] if snapshot else ''
            pending = len(self.state['pending']) + len(list(target.state_root.glob('*-pending.json')))
            def present(document, operation):
                if not operation:
                    return False
                try:
                    done = self.request(primary, '/operations/' + quote(operation, safe='')).get('status') == 'completed'
                    doc = self.request(primary, '/documents/' + document)
                except HTTPError as exc:
                    if exc.code == 404:
                        return False
                    raise
                return done and doc.get('bank_id') == primary and doc.get('id') == document and bool(doc.get('original_text'))
            survey = present(DOC_SURVEY, state.get('survey_operation'))
            gitlog = present(DOC_GIT, state.get('git_operation')) if head else False
            current = survey and not survey_due(target.cwd, state.get('survey_head'), head)
            current = current and (not head or (gitlog and state.get('git_head') == head))
            return {'gitlogPresent': gitlog, 'surveyPresent': survey,
                    'git': 'not-git' if snapshot is None else 'tracked',
                    'conversationsIngested': len(self.state['completed']), 'activeOps': pending,
                    'operation_scope': 'receipts known to this shared client, not the whole server queue',
                    'synced': bool(current and pending == 0)}
        if name == 'hindsight_search_knowledge_pages':
            data = self.request(primary, '/knowledge-base/search?' + urlencode({'q': args['query'], 'limit': 3}))
            return [{'page': x['name'], 'page_id': x['id'], 'snippet': x.get('snippet', ''), 'score': x.get('score', 0)} for x in data.get('results', [])]
        if name == 'hindsight_read_knowledge_page':
            return self.request(primary, '/knowledge-base/pages/' + quote(args['page_id'], safe=''))
        if name == 'hindsight_list_knowledge_pages':
            items = []
            def walk(nodes):
                for node in nodes:
                    if node.get('kind') == 'page':
                        items.append({k: node[k] for k in ('id', 'name', 'description') if k in node})
                    walk(node.get('children', []))
            walk(self.request(primary, '/knowledge-base/tree').get('roots', []))
            return {'items': items}
        title = args['title']
        slug = re.sub('[^a-z0-9]+', '-', title.lower()).strip('-') or 'doc'
        if name == 'hindsight_ingest_document':
            self.retain(slug, args['content'], context='ingested document', tags=['source:upload', 'harness:codex'])
            return {'ok': True, 'doc_id': slug}
        fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        journal = self.state.get('initiative')
        if journal and journal['fingerprint'] != fingerprint:
            raise ValueError('Finish the pending initiative before starting another')
        if not journal:
            journal = {'fingerprint': fingerprint, 'document': 'initiative-marker-' + slug[:60] + '-' + uuid.uuid4().hex,
                       'page_id': args.get('relates_to_page_id')}
        page_id = journal['page_id']
        if not page_id:
            roots = self.request(primary, '/knowledge-base/tree').get('roots', [])
            folder = next((x for x in roots if x.get('kind') == 'folder' and x.get('name', '').lower() == 'initiatives'), None)
            if folder is None:
                created = self.request(primary, '/knowledge-base/folders', {'name': 'Initiatives'})
                roots = self.request(primary, '/knowledge-base/tree').get('roots', [])
                folder = next((x for x in roots if x.get('id') == created.get('id') and x.get('name') == 'Initiatives'), None)
                if not folder:
                    raise RuntimeError('Initiative folder readback mismatch')
            existing = next((x for x in folder.get('children', []) if x.get('kind') == 'page' and x.get('name') == title), None)
            if existing:
                page_id = existing['id']
            else:
                response = self.request(primary, '/knowledge-base/pages', {
                    'name': title, 'parent_id': folder['id'], 'tags': ['knowledge:feature-work'],
                    'max_tokens': 4096, 'trigger': {'type': 'manual'},
                    'source_query': f'Summarize the "{title}" initiative: what is being built or changed, why, and its current state. Scope it to workspace {self.state["identity"]["cwd"]} itself, not its dependencies.'})
                page_id = response.get('page_id') or response.get('id')
        if not isinstance(page_id, str) or not page_id:
            raise RuntimeError('Missing initiative page receipt')
        page = self.request(primary, '/knowledge-base/pages/' + quote(page_id, safe=''))
        if page.get('id') != page_id:
            raise RuntimeError('Initiative page readback mismatch')
        journal['page_id'] = page_id
        self.state['initiative'] = journal
        self.save()
        verb = 'Update to an existing initiative' if args.get('relates_to_page_id') else 'New initiative'
        document = journal['document']
        self.retain(document, f'{verb}: {title}. {args["summary"]}',
                    context=f'initiative marker for [[page:{page_id}]]', tags=['knowledge:feature-work'],
                    metadata={'relatedPageId': page_id}, transaction=document)
        del self.state['initiative']
        self.save()
        return {'page_id': page_id}


def completed_turns(path, identity, *, first_turn_id=''):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Transcript must be a regular file')
        text = stream.read(8_000_001)
    if len(text) > 8_000_000:
        raise ValueError('Transcript exceeds bounded parser; no partial ingestion')
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not rows or rows[0].get('type') != 'session_meta':
        raise ValueError('Transcript has no native session metadata')
    meta = rows[0]['payload']
    if meta.get('id') != identity['session_id'] or meta.get('cwd') != identity['cwd']:
        raise ValueError('Foreign transcript')
    turns, messages, turn_id, terminal = [], [], '', set()
    started = not first_turn_id
    filtered = {'plugins.recommendations', 'agents_md.instructions', 'environments.environment_context'}
    for row in rows[1:]:
        p = row.get('payload', {})
        kind = row.get('type')
        if not started:
            if kind != 'turn_context' or p.get('turn_id') != first_turn_id:
                continue
            started = True
        if kind == 'turn_context':
            if p.get('cwd') != identity['cwd']:
                raise ValueError('Transcript changed workspace')
            turn_id = p.get('turn_id', '')
        elif kind == 'response_item' and p.get('type') == 'message' and p.get('role') in {'user', 'assistant'}:
            content = p.get('content', [])
            kinds = p.get('internal_chat_message_metadata_passthrough', {}).get('content_item_kinds', [])
            parts = [c['text'] for i, c in enumerate(content) if isinstance(c.get('text'), str)
                     and not (p['role'] == 'user' and len(kinds) == len(content) and kinds[i] in filtered)]
            value = '\n'.join(parts).strip()
            value = re.sub(r'<hindsight_knowledge>[\s\S]*?</hindsight_knowledge>', '', value).strip()
            if value:
                messages.append({'role': p['role'], 'content': value, **({'timestamp': row['timestamp']} if row.get('timestamp') else {})})
        elif kind == 'event_msg':
            if p.get('type') in {'task_complete', 'turn_complete'}:
                final_id = p.get('turn_id') or turn_id
                if not final_id or (turn_id and final_id != turn_id):
                    raise ValueError('Ambiguous native turn identity')
                if any(m['role'] == 'user' for m in messages) and any(m['role'] == 'assistant' for m in messages):
                    turns.append((final_id, messages))
                terminal.add(final_id)
                messages, turn_id = [], ''
            elif p.get('type') == 'turn_aborted':
                terminal.add(p.get('turn_id') or turn_id)
                messages, turn_id = [], ''
    return turns, terminal


def hook(session, event):
    name, cwd = event.get('hook_event_name'), event.get('cwd')
    canonical_path(cwd)
    if name == 'SubagentStart':
        parent_id, first = event.get('session_id'), event.get('turn_id')
        if event.get('agent_id') != session.thread_id or parent_id == session.thread_id or not isinstance(first, str) or not first:
            raise ValueError('Native child lineage and first turn are required')
        with Session(session.owner, parent_id).locked() as parent:
            parent.check()
        if session.state and (session.state.get('parent_session_id') != parent_id or session.state.get('first_turn_id') != first):
            raise ValueError('Child lineage cannot be rebound')
        session.bind(cwd)
        session.state.update(parent_session_id=parent_id, first_turn_id=first)
        session.save()
        return {}
    if name == 'SessionStart':
        session.bind(cwd)
        if event.get('transcript_path'):
            ingest_completed(session, event)
        return {}
    session.check(cwd)
    p = session.policy
    if name == 'UserPromptSubmit':
        prompt = event.get('prompt', event.get('user_prompt'))
        if not isinstance(prompt, str):
            raise ValueError('Prompt text is missing')
        if event.get('transcript_path'):
            ingest_completed(session, event)
        text = ''
        if p['auto_recall'] and p['memory_mode'] != 'tools':
            query = prompt if p['recall_sync'] else session.state['prefetch_query']
            if query:
                if p['recall_max_input_chars']:
                    query = query[:p['recall_max_input_chars']]
                text = session.recall(query, reflect=p['recall_prefetch_method'] == 'reflect')
        if text:
            header = p['recall_prompt_preamble'] or (
                '# Hindsight Memory (persistent cross-session context)\n'
                'Use this to answer questions about the user and prior sessions. '
                'Do not call tools to look up information that is already present here.')
            text = header + '\n\n' + text
        return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}
    if name not in {'Stop', 'SubagentStop', 'SessionEnd'}:
        raise ValueError('Unsupported hook event')
    if name == 'SubagentStop':
        if session.state.get('parent_session_id') != event.get('session_id') or not event.get('agent_transcript_path'):
            raise ValueError('Native child transcript and lineage are required')
        event = {**event, 'transcript_path': event['agent_transcript_path']}
    ingest_completed(session, event, wait_for_turn=event.get('turn_id') if name in {'Stop', 'SubagentStop'} else None)
    return {}


def finish_batch(session):
    batch = session.state['batch']
    document = 'codex-session-' + hashlib.sha256(session.thread_id.encode()).hexdigest()
    session.retain(document, batch['content'], tags=batch['tags'],
                   update_mode='append', transaction=json.dumps(batch['ids']))
    session.state['completed'].extend(batch['ids'])
    del session.state['batch']
    session.save()


def ingest_completed(session, event, wait_for_turn=None):
    identity, p = session.check(), session.policy
    # Stop must be registered async: native Stop precedes the completion marker.
    deadline = time.monotonic() + 30
    while True:
        turns, terminal = completed_turns(event['transcript_path'], identity, first_turn_id=session.state.get('first_turn_id', ''))
        if not wait_for_turn or wait_for_turn in terminal or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    # Recover the exact submitted batch before cadence can regroup its turns.
    if session.state.get('batch'):
        finish_batch(session)
    fresh = [(tid, messages) for tid, messages in turns if tid not in session.state['completed']]
    if not fresh:
        return
    session.state['prefetch_query'] = next((m['content'] for m in reversed(fresh[-1][1]) if m['role'] == 'user'), '')
    session.save()
    from agent.redact import redact_for_egress
    cadence = p['retain_every_n_turns'] if p['auto_retain'] else 1
    for offset in range(0, len(fresh), cadence):
        batch = fresh[offset:offset + cadence]
        if len(batch) < cadence and event['hook_event_name'] != 'SessionEnd':
            break
        messages = [m for _, turn in batch for m in turn]
        content = json.dumps([{**m, 'content': p['retain_' + ('user' if m['role'] == 'user' else 'assistant') + '_prefix'] + ': ' + m['content']} for m in messages], ensure_ascii=False)
        ids = [tid for tid, _ in batch]
        if p['auto_retain']:
            lineage = ['parent:' + session.state['parent_session_id']] if session.state.get('parent_session_id') else []
            session.state['batch'] = {'ids': ids, 'content': redact_for_egress(content),
                                      'tags': ['session:' + session.thread_id, *lineage]}
            session.save()
            finish_batch(session)
        else:
            session.state['completed'].extend(ids)
            session.save()

