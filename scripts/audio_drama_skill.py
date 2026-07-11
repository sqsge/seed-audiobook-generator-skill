#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import chapter_audio_workflow as chapter
import resumable_audio_drama as shared
import workflow_input_gate


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "outputs" / "skill_runs"
AUDIOBOOK_WORKFLOW = Path(__file__).resolve().parent / "audiobook_workflow.py"
CHUNK_WORKER = Path(__file__).resolve().parent / "chunk_worker.py"
ACTIVE_STATE: tuple[Path, dict] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def state_file(run_dir: Path) -> Path:
    return run_dir / "run_state.json"


def load_state(run_dir: Path) -> dict:
    return shared.read_json(state_file(run_dir))


def save_state(run_dir: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    shared.atomic_json(state_file(run_dir), state)


def append_event(run_dir: Path, event: str, **details: object) -> None:
    shared.append_event(run_dir, event, **details)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lease_path(run_dir: Path) -> Path:
    return run_dir / "run_lease.json"


def assert_no_active_lease(run_dir: Path) -> None:
    lease = shared.read_json(lease_path(run_dir))
    if lease and pid_alive(int(lease.get("pid", 0))):
        raise SystemExit(f"Run is active under pid {lease['pid']}; wait or stop it before changing approval or plan state.")


@contextmanager
def run_lease(run_dir: Path, state: dict):
    path = lease_path(run_dir)
    existing = shared.read_json(path)
    if existing and pid_alive(int(existing.get("pid", 0))):
        raise SystemExit(f"Run already active under pid {existing['pid']}")
    lease = {"pid": os.getpid(), "started_at": now_iso(), "heartbeat_at": now_iso()}
    shared.atomic_json(path, lease)
    state["lease"] = lease
    save_state(run_dir, state)
    global ACTIVE_STATE
    ACTIVE_STATE = (run_dir, state)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def stop_handler(signum, _frame):
        state["status"] = "interrupted"
        state["current_stage"] = "interrupted"
        state["last_error"] = f"Received signal {signum}; resume from retained stage artifacts."
        save_state(run_dir, state)
        append_event(run_dir, "run_interrupted", signal=signum)
        raise KeyboardInterrupt

    for sig in previous:
        signal.signal(sig, stop_handler)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        ACTIVE_STATE = None
        if path.exists() and shared.read_json(path).get("pid") == os.getpid():
            path.unlink()


def heartbeat(run_dir: Path, state: dict, stage: str, current_item: str | None = None) -> None:
    state["current_stage"] = stage
    state["current_item"] = current_item
    lease = state.setdefault("lease", {})
    lease["pid"] = os.getpid()
    lease["heartbeat_at"] = now_iso()
    shared.atomic_json(lease_path(run_dir), lease)
    save_state(run_dir, state)


def initialize(args: argparse.Namespace) -> tuple[Path, dict]:
    run_dir = RUN_ROOT / args.run_id
    source = Path(args.source_file).expanduser().resolve()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
    if state_file(run_dir).exists():
        state = load_state(run_dir)
        if state.get("schema_version") != 2:
            raise SystemExit("This run uses the retired workflow schema; start a new run id with the final Skill runner.")
        if source_hash != state.get("source_sha256"):
            raise SystemExit(f"Run id {args.run_id} belongs to a different source snapshot.")
        return run_dir, state
    if not source.exists():
        raise SystemExit(f"Source file does not exist: {source}")
    run_dir.mkdir(parents=True, exist_ok=True)
    clean, sections, preprocessing = chapter.build_chapter_plan(source, args.target_chars, args.max_chars)
    if args.voice_registry:
        registry_payload = shared.read_json(Path(args.voice_registry).expanduser().resolve())
        roles = shared.validate_voice_registry(registry_payload.get("roles", registry_payload))
    else:
        roles = shared.infer_voice_registry(clean, args.source_title or source.stem, run_dir / "planning")
    (run_dir / "source.txt").write_text(clean, encoding="utf-8")
    shared.atomic_json(run_dir / "preprocessing_report.json", preprocessing)
    shared.atomic_json(run_dir / "voice_registry.json", {"roles": roles, "speaker_reuse_warnings": chapter.speaker_reuse_warnings(roles)})
    section_states = []
    for index, item in enumerate(sections, start=1):
        section_id = f"section_{index:03d}"
        input_dir = run_dir / "inputs" / section_id
        input_dir.mkdir(parents=True, exist_ok=True)
        text = item["_chapter_text"].strip() + "\n"
        (input_dir / "source.txt").write_text(text, encoding="utf-8")
        shared.atomic_json(
            input_dir / "story_config.json",
            shared.story_config(
                f"{args.run_id}_{section_id}",
                f"{args.source_title or source.stem} - section {index:03d}",
                text,
                roles,
            ),
        )
        section_states.append(
            {
                "section_id": section_id,
                "status": "planned",
                "source_chars": len(text),
                "story_config": f"inputs/{section_id}/story_config.json",
                "work_dir": f"sections/{section_id}",
                "chunks": [],
                "final_audio": None,
                "last_error": None,
            }
        )
    state = {
        "schema_version": 2,
        "run_id": args.run_id,
        "status": "planned",
        "created_at": now_iso(),
        "source_file": str(source),
        "source_sha256": source_hash,
        "source_title": args.source_title or source.stem,
        "profile": {"performance_mode": args.performance_mode, "asr_mode": "off"},
        "sections": section_states,
        "pilot": {
            "required": True,
            "selected_chunk_keys": [],
            "approved": False,
            "approved_at": None,
            "auto_replan_limit_per_section": 1,
            "auto_replan_counts": {},
        },
        "final_audio": None,
        "current_stage": "planned",
        "current_item": None,
    }
    save_state(run_dir, state)
    append_event(run_dir, "run_initialized", section_count=len(section_states), schema_version=2)
    return run_dir, state


def run_subprocess(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


def prepare_section(run_dir: Path, state: dict, section: dict) -> bool:
    section_id = section["section_id"]
    section_dir = run_dir / section["work_dir"]
    story_config = run_dir / section["story_config"]
    heartbeat(run_dir, state, "prepare_section", section_id)
    if (section_dir / "manifest.json").exists():
        command = [sys.executable, str(AUDIOBOOK_WORKFLOW), "--story-config", str(story_config), "--resume-partial-run-id", section_id, "--output-root", str(run_dir / "sections")]
    elif section_dir.exists():
        command = [sys.executable, str(AUDIOBOOK_WORKFLOW), "--story-config", str(story_config), "--resume-partial-run-id", section_id, "--output-root", str(run_dir / "sections")]
    else:
        command = [sys.executable, str(AUDIOBOOK_WORKFLOW), "--story-config", str(story_config), "--run-id", section_id, "--output-root", str(run_dir / "sections")]
    result = run_subprocess(command, ROOT)
    logs = run_dir / "runner_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{section_id}.prepare.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (logs / f"{section_id}.prepare.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        error = result.stderr[-2000:] or f"prepare_exit_code={result.returncode}"
        planning_patterns = (
            "multiple active roles using the same speaker id",
            "has more than 3 active roles",
            "missing explicit Voice continuity mapping",
            "quoted dialogue for roles outside active_roles",
            "prompt too long",
        )
        if any(pattern in error for pattern in planning_patterns):
            chunk_match = re.search(r"(chunk_\d+)", error)
            chunk_id = chunk_match.group(1) if chunk_match else "planning_failure"
            feedback = {
                "stage": "planner_validation",
                "summary": error.strip(),
                "instruction": "Change chunk boundaries or active-role assignment; do not retry the same invalid plan.",
            }
            section["status"] = "needs_replan"
            section["last_error"] = error
            section["replan_feedback"] = feedback
            section["chunks"] = [{
                "chunk_id": chunk_id,
                "chunk_key": f"{section_id}/{chunk_id}",
                "status": "needs_replan",
                "attempts": 0,
                "audio": None,
                "last_error": error,
                "replan_feedback": feedback,
            }]
            state["status"] = "needs_replan"
            append_event(run_dir, "planner_validation_failed", section_id=section_id, feedback=feedback)
        else:
            section["status"] = "failed"
            section["last_error"] = error
            state["status"] = "blocked"
        save_state(run_dir, state)
        return False
    gate = workflow_input_gate.evaluate(section_dir)
    shared.atomic_json(section_dir / "logs" / "pre_generation_input_gate.json", gate)
    if gate["status"] != "pass":
        section["status"] = "needs_replan"
        section["last_error"] = f"Pre-generation input gate failed: {gate['failed_chunk_ids'] or gate['section_failures']}"
        section["replan_feedback"] = {
            "stage": "static_input_gate",
            "summary": section["last_error"],
            "failed_chunks": [item for item in gate.get("chunks", []) if item.get("status") == "fail"],
            "instruction": (
                "Revise chunk boundaries and the audible timeline before any Audio call. A character must not open a chunk "
                "without nearby narrative setup. Preserve the sound-only establishing beat, at least two timeline SFX events, "
                "an in-timeline score reaction, and a sound-only coda after the last spoken line."
            ),
        }
        section["chunks"] = [
            {
                "chunk_id": chunk_id,
                "chunk_key": f"{section_id}/{chunk_id}",
                "status": "needs_replan",
                "attempts": 0,
                "audio": None,
                "last_error": section["last_error"],
                "replan_feedback": section["replan_feedback"],
            }
            for chunk_id in gate.get("failed_chunk_ids", [])
        ]
        state["status"] = "needs_replan"
        save_state(run_dir, state)
        append_event(run_dir, "input_gate_failed", section_id=section_id, report=gate)
        return False
    section["chunks"] = []
    for path in sorted((section_dir / "06_generation_requests").glob("chunk_*.json")):
        request = shared.read_json(path)
        section["chunks"].append(
            {
                "chunk_id": request["chunk_id"],
                "chunk_key": f"{section_id}/{request['chunk_id']}",
                "status": "input_validated",
                "input_metrics": request.get("input_metrics", {}),
                "attempts": 0,
                "audio": None,
                "last_error": None,
            }
        )
    section["status"] = "prepared"
    section["last_error"] = None
    save_state(run_dir, state)
    append_event(run_dir, "section_prepared", section_id=section_id, chunk_count=len(section["chunks"]))
    return True


def prepare_all(run_dir: Path, state: dict) -> bool:
    for section in state["sections"]:
        if section["status"] in {"prepared", "accepted"} and section.get("chunks"):
            continue
        if not prepare_section(run_dir, state, section):
            return False
    return True


def all_chunks(state: dict) -> list[tuple[dict, dict]]:
    return [(section, chunk) for section in state["sections"] for chunk in section.get("chunks", [])]


def select_pilots(state: dict) -> list[str]:
    pairs = all_chunks(state)
    if not pairs:
        return []
    selectors = [
        lambda item: (
            item[1]["input_metrics"].get("narrator_unit_count", 0) * 3
            - item[1]["input_metrics"].get("dialogue_unit_count", 0) * 5
        ),
        lambda item: item[1]["input_metrics"].get("dialogue_unit_count", 0),
        lambda item: (
            item[1]["input_metrics"].get("source_sfx_cue_count", 0)
            + item[1]["input_metrics"].get("dialogue_unit_count", 0) * 2
        ),
    ]
    selected: list[str] = []
    for score in selectors:
        ranked = sorted(pairs, key=score, reverse=True)
        for _, chunk in ranked:
            if chunk["chunk_key"] not in selected:
                selected.append(chunk["chunk_key"])
                break
    return selected


def find_chunk(state: dict, chunk_key: str) -> tuple[dict, dict]:
    for section, chunk in all_chunks(state):
        if chunk["chunk_key"] == chunk_key:
            return section, chunk
    raise KeyError(chunk_key)


def generate_chunk(run_dir: Path, state: dict, section: dict, chunk: dict) -> bool:
    chunk_key = chunk["chunk_key"]
    heartbeat(run_dir, state, "generate_chunk", chunk_key)
    section_dir = run_dir / section["work_dir"]
    command = [
        sys.executable,
        str(CHUNK_WORKER),
        "--section-dir", str(section_dir),
        "--story-config", str(run_dir / section["story_config"]),
        "--chunk-id", chunk["chunk_id"],
        "--performance-mode", state["profile"].get("performance_mode", "balanced"),
    ]
    chunk["status"] = "generating"
    chunk["attempts"] += 1
    save_state(run_dir, state)
    result = run_subprocess(command, ROOT)
    logs = run_dir / "runner_logs" / "chunks"
    logs.mkdir(parents=True, exist_ok=True)
    safe_name = chunk_key.replace("/", "__")
    (logs / f"{safe_name}.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (logs / f"{safe_name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    report = shared.read_json(section_dir / "logs" / "chunk_delivery" / f"{chunk['chunk_id']}.json")
    if result.returncode == 0 and report.get("status") == "accepted":
        chunk["status"] = "accepted"
        chunk["audio"] = report.get("technical", {}).get("path")
        chunk["last_error"] = None
        save_state(run_dir, state)
        append_event(run_dir, "chunk_accepted", chunk_key=chunk_key)
        return True
    if report.get("status") == "needs_replan":
        chunk["status"] = "needs_replan"
        chunk["last_error"] = report.get("performance", {}).get("chunks", [{}])[0].get("summary") or "chunk delivery failed"
        chunk["replan_feedback"] = {
            "failed_chunk_id": chunk["chunk_id"],
            "summary": chunk["last_error"],
            "performance": report.get("performance", {}).get("chunks", [{}])[0],
            "technical": report.get("technical", {}),
        }
        section["status"] = "needs_replan"
        state["status"] = "needs_replan"
        save_state(run_dir, state)
        append_event(run_dir, "chunk_needs_replan", chunk_key=chunk_key, report=report)
        return False
    chunk["status"] = "input_validated"
    chunk["last_error"] = result.stderr[-1000:] or report.get("reason") or "provider_or_worker_failure"
    state["status"] = "blocked"
    state["last_error"] = chunk["last_error"]
    save_state(run_dir, state)
    append_event(run_dir, "chunk_worker_blocked", chunk_key=chunk_key, report=report, returncode=result.returncode)
    return False


def stitch_ready_sections(run_dir: Path, state: dict) -> None:
    for section in state["sections"]:
        chunks = section.get("chunks", [])
        if not chunks or not all(chunk["status"] == "accepted" for chunk in chunks):
            continue
        if section.get("status") == "accepted" and section.get("final_audio"):
            continue
        paths = [Path(chunk["audio"]) for chunk in chunks]
        output = chapter.stitch_chapter(run_dir / section["work_dir"], paths, f"{state['run_id']}_{section['section_id']}_full.wav")
        section["final_audio"] = str(output)
        section["status"] = "accepted"
        append_event(run_dir, "section_accepted", section_id=section["section_id"], final_audio=str(output))
    save_state(run_dir, state)


def run_pilots(run_dir: Path, state: dict) -> bool:
    while True:
        pilot = state["pilot"]
        if not pilot["selected_chunk_keys"]:
            pilot["selected_chunk_keys"] = select_pilots(state)
            save_state(run_dir, state)
        restart_after_replan = False
        for key in pilot["selected_chunk_keys"]:
            section, chunk = find_chunk(state, key)
            if chunk["status"] == "accepted":
                continue
            if generate_chunk(run_dir, state, section, chunk):
                continue
            if chunk.get("status") == "needs_replan" and auto_replan_pilot_section(run_dir, state, section, chunk):
                if not prepare_section(run_dir, state, section):
                    return False
                restart_after_replan = True
                break
            return False
        if restart_after_replan:
            continue
        stitch_ready_sections(run_dir, state)
        state["status"] = "awaiting_pilot_approval"
        state["current_stage"] = "awaiting_pilot_approval"
        state["current_item"] = None
        save_state(run_dir, state)
        append_event(run_dir, "pilot_ready", selected=pilot["selected_chunk_keys"])
        return True


def run_batch(run_dir: Path, state: dict) -> bool:
    for section, chunk in all_chunks(state):
        if chunk["status"] == "accepted":
            continue
        if chunk["status"] == "needs_replan":
            state["status"] = "needs_replan"
            save_state(run_dir, state)
            return False
        if not generate_chunk(run_dir, state, section, chunk):
            return False
        stitch_ready_sections(run_dir, state)
    stitch_ready_sections(run_dir, state)
    section_paths = [Path(section["final_audio"]) for section in state["sections"]]
    final = chapter.stitch_chapter(run_dir, section_paths, f"{state['run_id']}_full.wav")
    state["final_audio"] = str(final)
    state["status"] = "completed"
    state["current_stage"] = "completed"
    state["current_item"] = None
    save_state(run_dir, state)
    append_event(run_dir, "run_completed", final_audio=str(final))
    return True


def reconcile(run_dir: Path, state: dict) -> dict:
    lease = shared.read_json(lease_path(run_dir))
    lease_alive = pid_alive(int(lease.get("pid", 0)))
    changed = False
    if state.get("status") == "running" and not lease_alive:
        state["status"] = "interrupted"
        state["current_stage"] = "interrupted"
        state["last_error"] = "Reconciled stale running state after process exit."
        append_event(run_dir, "stale_state_reconciled")
        changed = True
    if not lease_alive and state.get("status") in {"interrupted", "blocked"}:
        recovered = []
        for section in state.get("sections", []):
            for chunk in section.get("chunks", []):
                if chunk.get("status") != "generating":
                    continue
                chunk["status"] = "input_validated"
                chunk["last_error"] = "Generation was interrupted; retry from the saved request."
                recovered.append(chunk.get("chunk_key"))
            for chunk in section.get("chunks", []):
                if chunk.get("status") != "input_validated":
                    continue
                delivery_path = run_dir / section.get("work_dir", "") / "logs" / "chunk_delivery" / f"{chunk.get('chunk_id')}.json"
                delivery = shared.read_json(delivery_path)
                audio = delivery.get("technical", {}).get("path") if delivery.get("status") == "accepted" else None
                if not audio or not Path(audio).exists():
                    continue
                chunk["status"] = "accepted"
                chunk["audio"] = audio
                chunk["last_error"] = None
                append_event(run_dir, "accepted_chunk_recovered", chunk_key=chunk.get("chunk_key"))
                changed = True
        if recovered:
            append_event(run_dir, "orphan_generating_chunks_recovered", chunk_keys=recovered)
            changed = True
        for section in state.get("sections", []):
            if section.get("status") != "planned":
                continue
            section_dir = run_dir / section.get("work_dir", "")
            request_paths = sorted((section_dir / "06_generation_requests").glob("chunk_*.json"))
            if not request_paths:
                continue
            gate = workflow_input_gate.evaluate(section_dir)
            if gate.get("status") != "pass":
                continue
            section["chunks"] = [
                {
                    "chunk_id": request["chunk_id"],
                    "chunk_key": f"{section['section_id']}/{request['chunk_id']}",
                    "status": "input_validated",
                    "input_metrics": request.get("input_metrics", {}),
                    "attempts": 0,
                    "audio": None,
                    "last_error": None,
                }
                for request in (shared.read_json(path) for path in request_paths)
            ]
            section["status"] = "prepared"
            section["last_error"] = None
            append_event(run_dir, "prepared_section_recovered", section_id=section["section_id"])
            changed = True
    if changed:
        save_state(run_dir, state)
    return state


def replan_section(
    run_dir: Path,
    state: dict,
    section_id: str,
    feedback: dict | None = None,
    automatic: bool = False,
) -> None:
    section = next((item for item in state["sections"] if item["section_id"] == section_id), None)
    if not section:
        raise SystemExit(f"Unknown section id: {section_id}")
    section_dir = run_dir / section["work_dir"]
    if section_dir.exists():
        archive = run_dir / "history" / "replan" / f"{time.strftime('%Y%m%d-%H%M%S')}_{time.time_ns() % 1_000_000:06d}_{section_id}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(section_dir), str(archive))
        append_event(run_dir, "section_archived_for_replan", section_id=section_id, archive=str(archive.relative_to(run_dir)))
    story_config_path = run_dir / section["story_config"] if section.get("story_config") else None
    story_config = shared.read_json(story_config_path) if story_config_path else {}
    if feedback and story_config_path:
        story_config["replan_feedback"] = feedback
        shared.atomic_json(story_config_path, story_config)
    section["status"] = "planned"
    section["chunks"] = []
    section["final_audio"] = None
    section["last_error"] = None
    pilot = state.setdefault("pilot", {})
    was_approved = bool(pilot.get("approved"))
    approved_at = pilot.get("approved_at")
    selected_chunk_keys = list(pilot.get("selected_chunk_keys", []))
    counts = pilot.setdefault("auto_replan_counts", {})
    if automatic:
        counts[section_id] = int(counts.get(section_id, 0)) + 1
    state["pilot"] = {
        "required": True,
        "selected_chunk_keys": selected_chunk_keys if was_approved else [],
        "approved": was_approved,
        "approved_at": approved_at if was_approved else None,
        "auto_replan_limit_per_section": int(pilot.get("auto_replan_limit_per_section", 1)),
        "auto_replan_counts": counts,
    }
    state["status"] = "interrupted"
    state["current_stage"] = "replan_requested"
    state["current_item"] = section_id
    save_state(run_dir, state)
    append_event(run_dir, "section_replan_requested", section_id=section_id, automatic=automatic, feedback=feedback)


def auto_replan_pilot_section(run_dir: Path, state: dict, section: dict, chunk: dict) -> bool:
    pilot = state.setdefault("pilot", {})
    limit = int(pilot.get("auto_replan_limit_per_section", 1))
    counts = pilot.setdefault("auto_replan_counts", {})
    section_id = section["section_id"]
    if int(counts.get(section_id, 0)) >= limit:
        append_event(run_dir, "pilot_auto_replan_exhausted", section_id=section_id, chunk_key=chunk.get("chunk_key"))
        return False
    feedback = {
        "stage": "pilot",
        "instruction": (
            "Replan this section instead of appending another repair sentence. Change dramatic boundaries, spoken density, "
            "and the ending timeline as needed. Preserve source order, fixed voices, key dialogue, and story meaning. "
            "Leave enough room after the final spoken phrase for its complete phoneme and a brief natural ambience tail."
        ),
        "failure": chunk.get("replan_feedback") or {
            "failed_chunk_id": chunk.get("chunk_id"),
            "summary": chunk.get("last_error"),
        },
    }
    replan_section(run_dir, state, section_id, feedback=feedback, automatic=True)
    state["status"] = "running"
    state["current_stage"] = "auto_replan_pilot"
    state["current_item"] = section_id
    save_state(run_dir, state)
    append_event(run_dir, "pilot_auto_replan_started", section_id=section_id, feedback=feedback)
    return True


def resume_pending_pilot_replan(run_dir: Path, state: dict) -> bool:
    if state.get("pilot", {}).get("approved"):
        return False
    for section in state.get("sections", []):
        if section.get("status") != "needs_replan":
            continue
        chunk = next((item for item in section.get("chunks", []) if item.get("status") == "needs_replan"), None)
        if chunk and auto_replan_pilot_section(run_dir, state, section, chunk):
            return True
    return False


def retry_cached_static_gate_once(run_dir: Path, state: dict) -> bool:
    attempted = False
    for section in state.get("sections", []):
        feedback = section.get("replan_feedback") or {}
        if section.get("status") != "needs_replan" or feedback.get("stage") != "static_input_gate":
            continue
        if section.get("cached_static_gate_retry_used"):
            continue
        section["cached_static_gate_retry_used"] = True
        section["status"] = "planned"
        section["chunks"] = []
        save_state(run_dir, state)
        append_event(run_dir, "cached_static_gate_retry_started", section_id=section["section_id"])
        attempted = True
        if not prepare_section(run_dir, state, section):
            return False
    return attempted


def print_status(run_dir: Path, state: dict) -> None:
    chunks = [chunk for _, chunk in all_chunks(state)]
    pilot_keys = state.get("pilot", {}).get("selected_chunk_keys", [])
    pilot_audio = [
        {"chunk_key": key, "audio": find_chunk(state, key)[1].get("audio")}
        for key in pilot_keys
        if any(chunk.get("chunk_key") == key for chunk in chunks)
    ]
    summary = {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "current_stage": state.get("current_stage"),
        "current_item": state.get("current_item"),
        "sections": {status: sum(item["status"] == status for item in state.get("sections", [])) for status in sorted({item["status"] for item in state.get("sections", [])})},
        "chunks": {status: sum(item["status"] == status for item in chunks) for status in sorted({item["status"] for item in chunks})},
        "pilot": state.get("pilot"),
        "pilot_audio": pilot_audio,
        "final_audio": state.get("final_audio"),
        "state_file": str(state_file(run_dir)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Final resumable long-form Seed Audio drama Skill.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--source-file", required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--source-title")
    run_parser.add_argument("--voice-registry")
    run_parser.add_argument("--target-chars", type=int, default=2400)
    run_parser.add_argument("--max-chars", type=int, default=2800)
    run_parser.add_argument("--performance-mode", choices=["off", "diagnostic", "balanced", "required"], default="balanced")
    run_parser.add_argument("--prepare-only", action="store_true")
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("--run-id", required=True)
    approve_parser = sub.add_parser("approve-pilot")
    approve_parser.add_argument("--run-id", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-id", required=True)
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--run-id", required=True)
    replan_parser = sub.add_parser("replan")
    replan_parser.add_argument("--run-id", required=True)
    replan_parser.add_argument("--section-id", required=True)
    args = parser.parse_args()

    if args.command == "run":
        run_dir, state = initialize(args)
        with run_lease(run_dir, state):
            state["status"] = "running"
            save_state(run_dir, state)
            if not prepare_all(run_dir, state):
                print_status(run_dir, state)
                return 3
            if args.prepare_only:
                state["status"] = "prepared"
                state["current_stage"] = "prepared"
                save_state(run_dir, state)
            else:
                run_pilots(run_dir, state)
        print_status(run_dir, state)
        return 0 if state["status"] in {"prepared", "awaiting_pilot_approval"} else 3

    run_dir = RUN_ROOT / args.run_id
    if not state_file(run_dir).exists():
        raise SystemExit(f"Unknown run id: {args.run_id}")
    state = load_state(run_dir)
    if args.command == "status":
        print_status(run_dir, state)
        return 0
    if args.command == "reconcile":
        print_status(run_dir, reconcile(run_dir, state))
        return 0
    if args.command == "replan":
        assert_no_active_lease(run_dir)
        replan_section(run_dir, state, args.section_id)
        print_status(run_dir, state)
        return 0
    if args.command == "approve-pilot":
        assert_no_active_lease(run_dir)
        if state.get("status") != "awaiting_pilot_approval":
            raise SystemExit("Pilot can be approved only after all selected pilot chunks are accepted.")
        state["pilot"]["approved"] = True
        state["pilot"]["approved_at"] = now_iso()
        state["status"] = "pilot_approved"
        save_state(run_dir, state)
        append_event(run_dir, "pilot_approved")
        print_status(run_dir, state)
        return 0
    if state.get("schema_version") != 2:
        raise SystemExit("This run uses the retired workflow schema; start a new final-workflow run id.")
    with run_lease(run_dir, state):
        if any(section.get("status") == "needs_replan" for section in state["sections"]):
            retry_cached_static_gate_once(run_dir, state)
        if any(section.get("status") == "needs_replan" for section in state["sections"]):
            if not resume_pending_pilot_replan(run_dir, state):
                raise SystemExit("A section is marked needs_replan and its automatic pilot replan is exhausted; run the explicit replan command before resuming providers.")
        state["status"] = "running"
        save_state(run_dir, state)
        if not all(section.get("chunks") and section.get("status") in {"prepared", "accepted"} for section in state["sections"]):
            if not prepare_all(run_dir, state):
                print_status(run_dir, state)
                return 3
        if not state.get("pilot", {}).get("approved"):
            if state.get("status") == "awaiting_pilot_approval":
                raise SystemExit("Pilot audio requires human approval before batch generation.")
            run_pilots(run_dir, state)
        else:
            run_batch(run_dir, state)
    print_status(run_dir, state)
    return 0 if state["status"] in {"completed", "awaiting_pilot_approval"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
