# Long-Form Batch Decomposition Extension

This extension adds a pre-generation planning layer for long fiction or chapter-length source files. It is based on the long-form audiobook requirements in the reference document and the attached prototype skill, but it keeps this skill's existing source-to-audio, Seed Audio input, generation, stitching, and QA mechanisms.

## Why Add This Layer

Seed Audio 1.0 has practical request limits for prompt length and generated duration, so a full chapter or book cannot be sent as one request. Long-form production also needs restartable state: when a request fails, the workflow should know which chapter, batch, and audio part failed.

## What We Reuse

The current workflow remains responsible for:

- source-unit parsing
- Seed 2.0 Pro rewrite
- role-to-voice registry
- per-chunk `<<TGT_SPK1>>/<<TGT_SPK2>>/<<TGT_SPK3>>` binding
- Seed Audio 1.0 generation requests
- audio stitching
- quality analysis

## What The New Layer Adds

The new `scripts/long_text_batch_planner.py` script adds:

- source reading and structural cleanup
- `source_clean.txt`
- `preprocessing_report.json`
- semantic chapter planning
- `chapter_plan.json`
- one chapter text file per planned unit
- `batch_plan.json`
- top-level `manifest.json`

## Target Flow

```text
long source text
  -> long_text_batch_planner.py
  -> source_clean.txt
  -> chapter_plan.json
  -> chapters/chapter_XXXX.txt
  -> batch_plan.json
  -> audio_drama_skill.py run per chapter
  -> existing Audio 1.0 prompt generation
  -> existing audio generation and stitching
```

## Design Principles

- Splitting is not rewriting.
- The cleaned source should preserve wording, order, punctuation rhythm, and dialogue.
- Semantic boundaries come before fixed length.
- Fallback splitting is allowed only when a unit would exceed the configured maximum.
- A batch is a failure-isolation unit, not an audio-generation prompt.
- Character hints and dialogue maps are advisory metadata; the existing rewrite layer still performs final role binding.

## Planner Command

```bash
python3 scripts/long_text_batch_planner.py --source-file path/to/novel.txt
```

Useful options:

```bash
python3 scripts/long_text_batch_planner.py \
  --source-file path/to/novel.txt \
  --target-chars 900 \
  --max-chars 1800 \
  --batch-size 20
```

## Output Contract

```text
outputs/long_runs/<run_id>/
  manifest.json
  source_clean.txt
  preprocessing_report.json
  chapter_plan.json
  batch_plan.json
  chapters/
    chapter_0001.txt
    chapter_0002.txt
```

## Next Integration Step

The planner prepares long-form inputs. Run each planned chapter through the public `audio_drama_skill.py run` entry point, record the resulting run IDs, and perform final book-level stitching only after all chapter-level outputs pass QA.
