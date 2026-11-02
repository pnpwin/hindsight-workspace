"""Read-only release identity gate. Stable SemVer releases only."""
import argparse
import ast
import json
from pathlib import Path
import re

NAME = 'hindsight-workspace'
NUMBER = r'(?:0|[1-9][0-9]*)'
VERSION = rf'{NUMBER}\.{NUMBER}\.{NUMBER}'


def unique_members(pairs):
    if len(dict(pairs)) != len(pairs):
        raise ValueError('JSON object contains duplicate keys')
    return dict(pairs)


def check(root: Path, tag: str | None = None) -> str:
    raw = (root / 'VERSION').read_text()
    if re.fullmatch(VERSION + r'\n?', raw) is None:
        raise ValueError('VERSION must contain one stable X.Y.Z version')
    version = raw.removesuffix('\n')
    if tag is not None and (re.fullmatch('v' + VERSION, tag) is None or tag != 'v' + version):
        raise ValueError('tag must exactly equal v' + version)
    lines = (root / 'plugin.yaml').read_text().splitlines()
    # Reject alternate root-key spellings rather than implementing YAML semantics.
    for line in lines:
        if line.strip() and not line[0].isspace() and not line.startswith('#'):
            if re.match(r'[A-Za-z_][A-Za-z0-9_]*:', line) is None:
                raise ValueError('plugin.yaml requires canonical unquoted root keys')
    # Restrict these two fields to canonical scalars, not a second YAML parser.
    for key, expected in [('name', NAME), ('version', json.dumps(version))]:
        if [line for line in lines if line.startswith(key + ':')] != [f'{key}: {expected}']:
            raise ValueError('plugin.yaml ' + key + ' does not match the release')
    plugin = json.loads((root / '.codex-plugin/plugin.json').read_text(), object_pairs_hook=unique_members)
    if plugin.get('name') != NAME or plugin.get('version') != version:
        raise ValueError('Codex name/version does not match the release')
    # Inspect the declaration without importing client code or starting a server.
    tree = ast.parse((root / 'src/hindsight_workspace/cli.py').read_text())
    servers = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name) and node.func.id == 'Server']
    if len(servers) != 1:
        raise ValueError('Expected exactly one MCP Server declaration')
    versions = [keyword.value for keyword in servers[0].keywords if keyword.arg == 'version']
    if (len(versions) != 1 or not isinstance(versions[0], ast.Constant)
            or versions[0].value != version):
        raise ValueError('MCP server version does not match the release')
    marketplace = json.loads((root / '.agents/plugins/marketplace.json').read_text(), object_pairs_hook=unique_members)
    entries = marketplace.get('plugins', [])
    if (marketplace.get('name') != NAME or len(entries) != 1
            or entries[0].get('name') != NAME or entries[0].get('source') != './'):
        raise ValueError('marketplace must reference this package exactly once')
    if (root / 'CHANGELOG.md').read_text().splitlines().count('## ' + version) != 1:
        raise ValueError('CHANGELOG.md must contain one heading for ' + version)
    return version


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', help='exact manually created release tag')
    args = parser.parse_args()
    try:
        version = check(Path(__file__).resolve().parents[1], args.tag)
    except (OSError, ValueError, TypeError, AttributeError, SyntaxError) as error:
        parser.exit(1, 'Release check failed: ' + str(error) + '\n')
    print(json.dumps({'version': version, 'tag': args.tag, 'passed': True}))
