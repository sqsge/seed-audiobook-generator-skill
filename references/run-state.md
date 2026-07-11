# Run State Contract

`outputs/skill_runs/<run-id>/run_state.json` is the authoritative state. Schema version 2 stores section and chunk states separately and is written atomically after every transition.

## Run States

- `planned`: source, sections, and voice registry exist.
- `running`: the runner holds the active lease.
- `prepared`: all Audio inputs passed the static gate; no audio was requested.
- `awaiting_pilot_approval`: representative pilot chunks passed automated checks and require listening approval.
- `pilot_approved`: a human approved the selected pilots.
- `needs_replan`: at least one input or rendered chunk must return to planning.
- `auto_replan_pilot`: a failed pilot section is being replanned by Seed 2 Pro from retained QA evidence.
- `blocked`: an executable or provider failed before a valid workflow decision.
- `interrupted`: execution ended while all completed artifacts were retained.
- `completed`: every chunk and section is accepted and the chapter is stitched.

## Chunk States

- `planned`
- `rewritten`
- `input_validated`
- `generating`
- `accepted`
- `needs_replan`

There is no unlimited retry state. One initial render and one repair are the maximum for one prompt. During pilots, a second audible failure triggers one automatic section replan and a fresh pilot set. If the replanned pilot fails, the run changes to `needs_replan` and waits for user action.

## Lease And Recovery

`run_lease.json` stores the runner pid and heartbeat. A live lease prevents duplicate workflows. `SIGINT` and `SIGTERM` mark the run interrupted before exit. If a process dies without running its handler, `reconcile` compares state with the lease pid and converts stale `running` to `interrupted` without calling providers.

Accepted chunks are immutable during normal resume. Existing Seed 2 Pro responses, prompts, audio, and QA reports are reused. Replaced audio remains under `07_audio_revisions`.
