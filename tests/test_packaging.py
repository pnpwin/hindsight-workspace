"""Packaging preserves native lifecycle ordering and separate host transports."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_native_manifests_and_async_collector_are_present(self):
        self.assertTrue((ROOT / '.codex-plugin/plugin.json').exists(), 'Codex marketplace manifest missing')
        manifest = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())
        self.assertEqual(manifest['version'], '0.4.0')
        hook_path = manifest['hooks']
        hooks = json.loads((ROOT / hook_path).read_text())['hooks']
        for event in ('SessionStart', 'SubagentStart', 'UserPromptSubmit', 'Stop', 'SubagentStop', 'SessionEnd'):
            commands = [h for group in hooks[event] for h in group['hooks']]
            memory = [h for h in commands if 'hooks.mjs' in h['command']]
            self.assertEqual(len(memory), 1)
            self.assertEqual(memory[0].get('async', False), event in ('Stop', 'SubagentStop'))
        collector = [h for group in hooks['UserPromptSubmit'] for h in group['hooks'] if 'sync.mjs' in h['command']]
        self.assertEqual(len(collector), 1)
        self.assertIs(collector[0]['async'], True)
        self.assertGreaterEqual(collector[0]['timeout'], 1000)
        mcp = json.loads((ROOT / 'mcp.json').read_text())['mcpServers']['hindsight-workspace']
        self.assertEqual(mcp['command'], 'node')
        self.assertIn('src/hindsight_workspace/codex/mcp.mjs', mcp['args'])
        self.assertEqual(mcp['cwd'], '.')
        self.assertGreaterEqual(mcp['tool_timeout_sec'], 120)
        marketplace = json.loads((ROOT / '.agents/plugins/marketplace.json').read_text())
        self.assertEqual(marketplace['plugins'][0]['source'], './')

    def test_gui_launcher_uses_installer_config_not_shell_environment(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as tmp:
            home = Path(tmp)
            codex = home / '.codex'
            codex.mkdir()
            owner = home / 'owner/hindsight/config.json'
            owner.parent.mkdir(parents=True)
            owner.write_text('{}')
            import sys
            (codex / 'hindsight-workspace.json').write_text(json.dumps({
                'python': sys.executable, 'policy': str(owner), 'hermes_source': os.environ['HERMES_SOURCE']}))
            env = {k: v for k, v in os.environ.items() if not k.startswith(('HINDSIGHT_', 'HERMES_'))}
            env.update(HOME=tmp, CODEX_HOME=str(codex), HERMES_DISABLE_LAZY_INSTALLS='1')
            result = subprocess.run(['node', str(ROOT / 'src/hindsight_workspace/codex/hooks.mjs')], env=env, capture_output=True,
                text=True, timeout=15, input=json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'gui-thread', 'cwd': '/synthetic/project'}))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})
