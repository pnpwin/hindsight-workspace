"""Shared, secret-free policy and lexical workspace identity. No implicit cwd."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path, PureWindowsPath
import posixpath
import re
from urllib.parse import urlsplit


DEFAULTS = {
    'mode': 'local_external', 'api_url': 'http://127.0.0.1:9177',
    'bank_id': 'hermes', 'bank_id_template': '{workspace}', 'trusted_project_dirs': [],
    'memory_mode': 'hybrid', 'timeout': 120, 'idle_timeout': 300,
    'mirror_to_own_bank': False, 'additional_banks': [], 'recall_additional_banks': [],
    'recall_budget': 'mid', 'recall_prefetch_method': 'recall',
    'auto_recall': True, 'recall_sync': False, 'recall_max_tokens': 4096,
    'recall_max_input_chars': 2000, 'recall_types': ['world', 'experience', 'observation'],
    'recall_tags': [], 'recall_tags_match': 'any', 'recall_min_scores': {},
    'reflect_tags': [], 'reflect_tags_match': 'any', 'recall_prompt_preamble': '',
    'recall_indicator': True, 'auto_retain': True, 'retain_cron': False,
    'retain_every_n_turns': 1, 'retain_async': True, 'retain_indicator': True,
    'retain_context': 'conversation between Hermes Agent and the User',
    'retain_tags': [], 'retain_source': '', 'retain_strategy': '',
    'retain_user_prefix': 'User', 'retain_assistant_prefix': 'Assistant',
    'observation_scopes': 'shared', 'prefetch_waits_for_retain': True,
    'prefetch_retain_drain_timeout': 10.0,
}
ALIASES = {'budget': 'recall_budget', 'prefetch_method': 'recall_prefetch_method',
           'recallAdditionalBanks': 'recall_additional_banks'}
SECRET_KEYS = {'apiKey', 'api_key', 'api_token'}
BANK_PATTERN = r'[A-Za-z0-9](?:[A-Za-z0-9_:-]*[A-Za-z0-9])?'


def canonical_path(cwd: str | os.PathLike | None) -> tuple[str, str]:
    raw = os.fspath(cwd) if cwd is not None else ''
    if not isinstance(raw, str) or not raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError('A declared absolute session cwd is required; no fallback bank')
    if PureWindowsPath(raw).is_absolute():
        return 'windows:', ntpath.normcase(ntpath.normpath(raw))
    if posixpath.isabs(raw):
        return 'posix:', posixpath.normpath(raw)
    raise ValueError('A declared absolute session cwd is required; no fallback bank')


def resolve_workspace(explicit: str, cwd: str | os.PathLike | None) -> str:
    # Preserve the old explicit-workspace and path-hash algorithm byte for byte.
    if explicit and explicit != 'hermes':
        if not isinstance(explicit, str) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?', explicit):
            raise ValueError('Invalid explicit workspace')
        return explicit
    namespace, canonical = canonical_path(cwd)
    if namespace == 'posix:' and canonical == posixpath.normpath(str(Path.home())):
        return 'home'
    basename = ntpath.basename(canonical) if namespace == 'windows:' else posixpath.basename(canonical)
    slug = re.sub(r'[^A-Za-z0-9]+', '-', basename).strip('-')[:48] or 'workspace'
    digest = hashlib.sha256((namespace + canonical).encode('utf-8')).hexdigest()[:16]
    return f'{slug}-{digest}'


def string_list(value) -> list[str]:
    if value is None or value == '':
        return []
    if isinstance(value, str):
        value = json.loads(value) if value.lstrip().startswith('[') else value.split(',')
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError('Expected a list of strings')
    return [x.strip() for x in value if x.strip()]


def _bank(value):
    if not isinstance(value, str) or not re.fullmatch(BANK_PATTERN, value):
        raise ValueError('Invalid bank identifier')
    return value


def normalize_policy(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('Policy must be an object')
    raw = copy.deepcopy(raw)
    for alias, key in ALIASES.items():
        if alias in raw:
            if key in raw and raw[key] != raw[alias]:
                raise ValueError(f'Conflicting policy aliases: {alias}, {key}')
            raw[key] = raw.pop(alias)
    if 'workspace_from_cwd' in raw:
        if type(raw.pop('workspace_from_cwd')) is not bool:
            raise ValueError('workspace_from_cwd must be a JSON boolean')
    for key in SECRET_KEYS:
        raw.pop(key, None)  # Authentication remains owned by the native provider.
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError('Unsupported policy fields: ' + ', '.join(sorted(unknown)))
    policy = {**copy.deepcopy(DEFAULTS), **raw}
    for key, default in DEFAULTS.items():
        value = policy[key]
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ValueError(f'{key} must be a JSON boolean')
        elif isinstance(default, (int, float)):
            if isinstance(value, bool):
                raise ValueError(f'{key} must be numeric')
            try:
                numeric = float(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f'{key} must be numeric') from exc
            minimum = 0 if key in {'recall_max_input_chars', 'prefetch_retain_drain_timeout'} else 1
            if not math.isfinite(numeric) or numeric < minimum or (isinstance(default, int) and not numeric.is_integer()):
                raise ValueError(f'Invalid {key}')
            policy[key] = int(numeric) if isinstance(default, int) else numeric
        elif isinstance(default, list):
            policy[key] = string_list(value)
        elif isinstance(default, str) and key != 'observation_scopes':
            if not isinstance(value, str):
                raise ValueError(f'{key} must be text')
    if policy['mode'] != 'local_external' or policy['bank_id_template'] != '{workspace}':
        raise ValueError('Shared memory requires local_external and {workspace}')
    if policy['retain_async'] is not True:
        raise ValueError('Shared capture requires retain_async=true: server operation IDs are ignored for synchronous retain')
    if policy['trusted_project_dirs']:
        raise ValueError('Repository overrides cannot redirect shared memory')
    choices = {'memory_mode': {'hybrid', 'tools', 'context'}, 'recall_budget': {'low', 'mid', 'high'},
               'recall_prefetch_method': {'recall', 'reflect'},
               'recall_tags_match': {'any', 'all', 'any_strict', 'all_strict'},
               'reflect_tags_match': {'any', 'all', 'any_strict', 'all_strict'}}
    for key, options in choices.items():
        if policy[key] not in options:
            raise ValueError(f'Invalid {key}')
    url = urlsplit(policy['api_url'])
    if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(c.isspace() for c in policy['api_url'])):
        raise ValueError('api_url must be an HTTP(S) endpoint without credentials/query/fragment')
    if url.port is not None and not 0 < url.port < 65536:
        raise ValueError('Invalid API port')
    policy['api_url'] = policy['api_url'].rstrip('/')
    _bank(policy['bank_id'])
    for key in ('additional_banks', 'recall_additional_banks'):
        policy[key] = list(dict.fromkeys(_bank(x) for x in policy[key]))
    policy['recall_types'] = policy['recall_types'] or ['observation']
    if set(policy['recall_types']) - {'world', 'experience', 'observation'}:
        raise ValueError('Invalid recall types')
    scores = policy['recall_min_scores']
    if isinstance(scores, str):
        scores = json.loads(scores) if scores else {}
    if scores is None:
        scores = {}
    if not isinstance(scores, dict) or any(not isinstance(k, str) or isinstance(v, bool)
            or not isinstance(v, (int, float)) or not math.isfinite(v) for k, v in scores.items()):
        raise ValueError('Invalid recall score floors')
    policy['recall_min_scores'] = {k: float(v) for k, v in scores.items()}
    scopes = policy['observation_scopes']
    if isinstance(scopes, str) and scopes.lstrip().startswith('['):
        scopes = json.loads(scopes)
    if isinstance(scopes, list):
        if scopes and all(isinstance(tag, str) for tag in scopes):
            scopes = [scopes]
        if not scopes or any(not isinstance(scope, list) or not string_list(scope) for scope in scopes):
            raise ValueError('Invalid observation scopes')
        scopes = [string_list(scope) for scope in scopes]
    elif scopes not in {'shared', 'combined', 'per_tag', 'all_combinations'}:
        raise ValueError('Invalid observation scopes')
    policy['observation_scopes'] = scopes
    for key in ('retain_user_prefix', 'retain_assistant_prefix'):
        policy[key] = policy[key].strip() or DEFAULTS[key]
    return policy


def load_policy(path: Path) -> dict:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('An explicit absolute policy path is required')
    with path.open(encoding='utf-8') as stream:
        text = stream.read(131073)
    if len(text) > 131072:
        raise ValueError('Policy exceeds 128 KiB')
    return normalize_policy(json.loads(text))


def hermes_projection(policy: dict) -> dict:
    return normalize_policy(policy)


def policy_hash(policy: dict) -> str:
    data = json.dumps(normalize_policy(policy), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def bank_sets(policy: dict, primary: str) -> tuple[list[str], list[str]]:
    writes = [primary]
    if policy['mirror_to_own_bank']:
        writes.append(policy['bank_id'])
    writes = list(dict.fromkeys(writes + policy['additional_banks']))
    reads = list(dict.fromkeys(writes + policy['recall_additional_banks']))
    return writes, reads


def resolve_identity(*, session_id: str, cwd: str | os.PathLike | None, selected_cwd: str | os.PathLike | None, policy: dict, explicit: str = '') -> dict:
    if (not isinstance(session_id, str) or not session_id or len(session_id) > 512
            or any(ord(c) < 33 or ord(c) == 127 for c in session_id)):
        raise ValueError('A valid native session identity is required')
    namespace, canonical = canonical_path(cwd)
    if (namespace, canonical) != canonical_path(selected_cwd):
        raise ValueError('Selected workspace and session cwd disagree')
    workspace = resolve_workspace(explicit, canonical)
    # This is the upstream {workspace} renderer, including legacy explicit IDs.
    bank = re.sub(r'([-_])\1+', r'\1', workspace).strip('-_')
    return {'session_id': session_id, 'cwd': canonical, 'workspace': workspace, 'explicit': explicit,
            'bank': bank, 'api_url': policy['api_url'], 'policy_hash': policy_hash(policy)}


def validate_binding(binding: dict, policy: dict, *, session_id: str, cwd: str) -> None:
    expected = resolve_identity(session_id=session_id, cwd=cwd, selected_cwd=binding.get('cwd'),
                                policy=policy, explicit=binding.get('explicit', ''))
    if binding != expected:
        raise ValueError('Stale or conflicting session binding; start a new bound session')
