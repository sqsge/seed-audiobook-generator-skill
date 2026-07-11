---
name: seed-audiobook-generator
description: Use this skill to turn long English fiction into a resumable multi-character Seed Audio drama. It plans sections and chunks, locks chapter-wide voices, statically validates every Audio 1.0 input, generates representative pilot samples for human approval, then runs chunk-level generation, review, replanning, and stitching with immutable artifacts and crash recovery.
---

# Seed Audio Drama Generator

Use this skill for a complete fiction chapter or long scene that should become an English multi-character audio drama. The source text is the only required story input. A fixed voice registry is optional.

## Non-Negotiable Rules

- Use `scripts/audio_drama_skill.py` as the only user-facing runner.
- Never send raw long fiction directly to Seed Audio.
- Keep one chapter-level role-to-voice registry across every section and chunk.
- Bind no more than three active voices in one Audio request, with no duplicate provider speaker id in that request.
- Validate source coverage, complete spoken sentences, voice bindings, sound layers, and prompt budget before any Audio provider call.
- Reserve 300 characters of the Audio prompt limit for a possible repair instruction.
- Generate narration-heavy, dialogue-heavy, and action-heavy pilots before batch generation.
- Require explicit human pilot approval. Do not infer approval from technical QA.
- Allow one initial generation and at most one audio repair per chunk.
- Route a second audible failure to `needs_replan`; do not repeat the same prompt.
- ASR is off by default and is not part of normal delivery acceptance.
- Preserve every source, plan, prompt, response, audio revision, report, event, and state transition.
- Never package `.env`, generated runs, provider URLs, or credentials.

## Final Workflow

```text
source text
  -> semantic sections
  -> chapter voice registry
  -> dramatic chunks
  -> Seed 2 Pro director rewrite
  -> static Audio input gate
  -> three representative pilot chunks
  -> human pilot approval
  -> chunk-level batch generation
  -> technical and performance review
  -> accepted OR needs_replan
  -> section stitching
  -> chapter stitching
```

Each chunk follows this state path:

```text
planned -> rewritten -> input_validated -> generating -> accepted
                                                  \-> needs_replan
```

Read [references/final-workflow.md](references/final-workflow.md), [references/run-state.md](references/run-state.md), and [references/quality-profiles.md](references/quality-profiles.md) before changing the workflow.

## Commands

Start a new run. This prepares every section, applies the static input gate, generates three pilots, then stops for approval:

```bash
python3 scripts/audio_drama_skill.py run \
  --source-file path/to/chapter.txt \
  --source-title "Chapter title" \
  --run-id chapter_run_001
```

Use a reviewed chapter-wide voice registry:

```bash
python3 scripts/audio_drama_skill.py run \
  --source-file path/to/chapter.txt \
  --voice-registry path/to/voice_registry.json \
  --run-id chapter_run_001
```

Prepare and validate all director inputs without generating audio:

```bash
python3 scripts/audio_drama_skill.py run \
  --source-file path/to/chapter.txt \
  --run-id chapter_prepare_001 \
  --prepare-only
```

Inspect progress without calling a provider:

```bash
python3 scripts/audio_drama_skill.py status --run-id chapter_run_001
```

After listening to all selected pilot files, explicitly approve them:

```bash
python3 scripts/audio_drama_skill.py approve-pilot --run-id chapter_run_001
```

Continue batch generation after approval or process interruption:

```bash
python3 scripts/audio_drama_skill.py resume --run-id chapter_run_001
```

When a failed chunk requires new boundaries or a new director plan, archive and replan its complete section:

```bash
python3 scripts/audio_drama_skill.py replan \
  --run-id chapter_run_001 \
  --section-id section_003
```

Replanning preserves the previous section under `history/replan/`, clears its invalid active chunk states, and requires new pilot approval before batch generation resumes.

Repair stale state after an uncatchable process exit without calling any provider:

```bash
python3 scripts/audio_drama_skill.py reconcile --run-id chapter_run_001
```

## Stop Conditions

Stop before audio generation when:

- source units are missing, duplicated, or reordered;
- a spoken Narrator line is incomplete or ends on a dangling word;
- narrator-only source coverage is too sparse;
- the base prompt exceeds the repair-reserve budget;
- active voices exceed three or share one provider speaker id;
- language, continuity, voice serialization, ambience, music, or sound-design markers are missing.

Stop batch generation when:

- pilots have not been approved;
- a chunk fails decoding or is effectively empty;
- balanced listening review finds a major rushed delivery, clipped ending, hard cut, masked voice, mechanical narration, or overlapping voices;
- the same chunk still fails after its one permitted repair.

The last case becomes `needs_replan`. Replanning may shorten narration, reduce simultaneous sound instructions, change a chunk boundary, or split the dramatic window. It must not blindly append another repair sentence.

## State And Artifacts

Runs live under `outputs/skill_runs/<run-id>/`:

- `run_state.json`: authoritative schema-2 workflow state
- `run_lease.json`: active process lease and heartbeat
- `events.jsonl`: append-only transition history
- `source.txt`, `preprocessing_report.json`, `voice_registry.json`
- `inputs/section_*/`: immutable section source and story configuration
- `sections/section_*/02_source_units.json`: traceable source units
- `sections/section_*/05_director_prompt_chunks/`: exact Audio inputs
- `sections/section_*/06_generation_requests/`: voice bindings and generation metadata
- `sections/section_*/logs/pre_generation_input_gate.json`: provider-call admission decision
- `sections/section_*/logs/chunk_delivery/`: per-chunk technical and performance decisions
- `sections/section_*/07_audio_revisions/`: replaced generations
- `stitched/<run-id>_full.wav`: accepted final chapter audio

Do not infer state from WAV presence. `run_state.json` plus per-chunk reports determine whether audio may be stitched.

## Verification

Run before packaging or publishing:

```bash
PYTHONPYCACHEPREFIX=/tmp/seed-audio-pyc python3 -m py_compile scripts/*.py
PYTHONPYCACHEPREFIX=/tmp/seed-audio-pyc python3 -m unittest discover -s tests -v
python3 /Users/bytedance/Documents/project/skill_test/codex-workflows/skills/skill-workflow-regression/scripts/run_skill_workflow_cases.py tests/workflow_cases.json
```

Report the run id, workflow phase, accepted and failed chunk counts, pilot approval state, current item, exact stop gate, and final audio path when complete.
