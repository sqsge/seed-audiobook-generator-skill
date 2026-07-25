#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
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


def review_once(section_dir: Path, chunk_id: str, mode: str, phase: str = "batch") -> dict:
    report = workflow.performance_audio_audit(section_dir, [part_path(section_dir, chunk_id)])
    report = workflow.attach_objective_audio_evidence(report, [part_path(section_dir, chunk_id)])
    return workflow.performance_gate(report, mode, phase=phase)


def snapshot_review_attempt(
    section_dir: Path,
    chunk_id: str,
    label: str,
    performance: dict,
    technical: dict,
) -> str:
    """Persist each reviewer result before the next call overwrites its raw response."""
    review_root = section_dir / "logs" / "performance_reviews" / chunk_id
    attempt_number = len(list(review_root.glob("attempt_*"))) + 1
    attempt_dir = review_root / f"attempt_{attempt_number:03d}_{label}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    audio = part_path(section_dir, chunk_id)
    audio_sha256 = hashlib.sha256(audio.read_bytes()).hexdigest() if audio.exists() else None
    raw_response = section_dir / "logs" / f"performance_{chunk_id}_response.txt"
    raw_copy = None
    if raw_response.exists():
        raw_copy = attempt_dir / "raw_response.txt"
        shutil.copy2(raw_response, raw_copy)
    write_json(
        attempt_dir / "review.json",
        {
            "attempt": attempt_number,
            "label": label,
            "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "audio": str(audio),
            "audio_sha256": audio_sha256,
            "technical": technical,
            "performance": performance,
            "raw_response": str(raw_copy) if raw_copy else None,
        },
    )
    return str(attempt_dir.relative_to(section_dir))


def is_tail_finish_only_failure(report: dict, chunk_id: str) -> bool:
    item = next((entry for entry in report.get("chunks", []) if entry.get("chunk_id") == chunk_id), {})
    issues = item.get("issues", [])
    tail_only_types = {"hard_boundary_or_abrupt_cut", "click_or_pop_at_boundary"}
    if not issues or not {issue.get("type") for issue in issues}.issubset(tail_only_types):
        return False
    evidence = " ".join(
        [str(item.get("summary", "")), str(item.get("repair_instruction", ""))]
        + [str(issue.get("evidence", "")) for issue in issues]
    ).lower()
    speech_damage = (
        "mid-sentence", "mid sentence", "mid-word", "mid word", "clipped word",
        "clipped sentence", "clipped speech", "truncated word", "truncated sentence",
        "truncated speech", "cuts off speech", "cuts off the dialogue", "incomplete sentence",
        "incomplete dialogue", "incomplete narration", "final phoneme",
    )
    finish_markers = ("fade", "score", "music", "ambience", "trailing silence", "hard cut", "abrupt cut")
    speech_complete_markers = (
        "after all speech is complete", "after the final dialogue line", "after the final spoken line",
        "all dialogue performances", "all speech is complete", "only the final chunk boundary",
        "only the closing", "background score", "background underscore", "trailing fade",
        "spoken dialogue is clear", "spoken dialogue performance", "free of speech artifacts",
        "dialogue clarity", "required foreground sound effects are all correctly executed",
    )
    return (
        not any(marker in evidence for marker in speech_damage)
        and any(marker in evidence for marker in finish_markers)
        and any(marker in evidence for marker in speech_complete_markers)
    )


def delivery_failure_status(report: dict, chunk_id: str) -> str:
    """A frozen script never changes structure because a render failed."""
    return "repair_audio"


def is_nonblocking_minor_boundary(report: dict, chunk_id: str) -> bool:
    """A complete spoken take with objective pass may retain a minor score-tail warning."""
    objective = report.get("objective_audio_by_chunk", {}).get(chunk_id, {})
    item = next((entry for entry in report.get("chunks", []) if entry.get("chunk_id") == chunk_id), {})
    issues = item.get("issues", [])
    if objective.get("hard_reasons") or not issues:
        return False
    soundscape_only_types = {
        "missing_required_background_music",
        "missing_required_ambience",
        "missing_required_sfx",
        "weak_background_music",
        "weak_ambience",
        "weak_sfx",
    }
    if objective.get("status") == "pass" and all(issue.get("type") in soundscape_only_types for issue in issues):
        return True
    finish_or_mix_types = {
        "hard_boundary_or_abrupt_cut",
        "voice_masked_by_music_or_sfx",
    }
    evidence = " ".join(str(issue.get("evidence", "")) for issue in issues).lower()
    speech_damage = (
        "mid-sentence", "mid sentence", "mid-word", "mid word", "clipped word",
        "truncated speech", "incomplete sentence", "uncompleted dialogue",
        "cuts off dialogue", "missing dialogue", "unintelligible", "gibberish",
    )
    if all(issue.get("type") in finish_or_mix_types for issue in issues) and not any(marker in evidence for marker in speech_damage):
        return True
    if not all(issue.get("type") == "hard_boundary_or_abrupt_cut" and issue.get("severity") == "minor" for issue in issues):
        return False
    return not any(marker in evidence for marker in speech_damage) and "final spoken" in evidence


def generate_chunk(
    section_dir: Path,
    chunk_id: str,
    performance_mode: str,
    force: bool = False,
    phase: str = "batch",
) -> tuple[int, dict]:
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

    performance = review_once(section_dir, chunk_id, performance_mode, phase=phase)
    review_attempts = [snapshot_review_attempt(section_dir, chunk_id, "initial", performance, technical)]
    local_silence_trim = None
    objective = performance.get("objective_audio_by_chunk", {}).get(chunk_id, {})
    if objective.get("repair_class") == "local_tail_trim_then_reaudit":
        audio = part_path(section_dir, chunk_id)
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
        archive_dir = section_dir / "07_audio_revisions" / chunk_id / f"{stamp}-local-silence-trim"
        counter = 1
        while archive_dir.exists():
            archive_dir = archive_dir.with_name(f"{stamp}-local-silence-trim-{counter}")
            counter += 1
        archive_dir.mkdir(parents=True)
        shutil.copy2(audio, archive_dir / audio.name)
        before = workflow.audio_duration(audio)
        workflow.trim_trailing_silence(audio, max_tail_sec=2.0)
        after = workflow.audio_duration(audio)
        local_silence_trim = {
            "status": "applied" if before is not None and after is not None and after < before - 0.25 else "skipped",
            "duration_before_sec": before,
            "duration_after_sec": after,
            "retained_natural_tail_sec": 2.0,
            "original_audio_archive": str(archive_dir / audio.name),
        }
        if local_silence_trim["status"] == "applied":
            technical = technical_gate(section_dir, chunk_id)
            performance = review_once(section_dir, chunk_id, performance_mode, phase=phase)
            review_attempts.append(snapshot_review_attempt(section_dir, chunk_id, "local_silence_trim", performance, technical))
    failed = set(performance.get("gate_failed_chunk_ids", []))
    if chunk_id in failed:
        repair_notes = workflow.performance_repair_notes(performance)
        try:
            workflow.generate_scene_audio(
                section_dir,
                force_chunk_ids={chunk_id},
                repair_notes=repair_notes,
                only_chunk_ids={chunk_id},
            )
        except SystemExit as exc:
            message = str(exc)
            if "Repair prompt cannot fit Seed Audio prompt limit" not in message:
                raise
            # Frozen-audio workflow: if the repair addendum makes the prompt too
            # long, do not replan or remove spoken coverage. Regenerate the same
            # original chunk request without the addendum.
            workflow.generate_scene_audio(
                section_dir,
                force_chunk_ids={chunk_id},
                only_chunk_ids={chunk_id},
            )
        technical = technical_gate(section_dir, chunk_id)
        performance = review_once(section_dir, chunk_id, performance_mode, phase=phase)
        review_attempts.append(snapshot_review_attempt(section_dir, chunk_id, "provider_repair", performance, technical))

    local_tail_repair = None
    if performance.get("gate_status") == "fail" and is_tail_finish_only_failure(performance, chunk_id):
        local_tail_repair = workflow.repair_delivery_tail(part_path(section_dir, chunk_id))
        if local_tail_repair.get("status") == "applied":
            technical = technical_gate(section_dir, chunk_id)
            performance = review_once(section_dir, chunk_id, performance_mode, phase=phase)
            review_attempts.append(snapshot_review_attempt(section_dir, chunk_id, "local_tail_finish", performance, technical))

    minor_boundary_warning = is_nonblocking_minor_boundary(performance, chunk_id)
    accepted = technical["status"] == "pass" and (
        performance.get("gate_status") in {"pass", "skipped"} or minor_boundary_warning
    )
    failure_status = delivery_failure_status(performance, chunk_id)
    payload = {
        "status": "accepted" if accepted else failure_status,
        "chunk_id": chunk_id,
        "technical": technical,
        "performance": performance,
        "review_attempts": review_attempts,
        "local_silence_trim": local_silence_trim,
        "local_tail_repair": local_tail_repair,
        "nonblocking_warning": "minor_score_or_ambience_tail_boundary" if minor_boundary_warning else None,
        "attempt_policy": (
            "initial generation; deterministic trim for proven silent padding; at most one provider chunk rerender; "
            "a score/ambience-only boundary may receive one local finish; frozen script, voices, prompts, and chunk boundaries"
        ),
    }
    timeline_path = section_dir / "qa_timeline.json"
    timeline = read_json(timeline_path) if timeline_path.exists() else {"chunks": {}}
    timeline.setdefault("chunks", {})[chunk_id] = {
        "technical": technical,
        "objective_audio": performance.get("objective_audio_by_chunk", {}).get(chunk_id, {}),
        "review": next((item for item in performance.get("chunks", []) if item.get("chunk_id") == chunk_id), {}),
        "status": payload["status"],
    }
    write_json(timeline_path, timeline)
    report_path = section_dir / "logs" / "chunk_delivery" / f"{chunk_id}.json"
    write_json(report_path, payload)
    return (0 if accepted else 3), payload


def recover_local_tail(
    section_dir: Path,
    chunk_id: str,
    performance_mode: str,
    phase: str = "pilot",
) -> tuple[int, dict]:
    """Apply the one unused deterministic tail finish to a retained score-only failure."""
    report_path = section_dir / "logs" / "chunk_delivery" / f"{chunk_id}.json"
    if not report_path.exists():
        raise SystemExit(f"No retained delivery report exists for {chunk_id}.")
    previous = read_json(report_path)
    if previous.get("status") != "repair_audio":
        raise SystemExit("Local tail recovery requires a retained repair_audio delivery report.")
    if previous.get("local_tail_repair"):
        raise SystemExit("The one permitted local tail recovery has already been attempted.")
    if not is_tail_finish_only_failure(previous.get("performance", {}), chunk_id):
        raise SystemExit("Retained QA does not prove a score/ambience-only tail defect.")
    audio = part_path(section_dir, chunk_id)
    if not audio.exists():
        raise SystemExit(f"Retained audio is missing for {chunk_id}.")

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    archive_dir = section_dir / "07_audio_revisions" / chunk_id / f"{stamp}-local-tail-recovery"
    archive_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(audio, archive_dir / audio.name)
    shutil.copy2(report_path, archive_dir / report_path.name)
    raw_response = section_dir / "logs" / f"performance_{chunk_id}_response.txt"
    if raw_response.exists():
        shutil.copy2(raw_response, archive_dir / raw_response.name)

    local_tail_repair = workflow.repair_delivery_tail(audio)
    if local_tail_repair.get("status") != "applied":
        payload = {**previous, "local_tail_repair": local_tail_repair, "recovery_archive": str(archive_dir)}
        write_json(report_path, payload)
        return 3, payload
    technical = technical_gate(section_dir, chunk_id)
    performance = review_once(section_dir, chunk_id, performance_mode, phase=phase)
    review_attempt = snapshot_review_attempt(section_dir, chunk_id, "explicit_local_tail_recovery", performance, technical)
    accepted = technical.get("status") == "pass" and performance.get("gate_status") in {"pass", "skipped"}
    payload = {
        "status": "accepted" if accepted else "repair_audio",
        "chunk_id": chunk_id,
        "technical": technical,
        "performance": performance,
        "review_attempts": [*(previous.get("review_attempts") or []), review_attempt],
        "local_tail_repair": local_tail_repair,
        "recovery_archive": str(archive_dir),
        "attempt_policy": previous.get("attempt_policy"),
    }
    write_json(report_path, payload)
    return (0 if accepted else 3), payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and review one prepared Seed Audio chunk.")
    parser.add_argument("--section-dir", required=True)
    parser.add_argument("--story-config", required=True)
    parser.add_argument("--chunk-id", required=True)
    parser.add_argument("--performance-mode", choices=["off", "diagnostic", "balanced", "required"], default="balanced")
    parser.add_argument("--phase", choices=["pilot", "batch"], default="batch")
    parser.add_argument("--force", action="store_true", help="Archive any existing chunk audio and generate a new render.")
    parser.add_argument(
        "--recover-local-tail",
        action="store_true",
        help="Use the retained QA report to apply one deterministic score/ambience-only tail finish.",
    )
    args = parser.parse_args()
    section_dir = Path(args.section_dir).expanduser().resolve()
    configure(section_dir, Path(args.story_config).expanduser().resolve())
    if args.recover_local_tail:
        code, payload = recover_local_tail(section_dir, args.chunk_id, args.performance_mode, phase=args.phase)
    else:
        code, payload = generate_chunk(
            section_dir,
            args.chunk_id,
            args.performance_mode,
            force=args.force,
            phase=args.phase,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
