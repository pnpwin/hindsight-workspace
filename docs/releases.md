# Release policy

## One version, one commit

`VERSION` is the release version. `plugin.yaml`, `.codex-plugin/plugin.json` and the MCP server declaration in `src/hindsight_workspace/cli.py` must contain the same stable SemVer `X.Y.Z`. The marketplace entry points to this package (`./`), so Codex obtains its version from that same plugin manifest, not a separately versioned copy. `scripts/check_release.py` rejects disagreement, a wrong plugin identity, an unrelated marketplace source, duplicate version fields and a missing changelog entry; it checks MCP source without importing the client.

The Git tag is exactly `vX.Y.Z`; the GitHub Release uses that tag and its exact commit. Published tags/releases are never moved or replaced. Fix a bad release with a new version. Patch increments are backward-compatible fixes; minor increments add features or change internal installation paths. While below 1.0, document incompatible changes in each minor release. Prerelease/build suffixes are deliberately not supported by this first release workflow.

## Maintainer sequence

1. Make the change on a branch. Update `VERSION`, both client manifests, the MCP server version, the packaging test expectation and `CHANGELOG.md` together. In `plugin.yaml`, root keys must be unquoted ASCII identifiers followed immediately by `:`; name is an unquoted scalar and version a double-quoted scalar. Quoted/escaped keys, merge keys and other root-key spellings are rejected, not interpreted as YAML alternatives. Comments, blank lines and indented values remain supported. JSON manifests reject duplicate members, including escaped spellings and nested objects. This read-only release gate deliberately accepts a canonical manifest format; it is not a general YAML validator.
2. Run the complete applicable offline suite and relocated native Hermes/Codex checks. Obtain review of the exact candidate and record compatibility, warnings and untested live behavior. Package CI checks syntax, release-contract tests and version/marketplace agreement; **it does not prove live-client acceptance**. A passing PR workflow does not waive the native/manual acceptance checklist below.
3. Merge the reviewed PR only with owner authorization. To release, select its reviewed commit on `main`, verify it has current required checks and obtain explicit release authorization. Do not tag a candidate still on HOLD for required acceptance.
4. Create one annotated tag for that commit and push that tag. For example, with an explicitly selected reviewed commit:

   ```sh
   VERSION="$(tr -d '\n' < VERSION)"
   python3 -B scripts/check_release.py --tag "v$VERSION"
   git tag -a "v$VERSION" "$REVIEWED_COMMIT" -m "Hindsight Workspace $VERSION"
   git push origin "refs/tags/v$VERSION"
   ```

5. `Tagged release` runs the reusable package checks, validates the exact tag/version and verifies that its commit is reachable from `origin/main`. Only then does its restricted publication job create a non-draft, non-prerelease GitHub Release with generated notes. It reads back the exact release's tag, commit and status. Existing releases are not updated automatically; a collision fails closed. GitHub supplies source archives for the tagged commit.

Normal branch pushes, PRs and merges do not create releases. A human-created tag is the publication decision, not merely a request to run tests. The workflow uses a commit-pinned checkout action, read-only permissions for checks and `contents: write` only for the tag publisher. Event values pass through quoted environment variables and the strict tag validator. A tag pushed by another workflow's `GITHUB_TOKEN` may not trigger a new workflow; use the maintainer's authorized normal Git route rather than adding an automatic token workaround.

## Native acceptance checklist before a release tag

- Whole-candidate tests, relocated imports, manifests, Codex hook/MCP transport and Hermes native adapter loading pass on the intended supported runtimes.
- Source/installed revision and Python descriptor are known; unrelated config, model/reasoning, credentials, databases and Hermes core are unchanged.
- If activation is part of this release, its newly qualified native update and relevant guards pass. Do not replay an older single-use installer or weaken a failed approval snapshot.
- Functional CLI/GUI claims have exact owned IDs, native cwd and linked successful tool results. Distinguish expected policy bank, observed diagnose bank and actual HTTP bank; missing trace stays UNOBSERVED.
- Document remaining warnings and delivery/recovery limits. SessionEnd timeout alignment is not proof of final network retain delivery.

## Installation and updates are separate

A GitHub Release does not update existing Hermes or Codex installations. Normal default-branch installation remains the updatable development path described in README. For a reproducible release, resolve its tag to the commit and use that same full commit SHA in both native managers. Hermes `--ref` requires the full SHA; a pin prevents ordinary updates. Do not substitute a tag string for that SHA.

Codex's package-cache version comes from `.codex-plugin/plugin.json`; changing the version creates a new version identity and requires native installation/refresh and hook trust checks. Never rename or edit installed cache directories by hand. Preserve user data and policy-owner paths independently of payload layout.

Release workflow installation, its first remote CI run, actual GitHub publication, local native activation and live acceptance are separate milestones. No one of them certifies the others.
