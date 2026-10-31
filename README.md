# Hindsight Workspace

Shared project memory for Hermes and Codex. Use it when you switch agents in the same working directory and want them to recall the same project context.

The clients need to agree on which memory bank belongs to a project, how to save and recall conversations, and whether a project has already been indexed. This plugin gives them one workspace resolver, one policy and a shared collector. Hermes delegates dialogue memory to the unmodified official Hindsight provider; Codex uses native hooks and MCP.

Release candidate: isolated tests and native installations pass. Live cross-client recall, Desktop sessions and production migration remain unverified. Do not replace a running legacy installation with these instructions.

## How it works

- The native session directory selects the bank. The same directory and policy owner share memory; conversations keep separate cursors. HOME maps to `home`. Moving a checkout or selecting a subdirectory changes its bank.
- One `<Hermes home>/hindsight/config.json` owns policy. Missing identity, foreign transcripts and policy drift are refused. Repository bank overrides are disabled.
- Completed turns are retained asynchronously, with recovery after interruption. Defaults include `mid` recall, 2000 input characters and 4096 output tokens. Context-only mode disables content tools; diagnostics remain available.
- A shared project survey and Git cursor avoid repeated ingestion by both clients. Git import reads up to 300 commit messages, 2000 characters each, not diffs. Surveys refresh after 20 new commits or a history rewrite; non-Git directories are surveyed once.

## Install

Requires local POSIX, Git, Node, an existing Hermes managed Python environment with its MCP SDK, the Hermes source checkout and a running Hindsight service. Install a clean official Hindsight provider at a revision accepted by `maintenance/clean_gate.py`. This package does not install runtimes or start services.

```sh
hermes plugins install pnpwin/hindsight-workspace --no-enable
hermes plugins validate /absolute/installed/plugin/path --json
hermes plugins doctor hindsight-workspace --ci

codex plugin marketplace add pnpwin/hindsight-workspace
codex plugin add hindsight-workspace@hindsight-workspace
codex plugin list --marketplace hindsight-workspace --available --json
```

These commands install the default branch without a revision pin, allowing ordinary updates.

Optionally, use the same full commit SHA for both clients. Set `REV` to a reviewed SHA, or resolve the current `main` once:

```sh
REV="$(git ls-remote https://github.com/pnpwin/hindsight-workspace.git refs/heads/main | cut -f1)"
hermes plugins install pnpwin/hindsight-workspace --ref "$REV" --no-enable
codex plugin marketplace add pnpwin/hindsight-workspace --ref "$REV"
```

Choose this variant instead of the corresponding commands above. Hermes accepts only a full SHA in `--ref`; a pin prevents ordinary Hermes updates. Resolving `main` gives matching versions, not a reviewed release.

This installs from a custom source/marketplace, not a curated central catalog. Installation alone does not configure or activate shared memory.

## Configure before activation

Each participating Hermes home's `hindsight-workspace/config.json` points to the shared policy:

```json
{"policy_path":"/absolute/shared-owner/hindsight/config.json"}
```

Native Hermes policy projections must match the owner's normalized policy, including defaults. Keep credentials in native secret scope or existing credential fields. `retain_async` must be true for safe retries. Configure the owner's `hindsight_workspace_survey` auxiliary route through Hermes; both clients use it.

Codex reads non-secret absolute paths from `<CODEX_HOME>/hindsight-workspace.json`:

```json
{
  "python":"/absolute/existing/managed/venv/bin/python",
  "policy":"/absolute/shared-owner/hindsight/config.json",
  "hermes_source":"/absolute/hermes/source"
}
```

`HINDSIGHT_WORKSPACE_PYTHON`, `HINDSIGHT_WORKSPACE_POLICY` and `HERMES_SOURCE` override these paths. Restart MCP after policy changes. Before selecting `memory.provider=hindsight-workspace` and enabling the plugin, verify projections and source checks; stop legacy capture hooks/MCP, collectors and updaters that would restore the old overlay.

## Data sent outside the workspace

Conversation content, selected project excerpts and commit messages reach the configured model and Hindsight service and consume quota. Collection never runs project code, builds or installers. It excludes hidden, secret-named, generated and ignored files and symlinks, and applies redaction.

Secret detection is not exhaustive. Non-Git ignore files are not interpreted. HOME surveys do not recursively scan arbitrary personal folders. Review the configured destinations before enabling collection.

## Migration and maintenance

Prepare reviewed configuration candidates with native client writers in an isolated home. `maintenance/migrate_shared.py` defaults to dry-run:

```sh
python maintenance/migrate_shared.py /absolute/proposal.json
python maintenance/migrate_shared.py /absolute/reviewed-plan.json --apply --clients-stopped
python maintenance/migrate_shared.py /absolute/receipt.json --rollback --clients-stopped
```

Apply and rollback require stopped clients and updaters. Writes are individually atomic, not one global transaction; rollback refuses newer writes. Backups may contain credentials and must stay private. Native managers own plugin payloads; this utility does not roll back databases, services or dependency environments. `adoption.py` verifies legacy cursors before explicit adoption without deleting receipts.

Use `maintenance/clean_gate.py` with the consumer's Python and an explicit consumer roster (`--consumer-config /absolute/path`). It checks approved source, policy, configuration and local bytecode. Unknown or externally redirected caches are refused. Keep payload writers stopped during validation/loading; the check does not protect against concurrent filesystem attackers. Integrating existing updater entrypoints remains a prerequisite for activation.

## Tests and compatibility

Use an isolated HOME/HERMES_HOME, owned `TMPDIR`, no live credentials, `HERMES_SOURCE` pointing to Hermes and `HINDSIGHT_UPSTREAM` to the reviewed official provider:

```sh
"$HERMES_PYTHON" -B -m unittest discover -s tests -v
HINDSIGHT_WORKSPACE_PYTHON="$HERMES_PYTHON" node --test tests/test_codex_transport.mjs
```

Offline tests cover Hermes 0.22.5, Hindsight plugin 1.2.3 and Codex 0.166.0 interfaces with synthetic HTTP/model fixtures. Public CI checks syntax and JSON only. Private Hermes API imports require compatibility checks on upgrades.

## License

MIT; see [LICENSE](LICENSE). This is an independent integration. `tests/fixtures/legacy_provider/settings.py` comes from [Vectorize Hindsight's Hermes integration](https://github.com/vectorize-io/hindsight/blob/d952b6e655fccab4581ee0722b51c5f103873d12/hindsight-integrations/hermes/settings.py); its copyright and MIT notice are retained. The workspace resolver fixture preserves pre-migration behavior. Hermes, Hindsight runtime/SDK and Codex are external prerequisites, not bundled runtimes.
