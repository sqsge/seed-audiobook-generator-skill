# Quality Profiles

The workflow separates generation validity from optional diagnostics and delivery review.

## Core Media Gate

Always enabled. It blocks only unusable output such as missing, undecodable, or effectively empty audio. Prompt-shape and duration heuristics remain diagnostic evidence rather than automatic reasons to spend another generation.

The separate pre-generation input gate runs before this stage. It can block provider calls for incomplete narration, missing source coverage, voice conflicts, or prompt-budget violations.

Speaker attribution is evidence-gated separately from performance QA. Named dialogue requires high confidence plus deterministic support in the source attribution or adjacent source units. This prevents a planner from promoting a weak guess from `medium` to `high` merely to satisfy validation.

## Performance Review

- `balanced` (default): blocks only major rushed pacing, clipped endings, hard cuts, masked voices, mechanical narration, or overlapping voices. Minor observations are retained without retry.
- `required`: every reviewer failure and reviewer unavailability blocks delivery. Use only for deliberate strict evaluation.
- `diagnostic`: stores review findings but never blocks or regenerates.
- `off`: does not call the audio reviewer.

## ASR

- `off` (default): no ASR call and no ASR gate.
- `diagnostic`: stores transcript evidence but cannot block delivery or trigger regeneration.
- `required`: transcript failures may block and repair. Select this only when exact spoken-text coverage is an explicit deliverable.

ASR is not a reliable judge of music, ambience, SFX, voice naturalness, or boundary quality. Those belong to performance review or human listening.

## Retry Policy

Retry only a chunk id selected by an enabled gate. Keep the previous generation and report. One targeted repair is allowed. If a pilot repair still fails a major audible gate, archive its section and let Seed 2 Pro create one materially revised plan from the QA evidence. If the new pilot also fails, mark it `needs_replan` instead of generating it again.

If the reviewer is unavailable in balanced or diagnostic mode, preserve the audio for listening without a provider-driven retry. Representative pilot chunks still require explicit human approval before batch generation.

Preventive input quality follows the official natural-language prompt guide rather than fixed engineering quotas. A valid request describes music, characters, dialogue and motivated effects in chronological prose while preserving source meaning and reference bindings.

In balanced Pilot review, missing scene-required background music, ambience or action SFX remains a major audible failure. Any hard boundary is also blocking, but the repair instruction should remain natural rather than append a canned technical coda.
