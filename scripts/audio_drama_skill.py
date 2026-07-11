#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
        "pilot": {"required": True, "selected_chunk_keys": [], "approved": False, "approved_at": None},
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
        section["status"] = "failed"
        section["last_error"] = result.stderr[-2000:] or f"prepare_exit_code={result.returncode}"
        state["status"] = "blocked"
        save_state(run_dir, state)
        return False
    gate = workflow_input_gate.evaluate(section_dir)
    shared.atomic_json(section_dir / "logs" / "pre_generation_input_gate.json", gate)
    if gate["status"] != "pass":
        section["status"] = "needs_replan"
        section["last_error"] = f"Pre-generation input gate failed: {gate['failed_chunk_ids'] or gate['section_failures']}"
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
        lambda item: item[1]["input_metrics"].get("narrator_unit_count", 0),
        lambda item: item[1]["input_metrics"].get("dialogue_unit_count", 0),
        lambda item: item[1]["input_metrics"].get("source_sfx_cue_count", 0),
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
    pilot = state["pilot"]
    if not pilot["selected_chunk_keys"]:
        pilot["selected_chunk_keys"] = select_pilots(state)
        save_state(run_dir, state)
    for key in pilot["selected_chunk_keys"]:
        section, chunk = find_chunk(state, key)
        if chunk["status"] == "accepted":
            continue
        if not generate_chunk(run_dir, state, section, chunk):
            return False
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
    if state.get("status") == "running" and not pid_alive(int(lease.get("pid", 0))):
        state["status"] = "interrupted"
        state["current_stage"] = "interrupted"
        state["last_error"] = "Reconciled stale running state after process exit."
        save_state(run_dir, state)
        append_event(run_dir, "stale_state_reconciled")
    return state


def replan_section(run_dir: Path, state: dict, section_id: str) -> None:
    section = next((item for item in state["sections"] if item["section_id"] == section_id), None)
    if not section:
        raise SystemExit(f"Unknown section id: {section_id}")
    section_dir = run_dir / section["work_dir"]
    if section_dir.exists():
        archive = run_dir / "history" / "replan" / f"{time.strftime('%Y%m%d-%H%M%S')}_{section_id}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(section_dir), str(archive))
        append_event(run_dir, "section_archived_for_replan", section_id=section_id, archive=str(archive.relative_to(run_dir)))
    section["status"] = "planned"
    section["chunks"] = []
    section["final_audio"] = None
    section["last_error"] = None
    state["pilot"] = {"required": True, "selected_chunk_keys": [], "approved": False, "approved_at": None}
    state["status"] = "interrupted"
    state["current_stage"] = "replan_requested"
    state["current_item"] = section_id
    save_state(run_dir, state)
    append_event(run_dir, "section_replan_requested", section_id=section_id)


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
    if any(section.get("status") == "needs_replan" for section in state["sections"]):
        raise SystemExit("A section is marked needs_replan; run the explicit replan command before resuming providers.")
    with run_lease(run_dir, state):
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
