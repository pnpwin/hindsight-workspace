"""Bounded Codex hook/stdio entrypoints. All paths come from installer-owned configuration."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

# Codex launches this file by absolute path, independently of its working directory.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'hindsight_workspace'

# The installer resolves the existing native checkout; never infer it from a workspace.
if os.environ.get('HERMES_SOURCE'):
    source = Path(os.environ['HERMES_SOURCE'])
    if not source.is_absolute():
        raise SystemExit('HERMES_SOURCE must be absolute')
    sys.path.insert(0, str(source))

from .codex_memory import Session, hook, tools
from .policy import load_policy, resolve_identity


def dispatch(command, payload, owner):
    if command == 'policy':
        return load_policy(owner)
    if command == 'identity':
        return resolve_identity(session_id=payload.get('session_id'), cwd=payload.get('cwd'),
                                selected_cwd=payload.get('cwd'), policy=load_policy(owner))
    if command == 'tools':
        return {'tools': tools(load_policy(owner))}
    if not isinstance(payload, dict):
        raise ValueError('Expected an object')
    meta = payload.get('_meta', {})
    sid = (payload.get('agent_id') or payload.get('session_id')) if command in {'hook', 'sync'} else meta.get('threadId') if isinstance(meta, dict) else None
    with Session(owner, sid).locked() as session:
        if command == 'sync':
            from .policy import canonical_path
            canonical_path(payload.get('cwd'))
            identity = session.check(payload.get('cwd'))
        elif command == 'hook':
            return hook(session, payload)
        elif command == 'tool':
            return {'result': session.tool(payload.get('name'), payload.get('arguments', {}))}
        else:
            raise ValueError('Unsupported command')
    # The workspace lock owns ingestion; do not block this session's memory hooks on a survey.
    from .survey_route import build_target
    from .hindsight_workspace import sync_workspace, atomic_json
    target = build_target(owner, identity)
    result = sync_workspace(target)
    atomic_json(target.state_root / 'last-run.json', {**result, 'session_id': sid})
    return result


async def serve(owner):
    import mcp_types as types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    policy = load_policy(owner)

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(**tool) for tool in tools(policy)])

    async def call_tool(ctx, params):
        try:
            # SDK-validated transport metadata, not model arguments or sessionId.
            meta = params.meta.model_dump() if hasattr(params.meta, 'model_dump') else params.meta or {}
            result = await asyncio.to_thread(dispatch, 'tool', {
                '_meta': meta, 'name': params.name, 'arguments': params.arguments or {}}, owner)
            return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False))])
        except Exception as exc:
            return types.CallToolResult(content=[types.TextContent(type='text', text='Workspace memory refused: ' + type(exc).__name__)], is_error=True)

    server = Server('hindsight-workspace', version='0.4.0', on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['identity', 'policy', 'hook', 'sync', 'tool', 'tools', 'mcp'])
    parser.add_argument('--policy', type=Path, required=True)
    args = parser.parse_args()
    load_policy(args.policy)
    if args.command == 'mcp':
        asyncio.run(serve(args.policy))
        return
    try:
        text = sys.stdin.read(2_000_001)
        if len(text) > 2_000_000:
            raise ValueError('Input exceeds safety budget')
        result = dispatch(args.command, json.loads(text), args.policy)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': 'Workspace memory refused; check identity, policy and pending receipts.'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
