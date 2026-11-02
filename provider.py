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

_MISSING = object()


def _native_turn_context(*, kwargs=None, expected=None, explicit_cwd=_MISSING, required=False):
    """Read the gateway's declared, task-local workspace and route when one is active.

    A missing cwd may be recovered only from this complete native context.  The gateway
    route key is carried independently of the durable session id and remains stable when
    Hermes rotates that id during compression or a session switch.
    """
    try:
        from gateway.session_policy import active_policy
    except ImportError as exc:
        if required:
            raise ValueError('A native session context is required when cwd is omitted') from exc
        return None

    policy = active_policy()
    if policy is None:
        if required:
            raise ValueError('An active native session policy is required when cwd is omitted')
        return None

    try:
        from agent.runtime_cwd import scoped_session_cwd
        from gateway.session_context import bound_session_key
        from hermes_cli.profiles import get_active_profile_name
        from hermes_constants import get_hermes_home
    except ImportError as exc:
        raise ValueError('The active native session context is incomplete') from exc

    policy_cwd = canonical_path(getattr(policy, 'cwd', None))
    scoped_cwd = canonical_path(scoped_session_cwd())
    if policy_cwd != scoped_cwd:
        raise ValueError('Native policy and session cwd disagree')
    if expected is not None and scoped_cwd != (expected['cwd_namespace'], expected['cwd']):
        raise ValueError('Memory call cwd differs from its native session binding')
    if explicit_cwd is not _MISSING and canonical_path(explicit_cwd) != scoped_cwd:
        raise ValueError('Explicit cwd conflicts with the active native session')
    if kwargs is not None and 'selected_cwd' in kwargs:
        if canonical_path(kwargs['selected_cwd']) != scoped_cwd:
            raise ValueError('Selected cwd conflicts with the active native session')

    route_key = bound_session_key()
    if not isinstance(route_key, str) or not route_key:
        raise ValueError('An active native session route is required')
    if kwargs is not None and kwargs.get('gateway_session_key') != route_key:
        raise ValueError('Native session route does not match provider initialization')
    if expected is not None and route_key != expected['route_key']:
        raise ValueError('Memory call belongs to another native session route')

    current_home = get_hermes_home()
    if kwargs is not None and not kwargs.get('hermes_home'):
        raise ValueError('Native profile home is required for provider initialization')
    home = Path(kwargs['hermes_home']) if kwargs is not None else current_home
    if home != current_home:
        raise ValueError('Native profile home does not match provider initialization')
    if expected is not None and current_home != expected['home']:
        raise ValueError('Memory call belongs to another native profile')

    current_profile = get_active_profile_name()
    profile = kwargs.get('agent_identity') if kwargs is not None else expected['profile'] if expected is not None else current_profile
    if (not isinstance(current_profile, str) or not current_profile or current_profile == 'custom'
            or not isinstance(profile, str) or not profile or profile == 'custom'
            or profile != current_profile):
        raise ValueError('Native profile identity does not match provider initialization')
    if expected is not None and current_profile != expected['profile']:
        raise ValueError('Memory call belongs to another native profile')

    return {
        'route_key': route_key,
        'cwd': scoped_cwd[1],
        'cwd_namespace': scoped_cwd[0],
        'home': home,
        'profile': profile,
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
        self._native_route: dict[str, Any] | None = None
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
        # A cwd-less caller may use only the complete declared gateway context.  An
        # explicit cwd remains compatible, but cannot contradict an active native turn.
        if 'cwd' in kwargs:
            native_context = _native_turn_context(kwargs=kwargs, explicit_cwd=kwargs['cwd'])
            cwd = kwargs['cwd']
        else:
            native_context = _native_turn_context(kwargs=kwargs, required=True)
            cwd = native_context['cwd']
        canonical_path(cwd)
        home = Path(kwargs.get('hermes_home') or get_hermes_home())
        path, policy = configuration(home)
        identity = resolve_identity(session_id=session_id, cwd=cwd,
                                    selected_cwd=kwargs.get('selected_cwd', cwd),
                                    explicit=kwargs.get('agent_workspace') or '', policy=policy)
        profile = kwargs.get('agent_identity') or ''
        if not isinstance(profile, str) or not profile or profile == 'custom':
            raise ValueError('A native profile identity is required')
        delegate_context = {**kwargs, 'cwd': identity['cwd'], 'agent_workspace': identity['workspace']}
        delegate_context.pop('selected_cwd', None)
        self._native().initialize(session_id, **delegate_context)
        self._home, self._profile = home, profile
        self._policy_path, self._policy, self._identity = path, policy, identity
        self._native_route = native_context

    def _guard(self, kwargs):
        if self._identity is None or self._home is None or self._closed:
            raise ValueError('Memory provider is not bound to an active session')
        if self._native_route is not None:
            context = _native_turn_context(expected=self._native_route, required=True)
            for field in ('caller_gsk', 'gateway_session_key'):
                if field in kwargs and kwargs[field] != context['route_key']:
                    raise ValueError('Memory call belongs to another native session route')
        if kwargs.get('hermes_home', str(self._home)) != str(self._home):
            raise ValueError('Cross-profile memory call refused')
        if kwargs.get('agent_identity', self._profile) != self._profile:
            raise ValueError('Cross-profile memory call refused')
        path, current = configuration(self._home)
        if path != self._policy_path:
            raise ValueError('Policy owner changed; a new session is required')
        validate_binding(self._identity, current,
                         session_id=kwargs.get('session_id') or self._identity['session_id'],
                         cwd=kwargs.get('cwd', self._identity['cwd']))

    def validate_workspace_context(self, session_id, cwd):
        """Validate a hook's caller identity against this provider's native binding."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError('A native session identity is required')
        self._guard({'session_id': session_id, 'cwd': cwd})

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

    def on_session_end(self, messages):
        # Native cache eviction can run without a turn scope. The approved
        # upstream inherits this exact no-op, so there is no content to admit.
        if (self._delegate is not None
                and type(self._delegate).on_session_end is MemoryProvider.on_session_end):
            return None
        self._guard({})
        return self._invoke('on_session_end', (messages,), {})

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
