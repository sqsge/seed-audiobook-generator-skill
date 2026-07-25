# Run State Contract

`outputs/skill_runs/<run-id>/run_state.json` is the authoritative state. Schema version 2 stores section and chunk states separately and is written atomically after every transition.

## Run States

- `planned`: source, sections, and voice registry exist.
- `awaiting_casting_approval`: every registered role has a dry sample and section planning is locked.
- `casting_approved`: a human approved the chapter-wide role-to-voice mapping.
- `running`: the runner holds the active lease.
- `prepared`: all Audio inputs passed the static gate; no audio was requested.
- `awaiting_pilot_approval`: representative pilot chunks passed automated checks and require listening approval.
- `pilot_approved`: a human approved the selected pilots.
- `needs_chunk_repair`: the plan remains valid but a render, mix, sound layer, or objective signal gate failed.
- `needs_replan`: source coverage, speaker attribution, spoken content, or chunk structure must return to planning.
- `structural_auto_replan`: a structurally failed Pilot or Batch suffix is being replanned from retained current and prior QA evidence.
- `human_review_required`: bounded non-structural repair cycles are exhausted.
- `blocked`: an executable or provider failed before a valid workflow decision.
- `interrupted`: execution ended while all completed artifacts were retained.
- `completed`: every chunk and section is accepted, the chapter is stitched, and `logs/final_chapter_audit.json` passes.

## Chunk States

- `planned`
- `rewritten`
- `input_validated`
- `generating`
- `accepted`
- `needs_chunk_repair`
- `needs_replan`

There is no unlimited retry state. A proven pure silent tail is trimmed locally to retain about two seconds and is audited again. Missing sound layers and other render defects use bounded chunk repair and never trigger suffix replan merely to spend another planning call. Structural failures receive up to the configured per-section budget, three automatic rounds by default. Exact plan repetition stops before Audio; two comparable non-improving rounds or an exhausted budget become `human_review_required` while retaining `needs_replan` evidence.

## Lease And Recovery

`run_lease.json` stores the runner pid and heartbeat. A live lease prevents duplicate workflows. `SIGINT` and `SIGTERM` mark the run interrupted before exit. If a process dies without running its handler, `reconcile` compares state with the lease pid and converts stale `running` to `interrupted` without calling providers.

Accepted chunks are immutable during normal resume. During justified structural replanning, accepted chunks before the failed boundary become `preserved_chunks`; their archived audio paths remain in state and are stitched before newly planned suffix chunks. Existing planner responses, prompts, audio, and QA reports are reused. Replaced audio remains under `07_audio_revisions`.
