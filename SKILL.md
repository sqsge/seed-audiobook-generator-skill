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
- Generate clean dry casting samples for every registered role and require explicit human casting approval before section planning or batch generation.
- Require attribution evidence and confidence for every quoted source unit. Never bind low-confidence unattributed dialogue to a named character.
- Verify named dialogue against source attribution or immediately adjacent source units; never trust model-reported confidence by itself.
- Bind no more than three active voices in one Audio request, with no duplicate provider speaker id in that request.
- Validate source coverage, complete spoken sentences, voice bindings, sound layers, and prompt budget before any Audio provider call.
- Follow the official Audio 1.0 prompt formula: music opening, natural first-appearance character description, quoted line, naturally timed sound effect, then the next story event.
- Use the configurable planning model (`SEED_AUDIO_REWRITE_MODEL`, default `dola-seed-2-1-turbo-260628`) to understand the source; then compile its prose into a bounded render contract. Do not trust open-ended duration or silent-tail wording directly.
- Derive chunk duration, pre/post roll, and audible-bed requirements from actual spoken content and story events. Do not impose fixed SFX or narration counts.
- Run Prompt V2 lint before any Audio call. Block impossible provider budgets; retain low-audibility stacking, duplicate directives, and sound-layer contradictions as explicit warnings.
- Generate a soundscape-oriented pilot, a voice/dialogue pilot, and a combined action pilot before batch generation.
- Require explicit human pilot approval. Do not infer approval from technical QA.
- Run one reusable high-complexity canary per section before the remaining Batch chunks.
- Allow one initial generation, deterministic silent-tail trim when objectively safe, and at most one targeted provider repair per chunk cycle.
- Treat any missing required music, ambience, action SFX, or hard output boundary as a Pilot failure even when speech itself is intelligible.
- Route missing layers, silent padding, and mix/boundary defects to `needs_chunk_repair`; reserve failed-suffix replan for source coverage, speaker, spoken-content, or structural chunk failures.
- Allow structural failed-suffix replanning to converge over a configurable bounded budget (three automatic rounds per section by default). Stop early when two comparable rounds show no measurable improvement or when a new plan compiles to the same fingerprint.
- Stop for user action when the structural budget is exhausted or convergence stalls. Never start full-chapter production before explicit pilot approval.
- ASR is off by default and is not part of normal delivery acceptance.
- Preserve every source, plan, prompt, response, audio revision, report, event, and state transition.
- Never package `.env`, generated runs, provider URLs, or credentials.

## Final Workflow

```text
source text
  -> semantic sections
  -> chapter voice registry
  -> dry role casting samples
  -> human casting approval
  -> dramatic chunks
  -> configurable planning model director rewrite
  -> render-contract compiler + Prompt V2 lint
  -> static Audio input gate
  -> three representative pilot chunks
  -> automated review
       -> structural failure: configured planner iterates the failed suffix within its convergence budget
       -> passed: human pilot approval
  -> chunk-level batch generation
  -> technical + adaptive signal + performance review
  -> accepted OR needs_chunk_repair OR needs_replan
  -> section stitching
  -> chapter stitching
  -> blocking chapter final audit
```

Each chunk follows this state path:

```text
planned -> rewritten -> input_validated -> section_canary -> generating -> accepted
                                                       \-> needs_chunk_repair
                                                       \-> needs_replan
```

Read [references/workflow-v2.md](references/workflow-v2.md), [references/final-workflow.md](references/final-workflow.md), [references/run-state.md](references/run-state.md), and [references/quality-profiles.md](references/quality-profiles.md) before changing the workflow.

## Commands

Start a new run. This creates the chapter registry and dry casting samples, then stops for casting approval:

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

After listening to every dry role sample, approve the casting:

```bash
python3 scripts/audio_drama_skill.py approve-casting --run-id chapter_run_001
python3 scripts/audio_drama_skill.py resume --run-id chapter_run_001
```

To change a run's reviewed registry without starting a second run, archive the prior registry and affected samples, invalidate casting approval, and update all immutable section inputs before resuming casting:

```bash
python3 scripts/audio_drama_skill.py update-casting-registry \
  --run-id chapter_run_001 \
  --voice-registry path/to/reviewed_voice_registry.json
python3 scripts/audio_drama_skill.py resume --run-id chapter_run_001
```

Only roles whose registry entries changed are regenerated. Unchanged casting samples are reused, and the prior evidence remains under `history/casting_registry/`.

Runs in the same controlled batch may reuse already generated casting samples without another provider call only when their complete role registries are identical. The import records the source run and audio SHA-256 and still requires human casting approval:

```bash
python3 scripts/audio_drama_skill.py import-casting-samples \
  --run-id chapter_run_001 \
  --from-run-dir path/to/source/run
```

The resumed run prepares every section, applies the attribution and static Audio input gates, generates representative pilots, then stops again for pilot approval.

After casting approval, prepare and validate all director inputs without generating scene audio:

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

Change the bounded convergence policy for an existing run explicitly:

```bash
python3 scripts/audio_drama_skill.py resume \
  --run-id chapter_run_001 \
  --max-structural-replans 3 \
  --max-stagnant-replans 2
```

Provider time budgets are finite and configurable. `SEED_AUDIO_REWRITE_MODEL` selects the planner, `SEED_REWRITE_TIMEOUT` controls each isolated planning attempt, `SEED_REWRITE_ATTEMPTS` controls its maximum attempt count, and `SEED_CASTING_TIMEOUT` controls each isolated role sample. Completed artifacts remain resumable after timeout.

Structural Pilot or Batch failures archive the current section snapshot, freeze every accepted chunk before the failure, and replan only the failed suffix using accumulated QA evidence. The default budget is three automatic rounds per section:

```bash
python3 scripts/audio_drama_skill.py run \
  --source-file path/to/chapter.txt \
  --run-id chapter_run_001 \
  --max-structural-replans 3
```

If the budget is exhausted or convergence stops, provide an explicit direction after reviewing the retained artifacts:

```bash
python3 scripts/audio_drama_skill.py replan \
  --run-id chapter_run_001 \
  --section-id section_003 \
  --instruction "Split before the final dialogue and keep the closing ambience audible instead of extending silence."
```

Replanning preserves every previous section under `history/replan/`, writes the current and prior QA rounds into the planning input, and reuses accepted prefix audio during final stitching. `replan_history` stores failure families, comparable severity scores, parent/new plan fingerprints, and convergence decisions. Exact plan repetition stops before another Audio call. Use `--full-section` only when source coverage, speaker attribution, or global scene structure is invalid and the accepted prefix must intentionally be discarded.

Repair stale state after an uncatchable process exit without calling any provider:

```bash
python3 scripts/audio_drama_skill.py reconcile --run-id chapter_run_001
```

When retained Pilot QA proves that speech is complete and only the score or
ambience tail is cut, apply the one permitted deterministic local finish and
review it again without another Audio generation:

```bash
python3 scripts/audio_drama_skill.py recover-local-tail \
  --run-id chapter_run_001 \
  --section-id section_003 \
  --chunk-id chunk_004
```

This command is restricted to a selected Pilot chunk already in
`needs_replan`. It archives the original WAV and delivery report, never resets
the failed-suffix replan counter, and cannot be used for clipped speech.

## Stop Conditions

Stop before audio generation when:

- source units are missing, duplicated, or reordered;
- a spoken Narrator line is incomplete or ends on a dangling word;
- narrator-only source coverage is too sparse;
- the base prompt exceeds the repair-reserve budget;
- the estimated audio-drama duration ceiling exceeds the configurable safe delivery window (60 seconds by default);
- active voices exceed three or share one provider speaker id;
- language, continuity, voice serialization, ambience, music, or sound-design markers are missing.

Stop batch generation when:

- pilots have not been approved;
- a chunk fails decoding or is effectively empty;
- balanced listening review finds a major rushed delivery, clipped ending, hard cut, masked voice, mechanical narration, or overlapping voices;
- objective audio evidence finds leading silence over 5 seconds or 20%, an internal gap over 8 seconds, repeated 4-second gaps occupying over 5% of the output, or trailing silence over 8 seconds or 30%;
- the same non-structural chunk still fails after its repair cycles;
- the stitched section or chapter fails the same adaptive final audit.

Normal 1-3 second dramatic pauses are allowed. Warnings do not block by themselves. Render and mix failures become `needs_chunk_repair`; `needs_replan` is reserved for structural failures and must inherit stored QA feedback. Pure silent padding after complete speech may be trimmed to a two-second natural tail and audited again; missing music, clipped speech, or incomplete dialogue cannot be fixed by trimming.

## State And Artifacts

Runs live under `outputs/skill_runs/<run-id>/`:

- `run_state.json`: authoritative schema-2 workflow state
- `run_lease.json`: active process lease and heartbeat
- `events.jsonl`: append-only transition history
- `source.txt`, `preprocessing_report.json`, `voice_registry.json`
- `inputs/section_*/`: immutable section source and story configuration
- `sections/section_*/02_source_units.json`: traceable source units
- `sections/section_*/05_director_prompt_chunks/`: exact Audio inputs
- `sections/section_*/06_generation_requests/`: voice bindings, generation metadata, `render_plan_contract`, and `prompt_lint`
- `sections/section_*/logs/pre_generation_input_gate.json`: provider-call admission decision
- `sections/section_*/logs/chunk_delivery/`: per-chunk technical and performance decisions
- `sections/section_*/07_audio_revisions/`: replaced generations
- `logs/final_chapter_audit.json`: blocking stitched-section and chapter signal audit
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
