import { spawn } from 'node:child_process';
import { closeSync, constants, fstatSync, openSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export function launch(command) {
  let python = process.env.HINDSIGHT_WORKSPACE_PYTHON;
  let policy = process.env.HINDSIGHT_WORKSPACE_POLICY;
  let source = process.env.HERMES_SOURCE;
  try {
    if (!python || !policy || !source) {
      // GUI processes need an installer-owned binding, not a login-shell environment.
      const home = process.env.CODEX_HOME || join(homedir(), '.codex');
      if (!isAbsolute(home)) throw new Error('relative Codex home');
      const fd = openSync(join(home, 'hindsight-workspace.json'), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      let settings;
      try {
        const info = fstatSync(fd);
        if (!info.isFile() || info.size > 16384) throw new Error('invalid launcher configuration');
        settings = JSON.parse(readFileSync(fd, 'utf8'));
      } finally { closeSync(fd); }
      if (!settings || Array.isArray(settings) || Object.keys(settings).some(key => !['python', 'policy', 'hermes_source'].includes(key))) {
        throw new Error('unknown launcher settings');
      }
      python ||= settings.python;
      policy ||= settings.policy;
      source ||= settings.hermes_source;
    }
    if (![python, policy, source].every(path => typeof path === 'string' && isAbsolute(path))) {
      throw new Error('absolute runtime paths required');
    }
  } catch {
    console.error('hindsight-workspace requires installer-resolved Python, policy and Hermes source paths');
    process.exitCode = 1;
    return;
  }
  const env = { ...process.env, HERMES_SOURCE: source, HERMES_DISABLE_LAZY_INSTALLS: '1' };
  const child = spawn(python, ['-B', fileURLToPath(new URL('../cli.py', import.meta.url)), command, '--policy', policy], { stdio: 'inherit', env });
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
  child.on('error', () => { console.error('hindsight-workspace runtime unavailable'); process.exitCode = 1; });
  child.on('exit', code => { process.exitCode = code ?? 1; });
}
