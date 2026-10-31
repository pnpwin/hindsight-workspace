"""Workspace binding before the unmodified native Hindsight lifecycle."""
from __future__ import annotations

import copy
import functools
import inspect
import json
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret
from hermes_constants import get_hermes_home
from plugins.memory import find_provider_dir, load_memory_provider
from .policy import (DEFAULTS, canonical_path, hermes_projection, load_policy,
                     normalize_policy, policy_hash, resolve_identity, validate_binding)


ENV_FIELDS = {
    'HINDSIGHT_BANK_ID': 'bank_id', 'HINDSIGHT_RETAIN_TAGS': 'retain_tags',
    'HINDSIGHT_RETAIN_OBSERVATION_SCOPES': 'observation_scopes',
    'HINDSIGHT_RETAIN_SOURCE': 'retain_source', 'HINDSIGHT_RETAIN_STRATEGY': 'retain_strategy',
    'HINDSIGHT_RETAIN_USER_PREFIX': 'retain_user_prefix',
    'HINDSIGHT_RETAIN_ASSISTANT_PREFIX': 'retain_assistant_prefix',
}


def configuration(home: Path):
    if not home.is_absolute() or home != get_hermes_home():
        raise ValueError('Provider home must match the active native profile scope')
    raw = json.loads((home / 'hindsight-workspace/config.json').read_text())
    if set(raw) != {'policy_path'} or not isinstance(raw['policy_path'], str):
        raise ValueError('Configure one explicit policy_path for hindsight-workspace')
    path = Path(raw['policy_path'])
    policy = load_policy(path)
    native = json.loads((home / 'hindsight/config.json').read_text())
    if set(DEFAULTS) - set(native) or normalize_policy(native) != policy:
        raise ValueError('Native projection is incomplete or differs from the policy owner; run migration dry-run')
    for env, key in ENV_FIELDS.items():
        value = get_secret(env, '')
        if value and normalize_policy({**policy, key: value}) != policy:
            raise ValueError(f'Conflicting native environment setting: {env}')
    return path, policy


class WorkspaceMemoryProvider(MemoryProvider):
    def __init__(self):
        self._delegate = None
        self._identity: dict[str, Any] | None = None
        self._home: Path | None = None
        self._profile = ''
        self._policy_path = None
        self._policy: dict[str, Any] | None = None
        self._reason = ''
        self._closed = False

    @property
    def name(self):
        return 'hindsight-workspace'

    @property
    def workspace_identity(self):
        return copy.deepcopy(self._identity)

    @property
    def workspace_policy(self):
        return copy.deepcopy(self._policy)

    @property
    def bound_home(self):
        return self._home

    @property
    def bound_profile(self):
        return self._profile

    @property
    def policy_path(self):
        return self._policy_path

    def _native(self):
        from .maintenance.clean_gate import verify_provider
        root = find_provider_dir('hindsight')
        if root is None:
            raise ValueError('The official hindsight provider is not installed')
        verify_provider(root)
        if self._delegate is None:
            candidate = load_memory_provider('hindsight', register_skills=False)
            if candidate is None or candidate.name != 'hindsight':
                raise ValueError('The official hindsight provider is not available')
            self._delegate = candidate
        if Path(inspect.getfile(type(self._delegate))) != Path(root) / '__init__.py':
            raise ValueError('Native provider origin differs from the approved source')
        return self._delegate

    def is_available(self):
        try:
            configuration(get_hermes_home())
            return self._native().is_available()
        except (OSError, ValueError, ImportError) as exc:
            self._reason = f'Workspace policy not ready ({type(exc).__name__}); inspect migration dry-run'
            return False

    def unavailable_reason(self):
        return self._reason or (self._delegate.unavailable_reason() if self._delegate else '')

    def initialize(self, session_id, **kwargs):
        if self._identity is not None or self._closed:
            raise ValueError('Use a new provider instance for a new workspace/profile')
        # Missing native identity must fail before even loading the upstream instance.
        canonical_path(kwargs.get('cwd'))
        home = Path(kwargs.get('hermes_home') or get_hermes_home())
        path, policy = configuration(home)
        identity = resolve_identity(session_id=session_id, cwd=kwargs.get('cwd'),
                                    selected_cwd=kwargs.get('selected_cwd', kwargs.get('cwd')),
                                    explicit=kwargs.get('agent_workspace') or '', policy=policy)
        profile = kwargs.get('agent_identity') or ''
        if not isinstance(profile, str) or not profile or profile == 'custom':
            raise ValueError('A native profile identity is required')
        context = {**kwargs, 'cwd': identity['cwd'], 'agent_workspace': identity['workspace']}
        context.pop('selected_cwd', None)
        self._native().initialize(session_id, **context)
        self._home, self._profile = home, profile
        self._policy_path, self._policy, self._identity = path, policy, identity

    def _guard(self, kwargs):
        if self._identity is None or self._home is None or self._closed:
            raise ValueError('Memory provider is not bound to an active session')
        if kwargs.get('hermes_home', str(self._home)) != str(self._home):
            raise ValueError('Cross-profile memory call refused')
        if kwargs.get('agent_identity', self._profile) != self._profile:
            raise ValueError('Cross-profile memory call refused')
        path, current = configuration(self._home)
        if path != self._policy_path:
            raise ValueError('Policy owner changed; a new session is required')
        validate_binding(self._identity, current,
                         session_id=kwargs.get('session_id') or self._identity['session_id'],
                         cwd=kwargs.get('cwd') or self._identity['cwd'])

    def _invoke(self, method, args, kwargs):
        function = getattr(self._native(), method)
        params = inspect.signature(function).parameters
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in params}
        return function(*args, **kwargs)

    def get_tool_schemas(self):
        return self._invoke('get_tool_schemas', (), {})

    def on_session_switch(self, new_session_id, **kwargs):
        self._guard(kwargs)
        assert self._identity is not None and self._policy is not None
        new = resolve_identity(session_id=new_session_id, cwd=self._identity['cwd'],
                               selected_cwd=self._identity['cwd'], policy=self._policy,
                               explicit=self._identity['explicit'])
        self._invoke('on_session_switch', (new_session_id,), kwargs)
        self._identity = new

    def identity_signature(self):
        path, policy = configuration(get_hermes_home())
        result = self._native().identity_signature()
        return {**result, 'hindsight-workspace': {'policy_path': str(path), 'policy_hash': policy_hash(policy)}}

    def shutdown(self):
        if not self._closed:
            self._closed = True
            # Cleanup also drains turns admitted before a policy change; never rebind them.
            if self._delegate is not None:
                self._delegate.shutdown()


# Forward the whole ABC, not a hand-maintained subset that drops new lifecycle hooks.
def _forward(name, contract):
    @functools.wraps(contract)
    def call(self, *args, **kwargs):
        if name not in {'get_config_schema', 'save_config', 'backup_paths', 'get_tool_schemas'}:
            self._guard(kwargs)
        return self._invoke(name, args, kwargs)
    setattr(call, '__isabstractmethod__', False)
    return call


for _name, _contract in vars(MemoryProvider).items():
    if not _name.startswith('_') and inspect.isfunction(_contract) and _name not in vars(WorkspaceMemoryProvider):
        setattr(WorkspaceMemoryProvider, _name, _forward(_name, _contract))

from abc import update_abstractmethods
update_abstractmethods(WorkspaceMemoryProvider)
