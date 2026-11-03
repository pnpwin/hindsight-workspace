# Changelog

## Unreleased

- Accept reviewed upstream `2c00b59a` and `9486680b` with the same provider sources as `34a13868`, while keeping older sources and unknown revisions separate.
- Keep previously approved Hindsight sources working during upgrades, and bind each recorded revision to its exact source and bytecode set.

## 0.4.1

Accept upstream `34a13868` (vectorize-io/hindsight main) as the approved provider revision.

- Refresh the clean-gate hashes for `__init__.py`, `embedded.py` and the upstream test modules.
- Upstream changes are retain-durability fixes, the managed-uvx path and a keyless local LLM key; no public plugin API was removed.

## 0.4.0

Prepared candidate; GitHub publication and installed-client acceptance are separate gates.

- Move maintained runtime source into `src/`, preserving host-required root descriptors.
- Add a checked version contract and releases triggered by manually created tags.
- Set the Codex SessionEnd hook timeout to the native three-second limit; this does not guarantee final network delivery.

## 0.3.0

- Shared Hermes/Codex workspace identity, policy, memory and bounded ingestion.
- Hermes native session-context binding for gateway turns with omitted constructor cwd.
- This is historical manifest version metadata, not a claim that a GitHub release was published.
