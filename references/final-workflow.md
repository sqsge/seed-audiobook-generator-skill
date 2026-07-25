# Final Workflow Architecture

## Planning Layer

1. Normalize source text and preserve a source hash.
2. Split at semantic paragraph and scene boundaries.
3. Create one chapter-wide role and voice registry.
4. Generate one clean dry casting sample per role and require human approval.
5. Use the configured planner (default `dola-seed-2-1-turbo-260628`) to parse source units, record speaker evidence/confidence, and propose dramatic chunks.
6. Derive a render contract from spoken words, story events, and requested sound layers.
7. Compile and lint a chronological mixed-scene Audio prompt. The compiler owns duration bounds, audible pre/post roll, and the no-silent-padding rule.

Planning is complete only when every source unit appears exactly once and in order.

## Admission Layer

The static gate runs before scene generation. It verifies complete spoken sentences, narrator coverage, must-keep dialogue, supported speaker attribution, unique active voices, three-reference capacity, required sound layers, language lock, prompt budget, and the content-derived render contract. Prompt lint blocks provider-limit violations and records duplicate, contradictory, or over-quiet wording. A named-character line must be high confidence and independently supported by the quote attribution or an immediately adjacent source unit using the role's aliases. Model-reported confidence alone is never sufficient. A failed admission report forbids provider calls.

## Casting Layer

The runner generates dry samples from the chapter registry before section planning. Human approval is mandatory because provider labels and automated gender/age judgments do not prove that a voice is perceptually suitable for the role. Changing a registered speaker invalidates casting approval and requires new samples.

## Pilot Layer

Select distinct narration-heavy, dialogue-heavy, and action-heavy chunks. Generate and review only those chunks. Automated review verifies technical and major audible defects; a human still approves the voice identity, dramatic style, music balance, ambience, and overall naturalness.

## Batch Layer

After approval, select one high-complexity canary per section, generate and accept those first, then reuse them during the remaining sequential Batch. Each chunk stores its request, render contract, Prompt lint, audio, objective signal evidence, listening review, attempt count, and decision. Accepted chunks are skipped on resume.

The adaptive signal gate intentionally permits ordinary 1-3 second dramatic pauses. It warns on leading silence over 2 seconds, internal gaps over 4 seconds, and trailing silence over 4 seconds or 15%. It blocks leading silence over 5 seconds or 20%, an internal gap over 8 seconds, repeated 4-second gaps occupying over 5% of the output, and trailing silence over 8 seconds or 30%. A planned dramatic hold is valid only when the requested ambience or score remains audible; a truly silent interval is not exempt.

## Failure Routing

- Provider transport failure: retain state and resume the same stage.
- Configured planning-model calls run in an isolated child process with a parent-enforced timeout and configurable finite attempt count. A timeout never launches an automatic duplicate request.
- Casting samples also use a parent-enforced per-role timeout; completed role samples are checkpointed and reused on resume.
- Static source/structure failure: `needs_replan`, with no Audio call.
- Prompt generation follows the official natural chronological style. Music is described with style, instruments, rhythm and atmosphere; characters are described naturally on first appearance; sound effects are inserted where the story motivates them.
- The static gate does not prescribe sound-effect counts or canned timeline phrases. Audible sound quality is evaluated through representative pilots and human listening.
- Pure trailing silent padding after complete speech: retain about two seconds of natural tail, trim locally, and re-audit before spending on another render.
- Missing music/ambience, hard mix boundary, or other render failure: `needs_chunk_repair`, with one targeted fresh chunk rerender.
- Spoken-content, attribution, source-coverage, or chunk-structure failure: `needs_replan`.
- A structural Pilot or Batch failure: archive the section snapshot, preserve accepted prefix chunks, and ask the configured planner for a materially revised failed-suffix plan using current and prior QA rounds.
- Manual `replan` inherits retained failure evidence and accepts `--instruction` for a new human structural direction. `feedback: null` is not a valid recovery path when QA evidence exists.
- A tail-only score or ambience finish may receive one deterministic local trim/fade and another listening review. Any clipped word, sentence, dialogue, or narration remains a planning/generation failure.
- Retained Pilot failures may use the explicit `recover-local-tail` command only when append-only QA proves all speech is complete and the defect affects score or ambience alone. The original WAV and delivery report are archived before modification.
- `replan --full-section` is an explicit escape hatch for global coverage, attribution, or scene-structure defects; it is not the default response to one failed chunk.
- Failure after the allowed non-structural repair cycles: `human_review_required`; do not automatically turn it into a suffix replan.
- Structural replan convergence: allow up to `SEED_AUDIO_MAX_STRUCTURAL_REPLANS` rounds per section (default 3). Compare failure family/severity and parent/new plan fingerprints. Never render an identical plan again; stop after two comparable non-improving rounds or when the budget is exhausted.
- Full chapter generation begins only after the representative pilots pass automated review and receive explicit human approval.
- Process interruption: `interrupted`, then resume from the current chunk.

## Assembly Layer

Stitch a section only after every chunk is accepted. Stitch the chapter only after every section is accepted. Run the adaptive signal audit again on every stitched section and the full chapter; write `completed` only when this final audit passes. A final-audit failure becomes `human_review_required` with timestamp evidence instead of entering a blind restitch loop. Presence of an audio file alone never authorizes assembly.
