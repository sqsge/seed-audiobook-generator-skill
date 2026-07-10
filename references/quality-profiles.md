# Quality Profiles

The workflow separates generation validity from optional diagnostics and delivery review.

## Core Media Gate

Always enabled. It blocks only unusable output such as missing, undecodable, or effectively empty audio. Prompt-shape and duration heuristics remain diagnostic evidence rather than automatic reasons to spend another generation.

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

Retry only chunk ids selected by an enabled gate. Keep the previous generation and report. If the reviewer is unavailable in balanced or diagnostic mode, preserve the audio for listening and continue without provider-driven regeneration.

The chapter runner defaults to two section-level review cycles. Reaching the limit requires inspection of the retained artifacts before any additional paid generation.
