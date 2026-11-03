# Hindsight Workspace

Shared project memory for Hermes and Codex. Use it when you switch agents in the same working directory and want them to recall the same project context.

The clients need to agree on which memory bank belongs to a project, how to save and recall conversations, and whether a project has already been indexed. This plugin gives them one workspace resolver, one policy and a shared collector. Hermes delegates dialogue memory to the unmodified official Hindsight provider; Codex uses native hooks and MCP.

Source-layout candidate: isolated adapter and transport checks do not certify an installed CLI or Desktop session. Activation and production migration require separate review. Do not replace a running legacy installation with these instructions.

## How it works

- The native session directory selects the bank. The same directory and policy owner share memory; conversations keep separate cursors. HOME maps to `home`. Moving a checkout or selecting a subdirectory changes its bank.
- One `<Hermes home>/hindsight/config.json` owns policy. Missing identity, foreign transcripts and policy drift are refused. Repository bank overrides are disabled.
- Completed turns are retained asynchronously, with recovery after interruption. Defaults include `mid` recall, 2000 input characters and 4096 output tokens. Context-only mode disables content tools; diagnostics remain available.
- A shared project survey and Git cursor avoid repeated ingestion by both clients. Git import reads up to 300 commit messages, 2000 characters each, not diffs. Surveys refresh after 20 new commits or a history rewrite; non-Git directories are surveyed once.

## Install

Requires local POSIX, Git, Node, an existing Hermes managed Python environment with its MCP SDK, the Hermes source checkout and a running Hindsight service. Install the clean official Hindsight provider at `TESTED_REVISION` in `src/hindsight_workspace/maintenance/clean_gate.py`; `TESTED_VERSION` records its plugin version. Only this last-tested revision is accepted, not earlier revisions with equal sources. Native maintenance requires `pinned: true`. This temporary fleet-migration revision accepts client/embed SDKs from `0.10.3` through `1.2.0` so shared-PM profiles can be updated serially without conflicting exact pins. Replace it with the final exact `1.2.0` revision after every profile has crossed this bridge. Dependencies remain owned by Hermes' native package manager; the plugin does not start services or edit managed environments directly.

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

Runtime code, including Codex launchers and maintenance, lives in `src/hindsight_workspace/`. The root `__init__.py` is the native Hermes loader; client manifests stay at the repository root. Existing policy, bank and durable state locations do not change.

Prepare reviewed configuration candidates with native client writers in an isolated home. Run maintenance from the repository root with the existing consumer Python. `migrate_shared` defaults to dry-run:

```sh
PYTHONPATH=src "$HERMES_PYTHON" -B -m hindsight_workspace.maintenance.migrate_shared /absolute/proposal.json
PYTHONPATH=src "$HERMES_PYTHON" -B -m hindsight_workspace.maintenance.migrate_shared /absolute/reviewed-plan.json --apply --clients-stopped
PYTHONPATH=src "$HERMES_PYTHON" -B -m hindsight_workspace.maintenance.migrate_shared /absolute/receipt.json --rollback --clients-stopped
```

Apply and rollback require stopped clients and updaters. Writes are individually atomic, not one global transaction; rollback refuses newer writes. Backups may contain credentials and must stay private. Native managers own plugin payloads; this utility does not roll back databases, services or dependency environments. `src/hindsight_workspace/adoption.py` verifies legacy cursors before explicit adoption without deleting receipts.

Use `PYTHONPATH=src "$HERMES_PYTHON" -B -m hindsight_workspace.maintenance.clean_gate` with an explicit consumer roster (`--consumer-config /absolute/path`). It checks approved source, policy, configuration and local bytecode. Unknown or externally redirected caches are refused. Cache checks are limited to CPython 3.11–3.15 and 2 MB per file; unsupported formats fail closed. Keep payload writers stopped during validation/loading; the check does not protect against concurrent filesystem attackers. Integrating existing updater entrypoints remains a prerequisite for activation.

## Tests and compatibility

Use isolated HOME/HERMES_HOME/CODEX_HOME, an owned `TMPDIR`, no live credentials, `HERMES_SOURCE` pointing to Hermes and `HINDSIGHT_UPSTREAM` to the reviewed official provider. Set `HERMES_HOME` to the isolated `$HOME/.hermes`, with no committed test runtime, and export `HERMES_DISABLE_LAZY_INSTALLS=1 UV_OFFLINE=1` so importing Hermes cannot provision dependencies. Run from the repository root:

```sh
PYTHONPATH=src "$HERMES_PYTHON" -B -m unittest discover -s tests -v
HINDSIGHT_WORKSPACE_PYTHON="$HERMES_PYTHON" node --test tests/test_codex_transport.mjs
```

Offline tests cover Hermes 0.22.5, the last-tested Hindsight provider and Codex 0.166.0 interfaces with synthetic HTTP/model fixtures. CI checks do not certify live client sessions. Private Hermes API imports require checks on upgrades.

Codex's synchronous `SessionEnd` hook has a three-second timeout. `Stop` and `SubagentStop` remain asynchronous. This bounds shutdown waiting; it does not guarantee retain delivery after process exit.

Hermes gateway constructors may omit `cwd` even for a session with a selected project. The adapter accepts that case only when the native task-local cwd, active session policy, gateway route key and profile agree. It pins that binding for subsequent memory calls and project collection. Explicit invalid directories, missing context and conflicting routes are refused; process cwd and environment variables cannot supply a fallback. An explicit cwd remains supported outside a native turn.

The native integration regression uses the unmodified Hermes kwargs producer, policy scope, memory manager and plugin loader, plus the clean official provider. It covers omitted cwd, project-hook capture and the upstream's inherited no-op session-end callback outside a turn. Concurrent route checks and sequential session switches are tested separately; overlapping queued retention and compression are not qualified by these tests. A passing isolated check does not certify an installed CLI/Desktop session.

## Updating the tested provider

After offline checks, replace `TESTED_REVISION`, `TESTED_VERSION` and the source/test hashes in the clean gate together. There is no compatibility history, release tag or version-bump workflow. Required client manifests and the MCP declaration retain our package identity `0.4.1`; this is separate from the official provider version. An upstream pin can be set with the native manager's `--ref` option.

## License

MIT; see [LICENSE](LICENSE). This is an independent integration. `tests/fixtures/legacy_provider/settings.py` comes from [Vectorize Hindsight's Hermes integration](https://github.com/vectorize-io/hindsight/blob/d952b6e655fccab4581ee0722b51c5f103873d12/hindsight-integrations/hermes/settings.py); its copyright and MIT notice are retained. The workspace resolver fixture preserves pre-migration behavior. Hermes, Hindsight runtime/SDK and Codex are external prerequisites, not bundled runtimes.
