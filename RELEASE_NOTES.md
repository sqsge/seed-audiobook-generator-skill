# 2026.07.22-r8-dev

This isolated release adds Workflow V2: configurable planner selection, content-derived render contracts, Prompt lint, per-section Batch canaries, adaptive silence gates, chunk-repair versus structural-replan routing, bounded multi-round structural convergence, and a blocking stitched-chapter final audit. It preserves the r7 baseline and historical runs unchanged.

## Prior release history

# 2026.07.21-r1

This release stabilizes the resumable audiobook workflow after the Chapter 2-4 customer-demo recovery audit.

## Workflow fixes

- Recover cached planner/static-gate failures locally and at most once per compatibility version.
- Never call the planner in cached-only mode when the cached response is invalid or missing.
- Keep planner/static validation separate from pilot audible-QA replanning.
- Persist pilot failure phase before interruption and support legacy selected-pilot recovery.
- Reuse partial-run source units only when the immutable stored source matches; fail closed when source proof is missing.
- Split oversized Audio requests locally below the 60-second safety ceiling, preferring narrative boundaries.
- Rebuild voice bindings after engineering splits and reject dialogue openers that leave movable narrative setup behind.
- Normalize evidence-backed speaker attribution, downgrade unsupported named dialogue to Narrator, and keep planner/static evidence rules aligned.
- Preserve model-omitted source units, source order, and legacy source-unit IDs during partial recovery.
- Support common OCR closing/paired quote forms, including contractions inside doubled-apostrophe quotes.
- Resolve engineering speaker repairs through actual registry role keys rather than hard-coded labels.

## Verification

- 92/92 unit tests passed.
- 3 workflow regression cases / 6 steps passed.
- Release check passed without warnings or blockers from a clean package copy.
- Chapter 2: 13/13 historical cached responses passed offline normalize, validation, request build, and static gate.
- Chapter 3: 6/6 historical cached responses passed the same offline replay.
- Independent audit found no remaining P0, P1, or P2 workflow defects in the repaired paths.
