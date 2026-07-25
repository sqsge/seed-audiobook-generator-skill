#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def stale_state() -> None:
    run_dir = ROOT / "outputs" / "skill_runs" / "regression_stale_state"
    write_json(
        run_dir / "run_state.json",
        {
            "schema_version": 2,
            "run_id": "regression_stale_state",
            "status": "running",
            "current_stage": "generate_chunk",
            "sections": [],
            "pilot": {"approved": False},
        },
    )
    write_json(run_dir / "run_lease.json", {"pid": 99999999, "heartbeat_at": "stale"})


def bad_input() -> None:
    section = ROOT / "outputs" / "workflow_regression" / "bad_input"
    write_json(section / "02_source_units.json", [{"source_unit_id": "s0001"}])
    write_json(section / "04_voice_registry.json", {"voices": [{"role": "Narrator", "speaker": "voice-a"}]})
    write_json(
        section / "06_generation_requests" / "chunk_001.json",
        {
            "chunk_id": "chunk_001",
            "source_unit_ids": ["s0001"],
            "active_roles": ["Narrator"],
            "text_prompt": (
                "All audible speech must be English only. Do not translate or speak Chinese.\n"
                "Voice continuity: Narrator uses <<TGT_SPK1>>. one voice at a time; no overlapping narration and dialogue.\n"
                "Ambient sound: wind. Background music: strings. Sound design: footsteps.\n"
                'Narrator (actor is <<TGT_SPK1>>, tense): "Students huddled against the"'
            ),
            "input_metrics": {"narrator_unit_count": 1, "dialogue_unit_count": 0},
            "expected_duration_sec": {"min": 10, "max": 30},
        },
    )


def localized_replan() -> None:
    run_dir = ROOT / "outputs" / "skill_runs" / "regression_localized_replan"
    section_dir = run_dir / "sections" / "section_001"
    accepted_audio = section_dir / "07_audio_parts" / "chunk_001.wav"
    accepted_audio.parent.mkdir(parents=True, exist_ok=True)
    accepted_audio.write_bytes(b"accepted-audio")
    write_json(
        section_dir / "02_source_units.json",
        [
            {"source_unit_id": "s0001", "source_text": "Accepted opening."},
            {"source_unit_id": "s0002", "source_text": "Failed ending."},
        ],
    )
    write_json(section_dir / "06_generation_requests/chunk_001.json", {"chunk_id": "chunk_001", "source_unit_ids": ["s0001"]})
    write_json(section_dir / "06_generation_requests/chunk_002.json", {"chunk_id": "chunk_002", "source_unit_ids": ["s0002"]})
    write_json(
        run_dir / "inputs/section_001/story_config.json",
        {"source_excerpt": "Accepted opening. Failed ending."},
    )
    write_json(
        run_dir / "run_state.json",
        {
            "schema_version": 2,
            "run_id": "regression_localized_replan",
            "status": "needs_replan",
            "sections": [
                {
                    "section_id": "section_001",
                    "status": "needs_replan",
                    "work_dir": "sections/section_001",
                    "story_config": "inputs/section_001/story_config.json",
                    "chunks": [
                        {
                            "chunk_id": "chunk_001",
                            "chunk_key": "section_001/chunk_001",
                            "status": "accepted",
                            "audio": str(accepted_audio),
                        },
                        {
                            "chunk_id": "chunk_002",
                            "chunk_key": "section_001/chunk_002",
                            "status": "needs_replan",
                            "replan_feedback": {"summary": "spoken ending remains clipped"},
                        },
                    ],
                }
            ],
            "pilot": {"approved": True, "selected_chunk_keys": []},
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", choices=["stale-state", "bad-input", "localized-replan"])
    args = parser.parse_args()
    if args.fixture == "stale-state":
        stale_state()
    elif args.fixture == "bad-input":
        bad_input()
    else:
        localized_replan()


if __name__ == "__main__":
    main()
