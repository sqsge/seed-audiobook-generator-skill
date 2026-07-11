#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import audiobook_workflow as workflow


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def configure(section_dir: Path, story_config: Path) -> None:
    config = read_json(story_config)
    workflow.apply_story_config(config)
    workflow.apply_voice_registry_file(section_dir / "04_voice_registry.json")


def part_path(section_dir: Path, chunk_id: str) -> Path:
    return section_dir / "07_audio_parts" / f"{chunk_id}.wav"


def technical_gate(section_dir: Path, chunk_id: str) -> dict:
    part = part_path(section_dir, chunk_id)
    validation = workflow.validate_audio([part]).get(str(part.relative_to(workflow.ROOT)), {})
    duration = workflow.audio_duration(part)
    passed = bool(validation.get("ffmpeg_decode_ok")) and duration is not None and duration >= 1.0
    return {
        "status": "pass" if passed else "fail",
        "chunk_id": chunk_id,
        "path": str(part),
        "duration_sec": duration,
        "ffmpeg_decode_ok": bool(validation.get("ffmpeg_decode_ok")),
    }


def review_once(section_dir: Path, chunk_id: str, mode: str) -> dict:
    report = workflow.performance_audio_audit(section_dir, [part_path(section_dir, chunk_id)])
    return workflow.performance_gate(report, mode)


def generate_chunk(section_dir: Path, chunk_id: str, performance_mode: str, force: bool = False) -> tuple[int, dict]:
    request_path = section_dir / "06_generation_requests" / f"{chunk_id}.json"
    if not request_path.exists():
        payload = {"status": "failed", "reason": "missing_generation_request", "chunk_id": chunk_id}
        write_json(section_dir / "logs" / "chunk_delivery" / f"{chunk_id}.json", payload)
        return 2, payload

    workflow.generate_reference_audio(section_dir)
    workflow.generate_scene_audio(
        section_dir,
        force_chunk_ids={chunk_id} if force else None,
        only_chunk_ids={chunk_id},
    )
    technical = technical_gate(section_dir, chunk_id)
    if technical["status"] != "pass":
        payload = {"status": "failed", "reason": "technical_gate", "technical": technical}
        write_json(section_dir / "logs" / "chunk_delivery" / f"{chunk_id}.json", payload)
        return 2, payload

    performance = review_once(section_dir, chunk_id, performance_mode)
    failed = set(performance.get("gate_failed_chunk_ids", []))
    if chunk_id in failed:
        workflow.generate_scene_audio(
            section_dir,
            force_chunk_ids={chunk_id},
            repair_notes=workflow.performance_repair_notes(performance),
            only_chunk_ids={chunk_id},
        )
        technical = technical_gate(section_dir, chunk_id)
        performance = review_once(section_dir, chunk_id, performance_mode)

    accepted = technical["status"] == "pass" and performance.get("gate_status") in {"pass", "skipped"}
    payload = {
        "status": "accepted" if accepted else "needs_replan",
        "chunk_id": chunk_id,
        "technical": technical,
        "performance": performance,
        "attempt_policy": "one initial generation plus at most one audio repair; a second failure requires replanning",
    }
    report_path = section_dir / "logs" / "chunk_delivery" / f"{chunk_id}.json"
    write_json(report_path, payload)
    return (0 if accepted else 3), payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and review one prepared Seed Audio chunk.")
    parser.add_argument("--section-dir", required=True)
    parser.add_argument("--story-config", required=True)
    parser.add_argument("--chunk-id", required=True)
    parser.add_argument("--performance-mode", choices=["off", "diagnostic", "balanced", "required"], default="balanced")
    parser.add_argument("--force", action="store_true", help="Archive any existing chunk audio and generate a new render.")
    args = parser.parse_args()
    section_dir = Path(args.section_dir).expanduser().resolve()
    configure(section_dir, Path(args.story_config).expanduser().resolve())
    code, payload = generate_chunk(section_dir, args.chunk_id, args.performance_mode, force=args.force)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
