"""Explicit cursor adoption: GET-only verification, exact-plan apply, no legacy deletion."""
from __future__ import annotations

from contextlib import ExitStack
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from urllib.error import HTTPError
from urllib.parse import quote
import uuid

from hindsight_workspace import Client, DOC_GIT, DOC_SURVEY, atomic_json, workspace_lock


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Expected a regular receipt')
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Receipt exceeds limit')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _plan(target, roots):
    identity = {'cwd': str(target.cwd), 'bank': target.bank, 'api_url': target.api_url}
    client, inputs, states, blockers, operations = Client(target, threading.Event()), {}, [], [], {}
    def status(operation):
        uuid.UUID(operation)
        if operation not in operations:
            try:
                operations[operation] = client.request('/operations/' + quote(operation, safe='')).get('status')
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                exc.close()
                operations[operation] = 'not_found'
        return operations[operation]
    for root in roots:
        if not root.is_absolute() or root.resolve() != root or root.name != target.state_root.name:
            raise ValueError('Legacy root must be the same workspace key without symlinks')
        if not root.exists() and root != target.state_root:
            raise ValueError('Explicit legacy root is missing')
        path = root / 'state.json'
        inputs[str(path)] = None
        if path.exists():
            state, inputs[str(path)] = _read(path)
            if any(state.get(k) != v for k, v in identity.items()):
                raise ValueError('Foreign legacy identity')
            states.append(state)
        for path in sorted(root.glob('*')):
            if path.name in {'state.json', 'last-run.json', 'lock'}:
                continue
            if not path.name.endswith('-pending.json'):
                raise ValueError('Unknown legacy receipt; inspect before migration')
            pending, inputs[str(path)] = _read(path)
            operation = pending.get('operation_id')
            state = status(operation) if operation else 'unsubmitted-survey'
            # Even a completed pending journal belongs to the old recovery path.
            blockers.append({'path': str(path), 'status': state})
    proposed, documents = dict(identity), {}
    for kind, document in (('git', DOC_GIT), ('survey', DOC_SURVEY)):
        candidates = [s for s in states if s.get(kind + '_operation')]
        if not candidates:
            continue
        try:
            doc = client.request('/documents/' + document)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            exc.close()
            doc = {}
        text = doc.get('original_text', '')
        matching = []
        for state in candidates:
            operation, head = state[kind + '_operation'], state.get(kind + '_head')
            completed = status(operation) == 'completed'
            if kind == 'survey':
                prefix = f'Workspace survey: {target.cwd}\nBaseline HEAD: {head or "non-Git/unborn"}\n\n'
                matches = isinstance(head, str) and isinstance(text, str) and text.startswith(prefix)
            else:
                matches = isinstance(text, str) and text.startswith(f'Git commit messages for workspace {target.cwd}\n') and '\n\n' in text and text.split('\n\n', 1)[1].split(' ', 1)[0] == head
            if completed and matches and doc.get('bank_id') == target.bank and doc.get('id') == document:
                matching.append((head, operation))
        if not matching:
            blockers.append({'document': document, 'status': 'no-readback-confirmed-cursor'})
            continue
        head, operation = sorted(matching)[0]
        proposed.update({kind + '_head': head, kind + '_operation': operation})
        documents[document] = hashlib.sha256(text.encode()).hexdigest()
    return {'version': 1, 'target': str(target.state_root), 'roots': [str(r) for r in roots],
            'inputs': inputs, 'documents': documents, 'operations': operations,
            'state': proposed, 'blockers': blockers, 'ready': not blockers}


def adopt(target, legacy_roots, *, apply=False, approved_plan=None):
    """Call only after stopping legacy writers; roots must enumerate the complete migration scope."""
    roots = sorted({target.state_root, *(Path(p) for p in legacy_roots)})
    plan = _plan(target, roots)
    if not apply:
        return plan
    if not plan['ready'] or plan != approved_plan:
        raise ValueError('Adoption requires an unchanged, read-back-verified approved plan')
    with ExitStack() as locks:
        for root in roots:
            if not locks.enter_context(workspace_lock(dataclasses.replace(target, data_dir=root.parent))):
                raise ValueError('Legacy or shared writer is still active')
        checked = _plan(target, roots)
        if checked != plan:
            raise ValueError('Receipts or documents changed after approval')
        atomic_json(target.state_root / 'state.json', checked['state'])
    return {**plan, 'applied': True}
