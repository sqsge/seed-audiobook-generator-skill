# Seed Audiobook Generator Skill

This repository contains a Codex skill for turning plain fiction text into a Seed Audio 1.0-ready multi-character audiobook or audio-drama workflow.

The workflow uses a configurable planning model (r8 defaults to `dola-seed-2-1-turbo-260628`) and Seed Audio 1.0 for mixed audio generation. A deterministic compiler turns the plan into bounded prompts, and adaptive objective QA prevents long silent padding from reaching the final chapter.

## Repository Layout

```text
.
├── SKILL.md
├── README.md
├── scripts/
│   ├── audiobook_workflow.py
│   ├── long_text_batch_planner.py
│   ├── seed_audio_client.py
│   ├── llm_chat.py
│   ├── tts_client.py
│   ├── asr_client.py
│   └── common.py
├── story_configs/
│   └── moonlit_cloister_duel_en.json
├── docs/
│   └── seed_audio_product_intro.md
├── references/
│   ├── env.example
│   └── env-vars.md
└── examples/
```

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
ffmpeg -version
cp .env.example .env
python3 scripts/audiobook_workflow.py --story-config story_configs/moonlit_cloister_duel_en.json
```

`ffmpeg` is a required system dependency for WAV validation, tail trimming, and stitching. Install it with your operating-system package manager before running the workflow. For tests, install `python3 -m pip install -r requirements-dev.txt`.

Add `--generate` only when Seed Audio credentials are configured and you want to call the real audio model:

```bash
python3 scripts/audiobook_workflow.py --story-config story_configs/moonlit_cloister_duel_en.json --generate
```

## Smoke Test

Run this no-network syntax smoke test before publishing changes:

```bash
python3 -m py_compile scripts/*.py
```

Run the workflow without `--generate` to validate rewrite, chunking, and input-review behavior before calling the real audio model.

Plan a long source file before generation:

```bash
python3 scripts/long_text_batch_planner.py --source-file examples/moonlit_cloister_source.txt
```

This creates a long-run folder with `source_clean.txt`, `preprocessing_report.json`, `chapter_plan.json`, `batch_plan.json`, `manifest.json`, and `chapters/chapter_XXXX.txt`. The planned chapters are then processed by the existing `audiobook_workflow.py` generation path.

## Configuration

Use the root `.env.example` as the safe template. The expanded variable reference remains in `references/env.example` and `references/env-vars.md`. Do not commit `.env`.

The main variables are:

- `LLM_API_KEY` or `ARK_API_KEY`
- `LLM_BASE_URL`
- `SEED_AUDIO_REWRITE_MODEL`
- `SEED_AUDIO_MAX_STRUCTURAL_REPLANS` (default `3`)
- `SEED_AUDIO_MAX_STAGNANT_REPLANS` (default `2`)
- `LLM_MODEL`
- `SEED_AUDIO_API_KEY`
- `SEED_AUDIO_URL`
- `SEED_AUDIO_MODEL`
- `TTS_API_KEY` when generated reference audio is needed

## Output Policy

Generated outputs go under `outputs/` and are ignored by git. Keep credentials, generated audio, logs, caches, and local-only run artifacts out of the repository.

## Core Demo

The bundled demo is `The Duel in the Moonlit Cloister`, an original English magic-duel scene designed to demonstrate:

- source-only input
- scene parsing
- role-to-voice registry
- per-chunk speaker slot binding
- mixed generation with dialogue, narration, ambience, SFX, and music
- input review and QA

## Long-Form Batch Planning

Long-form support is a pre-generation planning layer. It does not replace the existing source-to-audio mechanism.

The planner:

- cleans structural noise without rewriting prose
- splits long text by semantic boundaries first and length fallback second
- writes one chapter text file per planned unit
- creates a batch plan for failure isolation
- writes a manifest for resumable production

The existing workflow still handles:

- source-unit parsing
- configurable planning-model rewrite and Prompt V2 compilation
- role-to-voice mapping
- Seed Audio 1.0 request generation
- audio chunk generation
- stitching and QA
