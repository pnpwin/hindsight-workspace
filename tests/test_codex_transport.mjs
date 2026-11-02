import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const root = fileURLToPath(new URL('../', import.meta.url));

test('real MCP SDK carries threadId; sessionId is not a binding', { timeout: 25000 }, async () => {
  const python = process.env.HINDSIGHT_WORKSPACE_PYTHON;
  assert.ok(python && isAbsolute(python), 'set installer-resolved HINDSIGHT_WORKSPACE_PYTHON');
  assert.ok(process.env.TMPDIR, 'tests require owned scratch TMPDIR');
  const home = mkdtempSync(join(process.env.TMPDIR, 'codex-mcp-'));
  mkdirSync(join(home, 'hindsight'));
  const policy = join(home, 'hindsight/config.json');
  writeFileSync(policy, '{}');
  const env = { ...process.env, HINDSIGHT_WORKSPACE_POLICY: policy };
  let child;
  try {
    for (const id of ['thread-a', 'thread-b']) {
      const result = spawnSync('node', [join(root, 'src/hindsight_workspace/codex/hooks.mjs')], { env, encoding: 'utf8',
        input: JSON.stringify({ hook_event_name: 'SessionStart', session_id: id, cwd: '/synthetic/project' }), timeout: 10000 });
      assert.equal(result.status, 0, result.stderr);
    }
    child = spawn('node', [join(root, 'src/hindsight_workspace/codex/mcp.mjs')], { env, stdio: ['pipe', 'pipe', 'pipe'] });
    let diagnostics = '';
    child.stderr.on('data', data => diagnostics += data.toString());
    let next = 0;
    const waiting = new Map();
    const lines = createInterface({ input: child.stdout });
    lines.on('line', line => {
      const response = JSON.parse(line);
      const pending = waiting.get(response.id);
      if (pending) { clearTimeout(pending.timer); waiting.delete(response.id); pending.resolve(response); }
    });
    const request = (method, params) => new Promise((resolve, reject) => {
      const id = ++next;
      const timer = setTimeout(() => { waiting.delete(id); reject(new Error('MCP response deadline: ' + diagnostics)); }, 8000);
      waiting.set(id, { resolve, timer });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
    const initialized = await request('initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'synthetic-codex', version: '1' } });
    assert.ok(initialized.result, JSON.stringify(initialized) + diagnostics);
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
    const listed = await request('tools/list', {});
    assert.equal(listed.result?.tools.length, 9, JSON.stringify(listed) + diagnostics);
    for (const threadId of ['thread-a', 'thread-b']) {
      const called = await request('tools/call', { name: 'hindsight_diagnose', arguments: {}, _meta: { threadId, sessionId: 'unrelated-transport-session' } });
      assert.equal(called.result?.isError ?? false, false, JSON.stringify(called));
      assert.equal(JSON.parse(called.result.content[0].text).result.session_id, threadId);
    }
    const rejected = await request('tools/call', { name: 'hindsight_diagnose', arguments: {}, _meta: { sessionId: 'thread-a' } });
    assert.equal(rejected.result?.isError, true, JSON.stringify(rejected));
    child.stdin.end();
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('MCP did not exit on EOF')), 3000);
      child.once('exit', code => { clearTimeout(timer); code === 0 ? resolve() : reject(new Error(diagnostics)); });
    });
  } finally {
    if (child && child.exitCode === null) child.kill('SIGTERM');
    rmSync(home, { recursive: true, force: true });
  }
});
