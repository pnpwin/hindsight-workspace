"""Opt-in logical workspace identity; no network, subprocesses or directory changes."""
from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import re
from pathlib import Path, PureWindowsPath


def resolve_workspace(explicit: str, cwd: str | os.PathLike[str] | None) -> str:
    """Resolve once per provider: explicit name, session cwd, then terminal scope.

    Never fall back to resolve_agent_cwd/os.getcwd: those can select a sibling
    session's launch directory in a multiplexer. Missing scope fails closed.
    Paths are logical, lexically normalized identities, not local realpaths.
    """
    if explicit and explicit != "hermes":
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?", explicit):
            raise ValueError("Invalid explicit workspace: use an ASCII bank-safe identifier")
        return explicit
    if not cwd:
        from agent.runtime_cwd import scoped_session_cwd, scope_terminal_cwd

        cwd = scoped_session_cwd() or scope_terminal_cwd()
    raw = os.fspath(cwd) if cwd else ""
    if not isinstance(raw, str) or not raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("workspace_from_cwd requires a declared absolute cwd; refusing a shared fallback bank")
    if PureWindowsPath(raw).is_absolute():
        canonical = ntpath.normcase(ntpath.normpath(raw))
        basename, namespace = ntpath.basename(canonical), "windows:"
    elif posixpath.isabs(raw):
        canonical = posixpath.normpath(raw)
        if canonical == posixpath.normpath(str(Path.home())):
            return "home"
        basename, namespace = posixpath.basename(canonical), "posix:"
    else:
        raise ValueError("workspace_from_cwd requires a declared absolute cwd; refusing a shared fallback bank")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", basename).strip("-")[:48] or "workspace"
    digest = hashlib.sha256((namespace + canonical).encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"
