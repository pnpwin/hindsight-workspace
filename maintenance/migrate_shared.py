"""Apply a reviewed, staged file transition only while clients are stopped.

Default is dry-run. Generate YAML/TOML candidates with the native client writers
in an isolated home first; this transaction never edits payloads, databases,
credentials, dependency environments or services. Backups are not DB rollback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

ALLOWED = {'config.yaml', 'config.toml', 'hooks.json', 'hindsight/config.json',
           'hindsight-workspace/config.json', 'hindsight-workspace.json',
           *(f'plugin-data/hindsight/maintenance/{name}.py' for name in
             ('workspace_guard', 'candidate_gate', 'pin_monitor', 'update'))}


def regular(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Expected an absolute, non-symlink path')
    if path.name == '.env' or path.suffix in {'.db', '.sqlite', '.sqlite3'}:
        raise ValueError('Databases and credentials are not migration inputs')
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Expected a regular file')
        data = stream.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError('File exceeds migration limit')
    return data


def digest(path):
    data = regular(path)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def write(path, content, mode=0o600):
    path = Path(path)
    regular(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.migration-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def scope(record):
    roots = [Path(p) for p in record['roots']]
    if not roots or any(not p.is_absolute() or p.resolve() != p for p in roots):
        raise ValueError('Explicit non-symlink roots required')
    names = [c['target'] for c in record['changes']]
    if not names or len(set(names)) != len(names):
        raise ValueError('Empty or duplicate transition')
    for change in record['changes']:
        target = Path(change['target'])
        if not any(target.is_relative_to(root) and target.relative_to(root).as_posix() in ALLOWED for root in roots):
            raise ValueError('Target outside migration allowlist')
    backup_root = Path(record['backup_root'])
    if not backup_root.is_absolute() or backup_root.resolve() != backup_root:
        raise ValueError('Invalid backup root')


def validate(record):
    scope(record)
    for change in record['changes']:
        target = Path(change['target'])
        if digest(target) != change['before']:
            raise ValueError('Consumer changed since review')
        if digest(change['source']) != change['after']:
            raise ValueError('Staged candidate changed since review')
    for path, expected in record['protected'].items():
        if digest(path) != expected:
            raise ValueError('Protected source/config/state changed since review')
    backup_root = Path(record['backup_root'])
    if not backup_root.is_absolute() or backup_root.resolve() != backup_root:
        raise ValueError('Invalid backup root')


def plan(proposal):
    result = {**proposal, 'version': 1,
              'changes': [{**c, 'after': digest(c['source']),
                           'before_mode': Path(c['target']).stat().st_mode & 0o777 if digest(c['target']) is not None else None}
                          for c in proposal['changes']]}
    if any(c['after'] is None for c in result['changes']):
        raise ValueError('Staged payload missing')
    validate(result)
    return result


def backup(record):
    folder = Path(record['backup_root']) / str(uuid.uuid4())
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    backups, hashes = {}, {}
    for i, path in enumerate(sorted({*record['protected'], *(c['target'] for c in record['changes'])})):
        content = regular(path)
        hashes[path] = hashlib.sha256(content).hexdigest() if content is not None else None
        if content is not None:
            saved = folder / str(i)
            write(saved, content)
            backups[path] = str(saved)
    receipt = {**record, 'backups': backups, 'backup_hashes': hashes,
               'receipt_path': str(folder / 'receipt.json'), 'status': 'prepared'}
    write(receipt['receipt_path'], json.dumps(receipt, indent=2).encode())
    return receipt


@contextmanager
def mutation_scope(record):
    scope(record)
    with ExitStack() as locks:
        for root in sorted(set(record['roots'])):
            fd = os.open(Path(root) / '.hindsight-migration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            locks.callback(os.close, fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Migration lock must be regular')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def rollback(receipt):
    with mutation_scope(receipt):
        return _rollback(receipt)


def _rollback(receipt):
    scope(receipt)
    location = Path(receipt['receipt_path'])
    folder = Path(receipt['backup_root'])
    if location.parent.parent != folder or location.name != 'receipt.json' or str(uuid.UUID(location.parent.name)) != location.parent.name:
        raise ValueError('Invalid backup receipt location')
    if any(Path(p).parent != location.parent or not Path(p).name.isdigit() for p in receipt['backups'].values()):
        raise ValueError('Backup outside receipt directory')
    # Locks serialize our writers; unrelated writers must still be stopped.
    restores = []
    for change in receipt['changes']:
        if not {'before', 'after', 'before_mode', 'target', 'source'} <= change.keys():
            raise ValueError('Incomplete rollback entry')
        before, after, mode = change['before'], change['after'], change['before_mode']
        for value in (before, after):
            if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)):
                raise ValueError('Invalid rollback digest')
        if after is None or (before is None and mode is not None) or (before is not None and (type(mode) is not int or not 0 <= mode <= 0o777)):
            raise ValueError('Invalid rollback mode or digest')
        observed = digest(change['target'])
        if observed not in {before, after}:
            raise ValueError('Newer write prevents rollback; retain backups for reconciliation')
        saved = receipt['backups'].get(change['target'])
        content = regular(saved) if saved else None
        if (before is not None and (content is None or hashlib.sha256(content).hexdigest() != before)) or (before is None and saved is not None):
            raise ValueError('Missing or corrupt backup')
        restores.append((change, observed, content))
    for change, observed, content in reversed(restores):
        path = Path(change['target'])
        if digest(path) != observed:
            raise ValueError('Concurrent change during rollback; retain backups')
        if change['before'] is None:
            path.unlink(missing_ok=True)
        else:
            write(path, content, change['before_mode'])
        if digest(path) != change['before']:
            raise ValueError('Rollback readback mismatch')
    receipt = {**receipt, 'status': 'rolled-back'}
    write(receipt['receipt_path'], json.dumps(receipt, indent=2).encode())
    return receipt


def apply(record):
    with mutation_scope(record):
        return _apply(record)


def _apply(record):
    validate(record)
    receipt = backup(record)
    validate(record)
    for change in record['changes']:
        if receipt['backup_hashes'][change['target']] != change['before']:
            raise ValueError('Concurrent change during backup')
    try:
        for change in record['changes']:
            if digest(change['target']) != change['before']:
                raise ValueError('Concurrent change during publication')
            content = regular(change['source'])
            if content is None or hashlib.sha256(content).hexdigest() != change['after']:
                raise ValueError('Staged payload changed during publication')
            write(change['target'], content, 0o700 if (change['before_mode'] or 0) & 0o100 else 0o600)
        if any(digest(c['target']) != c['after'] for c in record['changes']):
            raise ValueError('Publication readback mismatch')
    except Exception:
        _rollback(receipt)
        raise
    receipt = {**receipt, 'status': 'applied'}
    write(receipt['receipt_path'], json.dumps(receipt, indent=2).encode())
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('record', type=Path, help='proposal for dry-run; reviewed plan for --apply; receipt for --rollback')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--rollback', action='store_true')
    parser.add_argument('--clients-stopped', action='store_true')
    args = parser.parse_args()
    if (args.apply or args.rollback) and not args.clients_stopped:
        parser.error('Stop every old/new client and updater before applying or rolling back')
    raw = regular(args.record)
    if raw is None:
        parser.error('Migration record does not exist')
    record = json.loads(raw)
    result = apply(record) if args.apply else rollback(record) if args.rollback else plan(record)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
