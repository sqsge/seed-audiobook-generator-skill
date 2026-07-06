---
name: seed-audiobook-generator
description: Use this skill to turn plain fiction text into Seed Audio 1.0-ready audio-drama prompts and generated audiobook/audio-drama assets using Seed 2.0 Pro rewriting, voice registry binding, reference audio, chunk generation, stitching, and QA reports.
---

# Seed Audiobook Generator

Use this skill when the user wants to convert a novel or scripted scene into a multi-character audiobook or audio-drama demo with Seed 2.0 Pro and Seed Audio 1.0.

## What This Skill Does

- Accepts plain source text or a story configuration.
- Parses the source into traceable story units.
- Builds a voice registry for narrator and characters.
- Rewrites the source into Seed Audio 1.0 sound-director prompts.
- Binds active roles to `<<TGT_SPK1>>`, `<<TGT_SPK2>>`, and `<<TGT_SPK3>>` per chunk.
- Generates reference voices and scene audio when credentials are available.
- Stitches generated chunks and writes QA artifacts.

## Safety Rules

- Never reveal `.env` contents or API keys.
- Do not commit generated media, run folders, logs, or local credentials.
- Keep user-provided source text separate from generated outputs.
- Use the smallest representative demo when validating expensive model calls.
- For copyrighted source material, use original demo text, public-domain text, or user-cleared text.

## Setup

1. Work from the skill root.
2. Copy `references/env.example` to `.env` and fill only the variables required for the current run.
3. Check `references/env-vars.md` before asking the user for credentials.

Required for rewrite:

- `LLM_API_KEY` or `ARK_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`

Required for audio generation:

- `SEED_AUDIO_API_KEY`
- `SEED_AUDIO_URL`
- `SEED_AUDIO_MODEL`

Optional for generated reference audio:

- `TTS_API_KEY`
- `TTS_SPEAKER`
- role-specific `SEED_AUDIO_SPEAKER_*` values when using fixed voices

## Main Commands

Dry-run rewrite and input review using the bundled demo:

```bash
python3 scripts/audiobook_workflow.py
```

Run the bundled magic-duel demo and generate audio:

```bash
python3 scripts/audiobook_workflow.py --story-config story_configs/moonlit_cloister_duel_en.json --generate
```

Run a new source text file:

```bash
python3 scripts/audiobook_workflow.py --source-file path/to/source.txt --language en --source-title "Demo Scene"
```

Resume audio generation from an existing run:

```bash
python3 scripts/audiobook_workflow.py --resume-run-id RUN_ID --generate
```

## Expected Outputs

The workflow writes outputs under `outputs/runs/<run_id>/`:

- source excerpt
- source units
- scene parse
- voice registry
- director prompt chunks
- generation requests
- audio chunks
- stitched final audio
- input review and QA reports

Generated outputs are intentionally ignored by git.

## Voice Binding Rule

Do not treat `<<TGT_SPK1>>`, `<<TGT_SPK2>>`, and `<<TGT_SPK3>>` as global character IDs. They are per-request reference-audio slots.

For each chunk:

1. Select only the roles active in that chunk.
2. Pass their reference audio in a stable order.
3. Bind dialogue lines to the numeric slot for that chunk.
4. Keep the global role registry stable across chunks.

## Reporting

After a run, report:

- source title
- run id
- chunk count
- whether generation was dry-run or real audio
- final audio path when generated
- input review status
- any failed chunks and likely cause

