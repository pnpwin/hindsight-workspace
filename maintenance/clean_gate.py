"""One fail-closed gate for clean-provider monitoring and managed publication.

The operator saves an explicit approval snapshot after native admission/review.
Neither monitoring nor scheduled dispatch regenerates approval or restores an
old overlay. Publication is delegated to the native manager, never file copies.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import sys

SOURCE = 'https://github.com/vectorize-io/hindsight.git#hindsight-integrations/hermes'
REVISIONS = ('d952b6e655fccab4581ee0722b51c5f103873d12', 'a5a619b5ca59c5c39e0f96be6f7a31fabd136bd5')
UPSTREAM = {
    '__init__.py': '1b1436b80e271be8915a743e696fd63e590ab0938ffff5fb581fb1882cf1a10a',
    'settings.py': 'cc922b593c99e9740bc49a1ec4e54824976307a1ce684f030f1f0d185a7dbded',
    'embedded.py': 'f8abd9a1ae70259524eb2d58c85335f14171f0974141e57313fe2192fda31a48',
    'setup.py': 'a592e2e5e31b598e30facb96311356075eb7469880b525f6444b720eb1441501',
    'templates.py': 'eac05ef641b5c8fa9610b9b959f32db8f340e1423d5363ebc88b601cb84b600e',
    'config_schema.py': '4d53c720aa7c129b5640d7c08ce67482ff21ce81c90d7bde1bc052ad90eeed1d',
    'pyproject.toml': 'ba9ae36f06f9475c3b70d84e8fced795539ca56b7a592fdf8fc9beddc3236602',
    'plugin.yaml': 'f46da374c184400ed70a12ce61529416134b3575d4bec71364bb4f98bcf5a809',
}
REQUIRED = {'__init__.py', 'provider.py', 'policy.py', 'hindsight_workspace.py',
            'survey_route.py', 'cli.py', 'codex_memory.py', 'plugin.yaml'}
UPSTREAM_TESTS = {
    'tests/conftest.py': '80387f0ba25b87b600845bae8d4372595ea3a7a8dcdafc9ba1925a24b1e75857',
    'tests/test_settings.py': '576381d48af1e2c64b275ba827c695182aa4164c5fe2b777326182e64bd399d0',
    'tests/test_env_settings.py': '78c9527006e7edc8354cfd09e83058f4a94a9b93b34f6b670d257e483ddc4b93',
    'tests/test_provider.py': '5fae677c59721e9ccd37e7f1de50d9790a3cefde1021071d229ebbf20a3e07a8',
    'tests/test_local_runtime.py': '316ca9e591b98ce933c5974307354b8f30a3dc10298022ce0f100c063537fa66',
    'tests/test_multibank.py': 'c93d5ce596a75d8c8792afa789907b2d4cecd04dff9d7cb628204f28867978ea',
}


def sha(path):
    path = Path(path)
    if path.resolve() != path or not path.is_file() or path.is_symlink():
        raise ValueError('Expected a regular, non-symlink approved file')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_bytecode(root, path):
    source = Path(importlib.util.source_from_cache(str(path)))
    approved = {**UPSTREAM, **UPSTREAM_TESTS}.get(source.relative_to(root).as_posix())
    if approved is None or sha(source) != approved:
        raise ValueError('Unknown bytecode source')
    filename = str(source)
    optimize = next((level for level in (0, 1, 2) if Path(importlib.util.cache_from_source(
        filename, optimization=str(level) if level else '')) == path), None)
    if optimize is None:
        raise ValueError('Unknown bytecode interpreter or optimization')
    code = compile(source.read_bytes(), filename, 'exec', dont_inherit=True, optimize=optimize)
    # Match the cache writer's retained code reference and marshal reference flags.
    retained = (code,)
    expected = marshal.dumps(retained[0])
    # Compare serialized known code; never deserialize an untrusted cache.
    with path.open('rb') as stream:
        data = stream.read(len(expected) + 17)
    if (data[:4] != importlib.util.MAGIC_NUMBER or int.from_bytes(data[4:8], 'little') not in (0, 1, 3)
            or data[16:] != expected):
        raise ValueError('Unapproved bytecode differs from approved source')


def verify_provider(root):
    if sys.pycache_prefix is not None:
        raise ValueError('External cache prefix is not approved; use the native local cache layout')
    root = Path(root)
    actual = {name: sha(root / name) for name in UPSTREAM}
    if actual != UPSTREAM:
        raise ValueError('Unknown upstream source or legacy workspace overlay')
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError('Symlink in approved provider tree')
        if path.is_file() and path.suffix in {'.py', '.pyc', '.pyo', '.so', '.pyd', '.dylib'}:
            if path.suffix == '.pyc' and path.parent.name == '__pycache__':
                verify_bytecode(root, path)
                continue
            if name not in UPSTREAM and UPSTREAM_TESTS.get(name) != sha(path):
                raise ValueError('Unknown importable upstream source')
            actual[name] = sha(path)
    return actual


def snapshot(home, *, consumers=()):
    home = Path(home)
    if not home.is_absolute() or home.resolve() != home:
        raise ValueError('Explicit native home required')
    metadata_path = home / 'plugins/.install-metadata.json'
    record = json.loads(metadata_path.read_text())['hindsight']
    if record.get('source') != SOURCE or record.get('revision') not in REVISIONS or record.get('pinned') is not True:
        raise ValueError('Unknown upstream source/pin')
    provider = home / 'plugins/hindsight'
    inventory = verify_provider(provider)
    adapter = home / 'plugins/hindsight-workspace'
    files = [p for p in adapter.rglob('*') if p.is_file() and not set(p.relative_to(adapter).parts) & {'.git', '__pycache__'}]
    if not REQUIRED <= {p.relative_to(adapter).as_posix() for p in files}:
        raise ValueError('Incomplete workspace adapter')
    # Import the already-admitted gate's policy, never an arbitrary installed module.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from policy import load_policy, policy_hash
    selector = home / 'hindsight-workspace/config.json'
    sha(selector)
    selected = json.loads(selector.read_text())
    if set(selected) != {'policy_path'} or not isinstance(selected['policy_path'], str):
        raise ValueError('Expected one explicit policy owner')
    policy_path = Path(selected['policy_path'])
    policy = load_policy(policy_path)
    if load_policy(home / 'hindsight/config.json') != policy:
        raise ValueError('Native policy and approved owner disagree')
    files += [provider / name for name in inventory]
    files += [policy_path, metadata_path, selector, home / 'config.yaml', home / 'hindsight/config.json',
              policy_path.parent.parent / 'config.yaml']
    consumers = sorted({str(Path(p)) for p in consumers})
    files += [Path(p) for p in consumers]
    return {'files': {str(p): sha(p) for p in sorted(set(files))}, 'consumers': consumers, 'policy_hash': policy_hash(policy),
            'revision': record['revision'], 'home': str(home)}


def monitor(home, approved):
    if snapshot(home, consumers=approved['consumers']) != approved:
        raise ValueError('Approval no longer matches payload/config/metadata')
    return {'status': 'ok', 'changed': False, 'revision': approved['revision']}


def dispatch(home, approved, candidate, *, publisher, scheduled):
    monitor(home, approved)
    if scheduled:
        return {'status': 'held-pinned', 'changed': False}
    verify_provider(candidate)
    monitor(home, approved)
    publisher(Path(candidate))
    # The tested transition keeps the reviewed pin; a new pin needs new approval.
    monitor(home, approved)
    return {'status': 'verified', 'changed': False, 'revision': approved['revision']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--snapshot', action='store_true', help='print a candidate for EXPLICIT operator approval')
    parser.add_argument('--consumer-config', type=Path, action='append', default=[], help='explicit external consumer roster; never scan HOME')
    args = parser.parse_args()
    if args.snapshot:
        result = snapshot(args.home, consumers=args.consumer_config)
    elif args.approval:
        result = monitor(args.home, json.loads(args.approval.read_text()))
    else:
        parser.error('Specify --approval for monitoring or explicit --snapshot')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
