---
name: seed-audiobook-generator
description: Use this skill to turn long fiction into a resumable multi-character Seed Audio drama. It plans sections and dramatic chunks, keeps a chapter-wide voice registry, performs Seed 2.0 Pro sound-director rewriting, generates and stitches Seed Audio, preserves every intermediate artifact and revision, and resumes after interruption without regenerating accepted sections.
---

# Resumable Seed Audio Drama

Use this skill for a complete fiction chapter or long scene that should become an English multi-character audio drama. The workflow is stateful: inspect or resume an existing run before starting a new one.

## Operating Rules

- Treat the source text as the only required content input.
- Create one immutable `run-id` for one source and production profile.
- Check `status` before `run` when a run may already exist.
- Never restart or duplicate a run merely because a provider call or review was interrupted.
- Reuse the chapter-wide voice registry for every section and chunk.
- Keep each Seed Audio request within the three-active-reference limit.
- Preserve source, section plans, voice registry, director prompts, requests, audio parts, revisions, reports, and event history.
- ASR is optional evidence. It is off by default and must not be used as the normal delivery gate.
- Use balanced performance review by default. Regenerate only chunks with major audible delivery defects.
- Never reveal or package `.env`, API keys, generated runs, or signed provider URLs.

## Primary Workflow

1. Clean the source and split it into semantic sections at paragraph and scene boundaries.
2. Infer or load one stable chapter-level role-to-voice registry.
3. Within each section, split by dramatic action and the maximum three active voices per request.
4. Use Seed 2.0 Pro to create chronological sound-director prompts containing narration, dialogue, ambience, music, and source-derived SFX.
5. Generate each chunk with Seed Audio, retaining successful existing chunks on resume.
6. Apply core media validation and balanced performance review.
7. Archive the previous audio and QA checkpoint before regenerating only a failed chunk.
8. Stitch accepted chunks into sections, then accepted sections into the full chapter.

Read [references/run-state.md](references/run-state.md) for state semantics and [references/quality-profiles.md](references/quality-profiles.md) before changing gates.

## Commands

Start or continue a run. If the `run-id` exists, this command loads its existing state:

```bash
python3 scripts/resumable_audio_drama.py run \
  --source-file path/to/chapter.txt \
  --source-title "Chapter title" \
  --run-id chapter_run_001
```

Inspect progress without calling any provider:

```bash
python3 scripts/resumable_audio_drama.py status --run-id chapter_run_001
```

Continue after interruption or a repaired configuration:

```bash
python3 scripts/resumable_audio_drama.py resume --run-id chapter_run_001
```

Prepare all director artifacts without generating Seed Audio:

```bash
python3 scripts/resumable_audio_drama.py run \
  --source-file path/to/chapter.txt \
  --run-id chapter_prepare_001 \
  --prepare-only
```

Use an approved fixed voice registry instead of inferring one:

```bash
python3 scripts/resumable_audio_drama.py run \
  --source-file path/to/chapter.txt \
  --voice-registry path/to/voice_registry.json \
  --run-id chapter_fixed_voices_001
```

Run ASR only when transcript evidence is explicitly needed:

```bash
python3 scripts/resumable_audio_drama.py run \
  --source-file path/to/chapter.txt \
  --run-id chapter_asr_diagnostic_001 \
  --asr-mode diagnostic
```

## Resume Decisions

- `accepted`: skip it.
- `prepared`: reuse its rewrite artifacts and begin generation.
- `running` or `interrupted`: reuse its manifest and completed audio chunks.
- `needs_review`: preserve the previous QA checkpoint, then retry only gated chunks.
- `failed`: keep the error and intermediate files; resume after the concrete cause is fixed.

The default permits at most two section-level review cycles. When that limit is reached, the state changes to `human_review_required` without another provider call.

After listening to the retained audio and reports, explicitly authorize one more paid cycle only when justified:

```bash
python3 scripts/resumable_audio_drama.py resume \
  --run-id chapter_run_001 \
  --allow-extra-review-cycle
```

Do not infer completion from the presence of WAV files alone. `run_state.json` is the chapter-level source of truth; each section manifest and QA report is the lower-level evidence.

## Default Acceptance

A section is accepted when:

- the final audio exists and decodes;
- no output chunk is missing or effectively empty;
- balanced review finds no major rushed delivery, clipped ending, hard cut, voice masking, mechanical narration, or overlapping voices;
- ASR passes only when `--asr-mode required` was explicitly selected.

Minor stylistic observations stay in reports and do not trigger regeneration. Reviewer unavailability does not delete generated audio; in balanced mode it remains reviewable instead of causing an automatic retry loop.

## Outputs

Runs are stored under `outputs/skill_runs/<run-id>/`:

- `run_state.json`: resumable chapter state
- `events.jsonl`: append-only progress history
- `source.txt`, `preprocessing_report.json`, `voice_registry.json`
- `inputs/section_*/`: immutable section source and story config
- `sections/section_*/`: source units, director prompts, generation requests, audio chunks, stitched section, and QA reports
- `history/`: interrupted rewrites and pre-retry QA checkpoints
- `sections/section_*/07_audio_revisions/`: replaced audio generations
- `stitched/<run-id>_full.wav`: completed chapter audio

## Verification

Before packaging or publishing this skill, run:

```bash
PYTHONPYCACHEPREFIX=/tmp/seed-audio-pyc python3 -m py_compile scripts/*.py
PYTHONPYCACHEPREFIX=/tmp/seed-audio-pyc python3 -m unittest discover -s tests -v
```

Report the run id, current state, accepted section count, section needing attention, final audio path if complete, and the exact gate responsible for any stop.
