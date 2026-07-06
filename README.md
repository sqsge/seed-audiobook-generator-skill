# Seed Audiobook Generator Skill

This repository contains a Codex skill for turning plain fiction text into a Seed Audio 1.0-ready multi-character audiobook or audio-drama workflow.

The workflow uses Seed 2.0 Pro for structured rewrite and Seed Audio 1.0 for mixed audio generation, including narration, character dialogue, ambience, action SFX, and music cues.

## Repository Layout

```text
.
├── SKILL.md
├── README.md
├── scripts/
│   ├── audiobook_workflow.py
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
cp references/env.example .env
python3 scripts/audiobook_workflow.py --story-config story_configs/moonlit_cloister_duel_en.json
```

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

## Configuration

Use `references/env.example` as the safe template. Do not commit `.env`.

The main variables are:

- `LLM_API_KEY` or `ARK_API_KEY`
- `LLM_BASE_URL`
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
