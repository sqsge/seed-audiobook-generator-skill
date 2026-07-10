# Performance QA Optimization Plan

## Branch Goal

This branch improves the current full-chapter audio-drama workflow after the baseline was archived on `main`.

The baseline can generate a complete chapter, but listening review found performance-quality defects in some sections:

- rushed narration during action-heavy passages
- clipped syllables or line endings
- hard chunk transitions
- occasional SFX/music masking speech
- unclear or unstable narration in weak sections

The goal of this branch is to add workflow-level safeguards for performance quality, not only coverage quality.

## Planned Workflow Changes

1. Add a `performance_pass` gate after existing duration, silence, ASR, and coverage checks. Implemented with `seed-2-0-lite-260428`, the currently available Ark model name for the requested 0428 Lite family.
2. Use an audio-capable reviewer model to review every generated chunk or stitched section for:
   - rushed delivery
   - clipped phonemes or cut-off line endings
   - hard edits at chunk boundaries
   - speech masked by SFX/music
   - unintelligible narration
3. Add boundary protection before generation:
   - do not end a chunk inside a spoken phrase, spell, shout, or emotional line
   - reserve a short natural room-tone tail at each chunk end
4. Reduce density for action-heavy scenes:
   - fewer dramatic events per request
   - clearer priority between speech, action SFX, ambience, and music
5. Add repair routing:
   - failed chunks should be regenerated with slower delivery and safer boundary prompts
- accepted chunks should be reused

## Implemented Strategy

- English action chunks now default to neutral speed rather than an elevated global speech-rate value.
- Seed 2.0 is explicitly told to express urgency with acting, music, and action sound, while preserving breaths, consonants, complete thoughts, and a natural ambience tail.
- Timing post-processing is disabled by default. The workflow no longer removes internal quiet audio or trims the stitched tail unless `SEED_AUDIO_POSTPROCESS_TIMING=true` is deliberately set.
- Each generated chunk is reviewed as audio, not inferred from prompt text. Major performance defects trigger a local prompt overlay and a lower-rate regeneration of only that chunk.
- If the reviewer service is unavailable, the run fails the performance gate without wasting extra generation retries; the generated artifacts remain available for diagnosis.

## Acceptance Standard

A full-chapter run should pass only when:

- source coverage passes
- ASR language/content gate passes
- silence/duration gate passes
- performance review passes for each section
- no section has severe rushed delivery, clipped speech, or hard transition failures

## Baseline Evidence

The current baseline was archived before this branch. Its known quality review is stored outside the package outputs in the workspace-level review artifacts:

- `outputs/audio_quality_review_seed_lite_260428/review_summary.md`

That review judged the full audio as not production-quality because some sections failed performance listening checks.
