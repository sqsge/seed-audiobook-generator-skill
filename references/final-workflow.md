# Final Workflow Architecture

## Planning Layer

1. Normalize source text and preserve a source hash.
2. Split at semantic paragraph and scene boundaries.
3. Create one chapter-wide role and voice registry.
4. Use Seed 2 Pro to parse source units and propose dramatic chunks.
5. Compile chronological mixed-scene Audio prompts.

Planning is complete only when every source unit appears exactly once and in order.

## Admission Layer

The static gate runs before Seed Audio. It verifies complete spoken sentences, narrator coverage, must-keep dialogue, unique active voices, three-reference capacity, required sound layers, language lock, and a 2700-character base prompt budget. A failed admission report forbids provider calls.

## Pilot Layer

Select distinct narration-heavy, dialogue-heavy, and action-heavy chunks. Generate and review only those chunks. Automated review verifies technical and major audible defects; a human still approves the voice identity, dramatic style, music balance, ambience, and overall naturalness.

## Batch Layer

After approval, generate one chunk at a time. Each chunk stores its own request, audio, technical gate, performance review, attempt count, and decision. Accepted chunks are skipped on resume.

## Failure Routing

- Provider transport failure: retain state and resume the same stage.
- Static input failure: `needs_replan`, with no Audio call.
- Prompt generation follows the official natural chronological style. Music is described with style, instruments, rhythm and atmosphere; characters are described naturally on first appearance; sound effects are inserted where the story motivates them.
- The static gate does not prescribe sound-effect counts or canned timeline phrases. Audible sound quality is evaluated through representative pilots and human listening.
- First audible failure: one targeted audio repair.
- Second audible failure in a pilot: archive the section and ask Seed 2 Pro for one materially revised plan using the QA evidence, then generate new pilots.
- Failure after that automatic replan: `needs_replan`; stop for user review and never render the same prompt a third time.
- Full chapter generation begins only after the representative pilots pass automated review and receive explicit human approval.
- Process interruption: `interrupted`, then resume from the current chunk.

## Assembly Layer

Stitch a section only after every chunk is accepted. Stitch the chapter only after every section is accepted. Presence of an audio file alone never authorizes assembly.
