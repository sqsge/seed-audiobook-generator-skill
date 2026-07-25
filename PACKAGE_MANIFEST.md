# Seed Audiobook Generator Package Manifest

This package contains the reusable Seed Audiobook Generator workflow code and documentation.

## Included

- `SKILL.md`: Codex skill entrypoint.
- `README.md`: setup, quick start, smoke test, and workflow overview.
- `agents/openai.yaml`: Codex skill trigger metadata.
- `scripts/`: workflow implementation.
  - `audiobook_workflow.py`: main source-to-audio-drama workflow.
  - `chapter_audio_workflow.py`: full-chapter orchestration and recovery workflow.
  - `long_text_batch_planner.py`: long-text chapter and batch planner.
  - `seed_audio_client.py`: Seed Audio 1.0 client.
  - `llm_chat.py`: configurable Ark-compatible planning client.
  - `asr_client.py`: ASR quality gate client.
  - `tts_client.py`: optional reference voice helper.
  - `common.py`: shared environment and filesystem helpers.
- `story_configs/`: reusable story configuration examples.
- `examples/`: small source-only demo inputs.
- `docs/`: product workflow, long-text extension, and case documentation.
- `references/`: environment variable template and setup notes.

## Excluded

- Real `.env` files and credentials.
- `.git/` metadata.
- Generated outputs under `outputs/`.
- Existing package builds under `dist/`.
- Audio, video, images, logs, caches, and local temporary files.

## Smoke Test

Run from the package root:

```bash
python3 -m py_compile scripts/*.py
python3 scripts/audiobook_workflow.py --story-config story_configs/moonlit_cloister_duel_en.json
python3 scripts/long_text_batch_planner.py --source-file examples/moonlit_cloister_source.txt
```

Use `--generate` only after configuring Seed Audio credentials.
