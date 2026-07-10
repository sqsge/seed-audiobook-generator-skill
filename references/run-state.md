# Run State Contract

`outputs/skill_runs/<run-id>/run_state.json` is the authoritative chapter state. It is written atomically after each state transition.

## Chapter States

- `planned`: source, sections, and voice registry are stored.
- `running`: one section is actively being rewritten, generated, or reviewed.
- `prepared`: all section director artifacts exist; audio generation has not completed.
- `interrupted`: the foreground process was interrupted and can be resumed.
- `needs_review`: generated audio exists, but one section failed an enabled delivery gate.
- `blocked`: a provider or executable returned a concrete failure.
- `completed`: every section is accepted and the chapter audio is stitched.

## Section States

- `planned`: section input exists.
- `running`: the lower-level workflow is active.
- `prepared`: director prompts and generation requests exist.
- `interrupted`: restart with the existing section manifest and artifacts.
- `needs_review`: preserve the current checkpoint and retry only gated chunks.
- `failed`: resolve `last_error`, then resume.
- `accepted`: immutable for normal resume; never regenerate automatically.

## Recovery Invariants

1. A repeated `run` with the same `run-id` loads state instead of re-planning.
2. `status` never calls an external provider.
3. Accepted sections are skipped.
4. Existing decoded chunks are reused.
5. Forced chunk regeneration first copies the previous prompt, raw audio, cleaned audio, and metadata into `07_audio_revisions`.
6. A previous QA checkpoint is copied into `history` before retrying a `needs_review` section.
7. A section directory without a manifest is retained under `history` before a clean rewrite attempt.
8. Repeated review failures stop at the configured review-cycle limit instead of creating an unbounded regeneration loop.

An additional cycle requires the explicit `resume --allow-extra-review-cycle` flag after human inspection.

The presence of a WAV is not an acceptance decision. State plus the section reports determine whether it can be stitched.
