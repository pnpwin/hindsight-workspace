# Hindsight Workspace

One workspace identity and memory policy for **Hermes, Codex CLI and local Codex Desktop**. The Hermes adapter delegates dialogue memory to the unmodified official Hindsight provider. Codex uses native lifecycle hooks and MCP. Both use the same bounded survey/Git collector and owner-scoped state.

**Release candidate — offline verification only.** A working Desktop session, production migration and two-way live recall have not yet been accepted. Do not replace a running legacy installation using these instructions.

## Contract

- Identity comes from the native session working directory, never the launcher's process cwd. A selected project wins; the client's normal directory outside a project is valid. Exact local HOME maps to `home`.
- Other bank names preserve the legacy lexical path algorithm: ASCII basename (48 characters) plus the first 16 SHA-256 hex characters of the normalized path and its POSIX/Windows namespace. No `realpath`, Git-root climbing or main-worktree collapsing. Moving a checkout or using a subdirectory changes its bank.
- One explicit `<Hermes home>/hindsight/config.json` owns policy. Derived native projections must match it. Missing identity, foreign transcripts, policy drift and incompatible overrides fail closed.
- Defaults: recall budget `mid`, types `world/experience/observation`, 4096 output tokens, **2000 input characters**, 120-second memory HTTP timeout; async retain after each completed turn; shared observation scope. Additional writable and recall-only banks retain their distinct roles. Clients do not rewrite bank settings.
- Codex binds root hook `session_id`, or a child's native `agent_id`, to MCP `_meta.threadId`. `_meta.sessionId`, model arguments and process cwd cannot select a bank. Children have separate conversation cursors and parent tags; inherited parent transcript history is excluded.
- Stop capture waits for a native completion marker and runs asynchronously. Interrupted work is recovered on a subsequent lifecycle event; a partial assistant message is not a completed turn.
- Codex `memory_mode=context` disables all content-access and write tools, including optional knowledge/initiative tools; the two diagnostic tools remain available. Recall preambles follow the shared policy, including the native default header. Restart the MCP client after policy changes; existing session bindings reject drift.

## Project ingestion and privacy

The collector surveys Git and non-Git workspaces. A map refreshes after 20 new commits or a history rewrite; a non-Git map is generated once. Git import requires `.git` directly in the selected directory and includes at most 300 commit messages, 2000 characters each, **not diffs**.

Surveys use the shared owner's native `hindsight_workspace_survey` auxiliary task through `PluginContext.llm.complete`: one prompt/route, no tools or agent loop. The owner must enable this plugin and authorize its auxiliary route. Codex starts bounded asynchronous collection on `UserPromptSubmit` after native session binding; a busy workspace is retried on a later turn.

State and per-workspace locks live under the policy owner's `plugin-data/hindsight-workspace`. The ingestion key is endpoint + bank + cwd, not harness/profile/session. Pending operation UUIDs and redacted content are journaled before submission. Cursors advance only after operation completion and exact document readback. Confirmed unchanged work makes no new model/retain call.

Collection does not run project code, tests, builds or installers. Git routing variables, hooks, fsmonitor, pager and signature display are disabled. Bounds: 200 directory entries, depth 3, eight excerpts of 4000 bytes, 24000 characters total; Git output 2 MB and 20 seconds per command. Hidden/secret-named/generated/ignored files and symlinks are excluded. PEM blocks and URL literals are scrubbed, and native egress redaction runs before model/memory requests and after summarization.

This is a partial map, **not exhaustive secret detection**. Selected excerpts and commit messages reach the configured model and Hindsight service and consume quota. Non-Git ignore files are not interpreted. HOME surveys do not recursively traverse arbitrary personal folders. Runtime collection currently requires a local POSIX host; Windows paths are supported as logical identities, not as a Windows collector runtime.

## Packaging and prerequisites

- Hermes uses native `plugin.yaml`; Codex uses `.codex-plugin/plugin.json` and `.agents/plugins/marketplace.json`.
- The native Codex manifest is intentional: the inspected Agent Plugins v1 MCP schema cannot represent a transport timeout. This package sets a 600-second MCP tool deadline around 120-second memory requests instead of silently inheriting a shorter transport deadline.
- Existing Hermes managed Python, Hermes source checkout, its Python MCP SDK, Node and Git are required. The launchers never acquire dependencies or start a Hindsight service.
- Offline tests use Hermes 0.22.5 interfaces, official Hindsight plugin 1.2.3 and Codex 0.166.0 interfaces. `maintenance/clean_gate.py` binds the reviewed upstream source revisions. Unknown source changes need review, not automatic patching.
- Installation from a Git source/custom marketplace is distinct from admission to a curated central catalog.

### Fresh-install command surface

These native commands install the package from this repository/custom marketplace. They do not configure the shared owner, select the Hermes provider or qualify live memory. An approved clean official Hindsight provider is a separate prerequisite; use a reviewed revision listed in `maintenance/clean_gate.py` rather than an arbitrary latest source.

```sh
REV="$(git ls-remote https://github.com/pnpwin/hindsight-workspace.git refs/heads/main | cut -f1)"
hermes plugins install pnpwin/hindsight-workspace --ref "$REV" --no-enable
hermes plugins validate /absolute/installed/plugin/path --json
hermes plugins doctor hindsight-workspace --ci

codex plugin marketplace add pnpwin/hindsight-workspace --ref "$REV"
codex plugin add hindsight-workspace@hindsight-workspace
codex plugin list --marketplace hindsight-workspace --available --json
```

Do not select the new provider until the clean official provider, policy projections and launcher bindings have been prepared and verified. Do not leave legacy Codex capture hooks/MCP or old workspace collectors running alongside the replacement.

### Configuration ownership

The selected Hermes home's `hindsight-workspace/config.json` contains exactly:

```json
{"policy_path":"/absolute/shared-owner/hindsight/config.json"}
```

The owner's policy must normalize through `policy.normalize_policy`; every native Hermes `hindsight/config.json` projection must include the full normalized defaults and match that owner. Preserve credentials in native secret scope or their existing credential fields; never put credentials in fingerprints, launcher arguments or receipts. Repository bank overrides must remain disabled. `retain_async` must be true because Hindsight ignores client operation IDs for synchronous retains.

After the projection and clean-source checks, use native Hermes configuration/enable commands to select `memory.provider=hindsight-workspace` and enable the plugin. Configure its `hindsight_workspace_survey` auxiliary slot through the native Hermes task configuration; both clients use that owner's route.

Codex GUI processes do not reliably inherit shell variables. The installer's `<CODEX_HOME>/hindsight-workspace.json` contains only non-secret absolute paths:

```json
{
  "python":"/absolute/existing/managed/venv/bin/python",
  "policy":"/absolute/shared-owner/hindsight/config.json",
  "hermes_source":"/absolute/hermes/source"
}
```

For an explicitly configured process, `HINDSIGHT_WORKSPACE_PYTHON`, `HINDSIGHT_WORKSPACE_POLICY` and `HERMES_SOURCE` override these bindings. HOME is used only to locate Codex configuration, never as a missing workspace identity fallback. Runtime and policy configuration must be owned/trusted by the operator.

## Migration and maintenance

`maintenance/migrate_shared.py` defaults to dry-run. It accepts a reviewed proposal containing explicit roots, staged source/target pairs with expected prior hashes, protected input hashes and a private backup root. Prepare YAML/TOML candidates with their native client writers in an isolated home first. The returned plan contains paths/hashes, not configuration contents.

```sh
python maintenance/migrate_shared.py /absolute/proposal.json
python maintenance/migrate_shared.py /absolute/reviewed-plan.json --apply --clients-stopped
python maintenance/migrate_shared.py /absolute/receipt.json --rollback --clients-stopped
```

Apply/rollback are **separate operator actions**, requiring all old/new clients and updaters to be stopped. Every input is checked before and after backup; unknown paths, symlinks and drift are refused. Writes are individually atomic and journaled, not a globally atomic filesystem transaction. Conditional rollback refuses to overwrite newer writes. Backups can contain existing configuration credentials and must remain private. Neither database/schema/service operations nor dependency-environment rollback are performed. Plugin payload publication belongs to native managers, not this file transaction.

`adoption.py` offers explicit GET-only verification and exact-plan adoption of legacy cursors. It does not delete legacy receipts or guess a winner by timestamp. Uncertain/pending work is not silently discarded.

`maintenance/clean_gate.py` checks clean approved upstream bytes plus an independently approved adapter/config/metadata snapshot. Monitoring and scheduled dispatch never restore `workspace_bank.py` or auto-update a pin. Its managed-publication boundary is fixture-tested; integrating existing operator-specific updater/fleet entrypoints remains an activation prerequisite. Do not retain an old updater that reinstalls the overlay.

The approval includes the policy-owner selector, owning policy and native auxiliary configuration. Enumerate external consumers explicitly with repeated `--consumer-config /absolute/path` when creating an approval snapshot (including each Codex `config.toml` and `hindsight-workspace.json`); the gate never discovers consumers by scanning HOME. The runtime adapter reuses the approved-source gate and checks the delegate's module origin before memory calls. Apply and rollback share a scope lock; unrelated writers must still be stopped.

Run the gate with the actual consumer's Python interpreter. Local bytecode caches are accepted only when their serialized code matches a fresh compilation of approved source for that interpreter and optimization level; cache bytes are never deserialized or executed by the gate. Foreign, mismatched or externally redirected caches (`PYTHONPYCACHEPREFIX` / `-X pycache_prefix`) fail closed and are not silently removed. Native payload writers must remain quiescent during validation/loading; this check is not a sandbox against a concurrent filesystem attacker.

## Verification

Use the existing managed interpreter and an isolated HOME/HERMES_HOME under an owned scratch directory. Set `HERMES_SOURCE` to the canonical Hermes checkout and `HINDSIGHT_UPSTREAM` to a clean reviewed official plugin directory. Do not copy live credentials. `TMPDIR` must point to owned scratch.

```sh
"$HERMES_PYTHON" -B -m unittest discover -s tests -v
HINDSIGHT_WORKSPACE_PYTHON="$HERMES_PYTHON" node --test tests/test_codex_transport.mjs
```

The Python suite covers frozen legacy bank IDs, native provider loading, policy/request parity, SDK metadata, conversation completion/recovery, child lineage, shared process locks, collector privacy, cursor adoption and guarded migration. HTTP/model boundaries are synthetic fixtures. Native marketplace admission/install/readback, security review and the live cross-client matrix are separate gates; unit success does not establish them.

Public CI checks Python/Node syntax and JSON files only; it does not run the host-bound integration suite or activate memory. Hermes requires a full 40-character SHA for `--ref`, not a branch name. Both commands above use one resolved revision; for controlled deployments, set `REV` to an explicitly reviewed commit instead of resolving the moving `main` branch.

## License and attribution

MIT; see [LICENSE](LICENSE). The frozen `tests/fixtures/legacy_provider/settings.py` comes from [Vectorize Hindsight's Hermes integration](https://github.com/vectorize-io/hindsight/blob/d952b6e655fccab4581ee0722b51c5f103873d12/hindsight-integrations/hermes/settings.py), copyright Vectorize AI, Inc.; its notice is retained in LICENSE. The small legacy workspace resolver fixture preserves this integration's pre-migration behavior. Hermes, Hindsight runtime/SDK and Codex are external prerequisites, not vendored runtimes.
