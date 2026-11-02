# Changelog

## 0.4.0

Prepared candidate; GitHub publication and installed-client acceptance are separate gates.

- Move maintained runtime source into `src/`, preserving host-required root descriptors.
- Add a checked version contract and releases triggered by manually created tags.
- Set the Codex SessionEnd hook timeout to the native three-second limit; this does not guarantee final network delivery.

## 0.3.0

- Shared Hermes/Codex workspace identity, policy, memory and bounded ingestion.
- Hermes native session-context binding for gateway turns with omitted constructor cwd.
- This is historical manifest version metadata, not a claim that a GitHub release was published.
