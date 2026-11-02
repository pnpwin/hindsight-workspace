"""Cold source relocation must not depend on checkout cwd or old root modules."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path('src/hindsight_workspace')


class SrcLayoutTests(unittest.TestCase):
    def test_cold_relocated_native_loader_and_codex_launch(self):
        self.assertTrue((ROOT / PACKAGE / 'hermes.py').is_file(), 'maintained Hermes runtime must live under src/')
        self.assertEqual({path.name for path in ROOT.glob('*.py')}, {'__init__.py'})
        self.assertFalse((ROOT / 'codex').exists())
        self.assertFalse((ROOT / 'maintenance').exists())
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temporary:
            base = Path(temporary)
            relocated = base / 'relocated plugin with spaces'
            relocated.mkdir()
            shutil.copy2(ROOT / '__init__.py', relocated / '__init__.py')
            shutil.copytree(ROOT / 'src', relocated / 'src')
            launch_cwd = base / 'unrelated working directory'
            launch_cwd.mkdir()
            code = '''import importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location('cold_plugin', root / '__init__.py', submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
providers, hooks = [], []
class Context:
    register_memory_provider = lambda self, provider: providers.append(provider)
    register_auxiliary_task = lambda self, *args, **kwargs: None
    register_hook = lambda self, name, fn: hooks.append(fn)
    on_unload = lambda self, callback: callback()
module.register(Context())
assert len(providers) == 1 and providers[0].name == 'hindsight-workspace'
assert len(hooks) == 1
for obj in (type(providers[0]), hooks[0]):
    origin = pathlib.Path(sys.modules[obj.__module__].__file__)
    assert origin.is_relative_to(root / 'src/hindsight_workspace'), origin
from cold_plugin.src.hindsight_workspace.maintenance import clean_gate
from cold_plugin.src.hindsight_workspace import survey_route
for dependency in (clean_gate, survey_route):
    assert pathlib.Path(dependency.__file__).is_relative_to(root / 'src/hindsight_workspace')
print('cold native loader and package dependency origins verified')
'''
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(relocated), os.environ['HERMES_SOURCE']],
                                    cwd=launch_cwd, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            owner = base / 'owner/hindsight/config.json'
            owner.parent.mkdir(parents=True)
            owner.write_text('{}')
            env = {**os.environ, 'HINDSIGHT_WORKSPACE_PYTHON': sys.executable,
                   'HINDSIGHT_WORKSPACE_POLICY': str(owner)}
            env.pop('PYTHONPATH', None)
            descriptor = json.loads((relocated / PACKAGE / 'codex/hooks.json').read_text())
            command = descriptor['hooks']['SessionStart'][0]['hooks'][0]['command']
            result = subprocess.run(shlex.split(command.replace('${PLUGIN_ROOT}', str(relocated))),
                                    cwd=launch_cwd, env=env, capture_output=True, text=True, timeout=20,
                                    input=json.dumps({'hook_event_name': 'SessionStart', 'session_id': 'relocated', 'cwd': str(launch_cwd)}))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})

    def test_session_end_has_bounded_timeout(self):
        manifest = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())
        hooks = json.loads((ROOT / manifest['hooks']).read_text())['hooks']
        command, = [command for group in hooks['SessionEnd'] for command in group['hooks']]
        self.assertEqual(command['timeout'], 3)
        self.assertIs(command['async'], False)
