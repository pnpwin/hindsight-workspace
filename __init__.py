"""Native memory adapter and bounded project-ingestion hook."""
from __future__ import annotations

import contextvars

import json
import logging
from pathlib import Path
import threading
import time

from .hindsight_workspace import Target, atomic_json, sync_workspace
from .policy import load_policy, normalize_policy, validate_binding

log = logging.getLogger(__name__)


def capture_target(*, ctx, agent, session_id, profile, home, settings, redact):
    """Read the adapter's public binding; never inspect upstream private fields."""
    try:
        if not profile or profile == 'custom' or not session_id or agent is None or agent.session_id != session_id:
            return None
        manager = getattr(agent, '_memory_manager', None)
        provider = manager.get_provider('hindsight-workspace') if manager else None
        if provider is None or provider.bound_profile != profile or provider.bound_home != Path(home):
            return None
        policy = load_policy(provider.policy_path)
        if normalize_policy(settings) != policy or provider.workspace_policy != policy:
            return None
        from tools.terminal_scope import terminal_env, TerminalPolicyUnavailable
        try:
            if terminal_env('TERMINAL_ENV', 'local') != 'local':
                return None
        except TerminalPolicyUnavailable:
            return None
        cwd = ctx.session_cwd()
        if not cwd or not Path(cwd).is_absolute() or agent.session_cwd != cwd:
            return None
        identity = provider.workspace_identity
        validate_binding(identity, policy, session_id=session_id, cwd=cwd)
        from .survey_route import build_target
        return build_target(provider.policy_path, identity, ctx=ctx, redact=redact)
    except (AttributeError, ImportError, KeyError, TypeError, ValueError, OSError):
        return None


class Worker:
    def __init__(self, sync=sync_workspace):
        self.sync = sync
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.jobs = {}

    def submit(self, target):
        key = str(target.state_root)
        with self.lock:
            if self.stop.is_set() or key in self.jobs or len(self.jobs) >= 2:
                return False
            context = contextvars.copy_context()
            thread = threading.Thread(target=context.run, args=(self._run, key, target), name='hindsight-workspace', daemon=True)
            self.jobs[key] = thread
            thread.start()
            return True

    def _run(self, key, target):
        try:
            result = self.sync(target, stop=self.stop)
        except Exception as exc:
            result = {'status': 'error', 'error_type': type(exc).__name__}
            log.warning('workspace automation failed (%s), will retry on a later turn', type(exc).__name__)
        try:
            atomic_json(target.state_root / 'last-run.json', {**result, 'session_id': getattr(target, 'session_id', ''), 'finished_at': time.time()})
        except OSError:
            log.warning('workspace automation could not save its result receipt')
        finally:
            with self.lock:
                self.jobs.pop(key, None)

    def close(self):
        self.stop.set()

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        with self.lock:
            threads = list(self.jobs.values())
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in threads)


def register(ctx):
    from .provider import WorkspaceMemoryProvider
    ctx.register_memory_provider(WorkspaceMemoryProvider())
    ctx.register_auxiliary_task('hindsight_workspace_survey', display_name='Workspace survey',
                               description='Bounded no-tools survey shared by Hermes and Codex.',
                               defaults={'timeout': 300})
    # The category loader only collects the provider; general discovery owns hook lifetime.
    if not hasattr(ctx, 'on_unload'):
        return
    worker = Worker()
    ctx.on_unload(worker.close)

    def on_turn(*, session_id='', parent_session_id='', **kwargs):
        if parent_session_id or worker.stop.is_set():
            return None
        try:
            from agent.subagent_lifecycle import get_active_subagent_parent
            from agent.redact import redact_for_egress
            from hermes_cli.config import load_config_readonly
            from hermes_cli.profiles import get_active_profile_name
            from hermes_constants import get_hermes_home
            profile, home = get_active_profile_name(), get_hermes_home()
            if not profile or profile == 'custom' or Path(ctx._manager.home_path) != home:
                return None
            config = load_config_readonly() or {}
            if (config.get('memory') or {}).get('provider') != 'hindsight-workspace':
                return None
            settings = json.loads((home / 'hindsight/config.json').read_text())
            target = capture_target(ctx=ctx, agent=get_active_subagent_parent(), session_id=session_id,
                                    profile=profile, home=home, settings=settings, redact=redact_for_egress)
            status = 'skipped-identity' if target is None else 'queued' if worker.submit(target) else 'busy-or-stopped'
            atomic_json(home / 'plugin-data/hindsight-workspace/last-hook.json', {
                'session_id': session_id, 'status': status, 'checked_at': time.time(),
                'bank': target.bank if target else None})
        except Exception as exc:
            log.warning('workspace hook skipped (%s)', type(exc).__name__)
        return None

    ctx.register_hook('pre_llm_call', on_turn)
