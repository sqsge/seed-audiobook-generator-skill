# Quality Profiles

The workflow separates generation validity from optional diagnostics and delivery review.

## Core Media Gate

Always enabled. It blocks unusable output and objectively excessive silence while preserving natural dramatic timing. Ordinary 1-3 second pauses are allowed. The adaptive hard thresholds combine seconds and output share: leading over 5 seconds or 20%, an internal gap over 8 seconds, repeated 4-second gaps occupying over 5% of the output, and trailing over 8 seconds or 30%.

The separate pre-generation input gate runs before this stage. It can block provider calls for incomplete narration, missing source coverage, voice conflicts, or prompt-budget violations.

Speaker attribution is evidence-gated separately from performance QA. Named dialogue requires high confidence plus deterministic support in the source attribution or adjacent source units. This prevents a planner from promoting a weak guess from `medium` to `high` merely to satisfy validation.

## Performance Review

- `balanced` (default): during batch delivery, blocks major rushed pacing, clipped endings, hard cuts, masked voices, mechanical narration, or overlapping voices. During Pilot, every hard boundary and every missing required music, ambience, or action-SFX layer blocks regardless of reviewer severity. Objective adaptive-silence hard failures block in both Pilot and Batch even when the reviewer says `pass` or calls the issue minor. Other minor observations are retained without retry.
- `required`: every reviewer failure and reviewer unavailability blocks delivery. Use only for deliberate strict evaluation.
- `diagnostic`: stores review findings but never blocks or regenerates.
- `off`: does not call the audio reviewer.

## ASR

- `off` (default): no ASR call and no ASR gate.
- `diagnostic`: stores transcript evidence but cannot block delivery or trigger regeneration.
- `required`: transcript failures may block and repair. Select this only when exact spoken-text coverage is an explicit deliverable.

ASR is not a reliable judge of music, ambience, SFX, voice naturalness, or boundary quality. Those belong to performance review or human listening.

## Retry Policy

Retry only a chunk id selected by an enabled gate. Keep the previous generation and report. For proven silent padding after complete speech, trim locally to a two-second natural tail and re-audit first. Missing music or ambience is not trim-repairable and receives one targeted provider rerender. Non-structural failures remain `needs_chunk_repair`; only spoken-content, attribution, coverage, or chunk-structure defects enter `needs_replan`. After repair cycles are exhausted, stop at `human_review_required` rather than spending on an automatic suffix replan.

Structural replans are a separate bounded convergence loop. The default maximum is three automatic failed-suffix rounds per section. A lower objective/reviewer severity score counts as progress; a different failure family resets the stagnation counter. Two comparable rounds without improvement stop for human direction. A plan with the same source mapping, roles, compiled prompt, duration, and render contract as its parent is rejected before Audio generation.

Every reviewer invocation is stored under `logs/performance_reviews/<chunk>/`
before another invocation may overwrite the provider's raw response. Tail-only
classification requires affirmative evidence that speech is complete; generic
mentions of dialogue near a score-tail defect must not suppress the local
finish, while explicit mid-word, mid-sentence, clipped-speech, or incomplete
dialogue evidence always forbids it.

If the reviewer is unavailable in balanced or diagnostic mode, preserve the audio for listening without a provider-driven retry. Representative pilot chunks still require explicit human approval before batch generation.

Preventive input quality follows the official natural-language prompt guide rather than fixed engineering quotas. A valid request describes music, characters, dialogue and motivated effects in chronological prose while preserving source meaning and reference bindings.

In balanced Pilot review, missing scene-required background music, ambience or action SFX remains a major audible failure. Any hard boundary is also blocking, but the repair instruction should remain natural rather than append a canned technical coda.
