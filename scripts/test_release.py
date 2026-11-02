"""Release-contract tests; no client, service, credentials or GitHub writes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_ci_runs_version_gate(self):
        self.assertIn('scripts/check_release.py', (ROOT / '.github/workflows/check.yml').read_text())


class ReleaseContractTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('check_release', ROOT / 'scripts/check_release.py')
        assert spec is not None and spec.loader is not None
        self.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.checker)
        self.temp = tempfile.TemporaryDirectory(prefix='release contract ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.put('VERSION', '0.4.0\n')
        self.put('plugin.yaml', 'name: hindsight-workspace\nversion: "0.4.0"\n')
        self.put('.codex-plugin/plugin.json', {'name': 'hindsight-workspace', 'version': '0.4.0'})
        self.put('.agents/plugins/marketplace.json', {'name': 'hindsight-workspace', 'plugins': [{'name': 'hindsight-workspace', 'source': './'}]})
        self.put('CHANGELOG.md', '# Changelog\n\n## 0.4.0\n\n- Relocate runtime source.\n')
        self.put('src/hindsight_workspace/cli.py', "server = Server('hindsight-workspace', version='0.4.0')\n")

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content) if isinstance(content, dict) else content)

    def test_matching_versions_and_tag(self):
        self.assertEqual(self.checker.check(self.root, 'v0.4.0'), '0.4.0')

    def test_pr_check_needs_no_tag(self):
        self.assertEqual(self.checker.check(self.root), '0.4.0')

    def test_wrong_or_malformed_tag_refused(self):
        for tag in ('v0.3.0', '0.4.0', 'v00.4.0', 'v0.4.0-rc.1', 'v0.4.0\n', 'v0.4.0;false', ''):
            with self.subTest(tag=repr(tag)), self.assertRaises(ValueError):
                self.checker.check(self.root, tag)

    def test_mismatched_client_version_refused(self):
        self.put('.codex-plugin/plugin.json', {'name': 'hindsight-workspace', 'version': '0.3.0'})
        with self.assertRaises(ValueError):
            self.checker.check(self.root)

    def test_mcp_server_version_is_checked_without_import(self):
        for source in (
            "server = Server('hindsight-workspace', version='0.3.0')\n",
            "server = Server('hindsight-workspace')\n",
            "server = Server('hindsight-workspace', version=external_value)\n",
            "# no MCP server\n",
            "server = Server('hindsight-workspace', version='0.4.0')\n" * 2,
        ):
            with self.subTest(source=source):
                self.put('src/hindsight_workspace/cli.py', source)
                with self.assertRaises(ValueError):
                    self.checker.check(self.root)
        self.put('src/hindsight_workspace/cli.py', "raise RuntimeError('must not import')\nserver = Server('hindsight-workspace', version='0.4.0')\n")
        self.assertEqual(self.checker.check(self.root), '0.4.0')

    def test_invalid_version_refused(self):
        for version in ('00.4.0\n', '0.4\n', '0.4.0-rc.1\n', ' 0.4.0\n', '0.4.0\n\n'):
            with self.subTest(version=repr(version)):
                self.put('VERSION', version)
                with self.assertRaises(ValueError):
                    self.checker.check(self.root)

    def test_yaml_duplicate_version_refused(self):
        self.put('plugin.yaml', 'name: hindsight-workspace\nversion: "0.4.0"\nversion: "0.4.0"\n')
        with self.assertRaises(ValueError):
            self.checker.check(self.root)

    def test_duplicate_json_members_refused(self):
        for key in ('version', r'\u0076ersion'):
            with self.subTest(key=key):
                self.put('.codex-plugin/plugin.json',
                         '{"name":"hindsight-workspace","version":"0.3.0","' + key + '":"0.4.0"}')
                with self.assertRaises(ValueError):
                    self.checker.check(self.root)
        self.put('.codex-plugin/plugin.json', {'name': 'hindsight-workspace', 'version': '0.4.0'})
        self.put('.agents/plugins/marketplace.json',
                 '{"name":"hindsight-workspace","plugins":[{"name":"hindsight-workspace","source":"../other","source":"./"}]}')
        with self.assertRaises(ValueError):
            self.checker.check(self.root)

    def test_noncanonical_yaml_root_keys_refused(self):
        for key in ('"version"', "'version'", r'"\u0076ersion"', 'version '):
            with self.subTest(key=key):
                self.put('plugin.yaml', 'name: hindsight-workspace\nversion: "0.4.0"\n' + key + ': "0.3.0"\n')
                with self.assertRaises(ValueError):
                    self.checker.check(self.root)

    def test_canonical_yaml_allows_comments_and_nested_values(self):
        self.put('plugin.yaml', '# Package identity\n\nname: hindsight-workspace\nversion: "0.4.0"\nprovides_hooks:\n  - pre_llm_call\n')
        self.assertEqual(self.checker.check(self.root), '0.4.0')

    def test_marketplace_must_reference_this_package_once(self):
        for plugins in ([], [{'name': 'other', 'source': './'}], [{'name': 'hindsight-workspace', 'source': '../other'}], [{'name': 'hindsight-workspace', 'source': './'}] * 2):
            with self.subTest(plugins=plugins):
                self.put('.agents/plugins/marketplace.json', {'name': 'hindsight-workspace', 'plugins': plugins})
                with self.assertRaises(ValueError):
                    self.checker.check(self.root)

    def test_missing_changelog_version_refused(self):
        self.put('CHANGELOG.md', '# Changelog\n\n## 0.3.0\n')
        with self.assertRaises(ValueError):
            self.checker.check(self.root)

    def test_wrong_plugin_name_refused(self):
        self.put('.codex-plugin/plugin.json', {'name': 'other', 'version': '0.4.0'})
        with self.assertRaises(ValueError):
            self.checker.check(self.root)


if __name__ == '__main__':
    unittest.main()
