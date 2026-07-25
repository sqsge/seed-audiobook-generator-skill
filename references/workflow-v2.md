# Workflow V2 Design

Workflow V2 is preventive first: the planner describes the scene, but deterministic code decides whether the plan is renderable before Seed Audio is called.

## Preventive path

1. Build a chapter audio bible and fixed voice registry.
2. Plan source-covered chunks with the configured model.
3. Derive each chunk's speech estimate, sound-event complexity, pre/post roll, target duration, and safe duration range.
4. Compile one chronological prompt and lint duplicated, contradictory, over-quiet, or provider-incompatible instructions.
5. Generate one high-complexity canary per section and reuse accepted canary audio.

## Delivery path

1. Decode and duration check.
2. Adaptive objective silence check.
3. Seed Lite listening review with objective evidence attached.
4. Pure trailing silent padding: archive, trim to a two-second natural tail, and re-audit.
5. Missing layers or mix defects: fresh chunk rerender and `needs_chunk_repair` if still bad.
6. Spoken-content, attribution, coverage, or structural defects: iterate the failed suffix under the structural replan budget.
7. Feed the last two round fingerprints, failure families, and comparable severity scores into the next plan. Stop before Audio when the new plan is identical.
8. Stop at `human_review_required` after two comparable non-improving structural rounds, an exhausted structural budget, or exhausted non-structural repair.
9. Re-scan every stitched section and the complete chapter before `completed`.

## Why the gate is adaptive

The workflow does not reject a one-second or ordinary dramatic pause. It combines absolute duration and share of output. The current hard bounds are leading over 5 seconds or 20%, an internal gap over 8 seconds, repeated 4-second gaps occupying over 5% of the output, and trailing over 8 seconds or 30%. If music or room tone remains audible, it does not appear as true near-silence and therefore is not penalized as a dramatic hold.

The six Chapter 2/3/4 regression intervals are stored in `tests/fixtures/workflow_v2_badcases.json`; all must remain hard failures.
