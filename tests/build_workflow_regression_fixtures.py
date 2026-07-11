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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", choices=["stale-state", "bad-input"])
    args = parser.parse_args()
    if args.fixture == "stale-state":
        stale_state()
    else:
        bad_input()


if __name__ == "__main__":
    main()
