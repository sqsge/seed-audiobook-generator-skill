#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import chapter_audio_workflow as chapter
import llm_chat


ROOT = Path(__file__).resolve().parents[1]
AUDIOBOOK_WORKFLOW = Path(__file__).resolve().parent / "audiobook_workflow.py"
RUN_ROOT = ROOT / "outputs" / "skill_runs"
VOICE_CANDIDATES = [
    "en_male_knightley_uranus_bigtts",
    "en_male_josh_uranus_bigtts",
    "en_male_hades_uranus_bigtts",
    "en_male_ronald_uranus_bigtts",
    "en_female_rachel_p1_uranus_bigtts",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def state_path(run_dir: Path) -> Path:
    return run_dir / "run_state.json"


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Voice planner returned no JSON object.")
    return json.loads(text[start : end + 1])


def infer_voice_registry(source_text: str, title: str, planning_dir: Path | None = None) -> dict:
    prompt = f"""Identify the stable speaking-role registry for this complete English fiction chapter.
Return JSON only. Include Narrator and each character who must speak in an audio-drama adaptation.
Use only these speaker ids: {json.dumps(VOICE_CANDIDATES)}. Reuse an id only for roles that should never speak in the same short scene.
Each role requires key, label, reference_mode='speaker', default_speaker, description, attribution_keywords, and reference_prompt.
Keep role names concise and stable across the whole chapter.

Title: {title}
Source:
{source_text}

Schema: {{"roles": {{"Narrator": {{"key":"narrator","label":"Narrator","reference_mode":"speaker","default_speaker":"...","description":"...","attribution_keywords":[],"reference_prompt":"..."}}}}}}
"""
    if planning_dir:
        planning_dir.mkdir(parents=True, exist_ok=True)
        (planning_dir / "voice_registry_request.txt").write_text(prompt, encoding="utf-8")
    response = llm_chat.chat_text(
        prompt,
        system="Return one complete JSON object for a chapter-level audio-drama voice registry.",
        model=os.getenv("LLM_MODEL", "seed-2-0-pro-260328"),
        temperature=0.1,
        max_tokens=6000,
    )
    if planning_dir:
        (planning_dir / "voice_registry_response.txt").write_text(response, encoding="utf-8")
    payload = extract_json(response)
    return validate_voice_registry(payload.get("roles"))


def validate_voice_registry(roles: object) -> dict:
    if not isinstance(roles, dict) or not roles or "Narrator" not in roles:
        raise ValueError("Voice registry must contain a non-empty roles object and Narrator.")
    for role, spec in roles.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Voice registry role {role} must be an object.")
        if spec.get("default_speaker") not in VOICE_CANDIDATES:
            raise ValueError(f"Unsupported speaker for {role}: {spec.get('default_speaker')}")
        spec["reference_mode"] = "speaker"
    return roles


def story_config(section_id: str, title: str, source_text: str, roles: dict) -> dict:
    return {
        "scene_id": section_id,
        "production_mode": "audio_drama_adaptation",
        "prompt_language": "en",
        "auto_planning": True,
        "lock_roles": True,
        "source_title": title,
        "source_url": "local:chapter_source_section",
        "source_excerpt": source_text,
        "roles": roles,
    }


def save_state(run_dir: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    atomic_json(state_path(run_dir), state)


def append_event(run_dir: Path, event: str, **details: object) -> None:
    payload = {"at": now_iso(), "event": event, **details}
    path = run_dir / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def initialize(args: argparse.Namespace) -> tuple[Path, dict]:
    run_dir = RUN_ROOT / args.run_id
    if state_path(run_dir).exists():
        state = read_json(state_path(run_dir))
        requested_source = Path(args.source_file).expanduser().resolve()
        requested_hash = hashlib.sha256(requested_source.read_bytes()).hexdigest() if requested_source.exists() else None
        if requested_hash != state.get("source_sha256"):
            raise SystemExit(f"Run id {args.run_id} already belongs to a different source snapshot.")
        return run_dir, state
    source = Path(args.source_file).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Source file does not exist: {source}")
    run_dir.mkdir(parents=True, exist_ok=True)
    clean, sections, preprocessing = chapter.build_chapter_plan(source, args.target_chars, args.max_chars)
    if args.voice_registry:
        registry_payload = read_json(Path(args.voice_registry).expanduser().resolve())
        roles = validate_voice_registry(registry_payload.get("roles", registry_payload))
    else:
        roles = infer_voice_registry(clean, args.source_title or source.stem, run_dir / "planning")
    (run_dir / "source.txt").write_text(clean, encoding="utf-8")
    atomic_json(run_dir / "preprocessing_report.json", preprocessing)
    atomic_json(run_dir / "voice_registry.json", {"roles": roles, "speaker_reuse_warnings": chapter.speaker_reuse_warnings(roles)})

    section_states: list[dict] = []
    for index, item in enumerate(sections, start=1):
        section_id = f"section_{index:03d}"
        input_dir = run_dir / "inputs" / section_id
        source_file = input_dir / "source.txt"
        config_file = input_dir / "story_config.json"
        text = item["_chapter_text"].strip() + "\n"
        input_dir.mkdir(parents=True, exist_ok=True)
        source_file.write_text(text, encoding="utf-8")
        atomic_json(
            config_file,
            story_config(
                section_id=f"{args.run_id}_{section_id}",
                title=f"{args.source_title or source.stem} - section {index:03d}",
                source_text=text,
                roles=roles,
            ),
        )
        section_states.append(
            {
                "section_id": section_id,
                "status": "planned",
                "source_chars": len(text),
                "source_file": str(source_file.relative_to(run_dir)),
                "story_config": str(config_file.relative_to(run_dir)),
                "work_dir": f"sections/{section_id}",
                "attempts": 0,
                "review_cycles": 0,
                "final_audio": None,
                "last_error": None,
            }
        )
    state = {
        "schema_version": 1,
        "run_id": args.run_id,
        "status": "planned",
        "created_at": now_iso(),
        "source_file": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_title": args.source_title or source.stem,
        "profile": {"asr_mode": args.asr_mode, "performance_mode": args.performance_mode},
        "limits": {"max_review_cycles": args.max_review_cycles},
        "sections": section_states,
        "final_audio": None,
    }
    save_state(run_dir, state)
    append_event(run_dir, "run_initialized", section_count=len(section_states))
    return run_dir, state


def preserve_interrupted_rewrite(run_dir: Path, section_dir: Path, section_id: str) -> None:
    if not section_dir.exists() or (section_dir / "manifest.json").exists():
        return
    history = run_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    target = history / f"{section_id}.rewrite_interrupted_{time.strftime('%Y%m%d-%H%M%S')}"
    section_dir.rename(target)


def preserve_review_checkpoint(run_dir: Path, section_dir: Path, section_id: str, attempt: int) -> None:
    if not section_dir.exists():
        return
    target = run_dir / "history" / section_id / f"attempt_{attempt:03d}_{time.strftime('%Y%m%d-%H%M%S')}"
    copied = False
    for relative in (
        Path("manifest.json"),
        Path("logs/audio_quality_report.json"),
        Path("logs/asr_language_report.json"),
        Path("logs/performance_review_report.json"),
        Path("logs/generation_log.json"),
    ):
        source = section_dir / relative
        if source.exists():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied = True
    if copied:
        append_event(run_dir, "review_checkpoint_preserved", section_id=section_id, path=str(target.relative_to(run_dir)))


def section_result(section_dir: Path) -> dict:
    validation = read_json(section_dir / "logs" / "validation_log.json")
    quality = read_json(section_dir / "logs" / "audio_quality_report.json")
    asr = read_json(section_dir / "logs" / "asr_language_report.json")
    performance = read_json(section_dir / "logs" / "performance_review_report.json")
    audios = sorted((section_dir / "08_stitched").glob("*.wav"))
    media_ok = bool(validation) and all(item.get("ffmpeg_decode_ok") for item in validation.values())
    accepted = (
        media_ok
        and quality.get("core_status") == "pass"
        and asr.get("gate_status") in {"pass", "skipped"}
        and performance.get("gate_status") in {"pass", "skipped"}
        and bool(audios)
    )
    return {
        "accepted": accepted,
        "media_validation_status": "pass" if media_ok else "fail",
        "quality_core_status": quality.get("core_status", "missing"),
        "quality_diagnostic_status": quality.get("status", "missing"),
        "asr_gate_status": asr.get("gate_status", "missing"),
        "performance_gate_status": performance.get("gate_status", "missing"),
        "performance_failed_chunks": performance.get("gate_failed_chunk_ids", []),
        "final_audio": str(audios[0]) if audios else None,
    }


def run_sections(
    run_dir: Path,
    state: dict,
    *,
    generate: bool,
    allow_extra_review_cycle: bool = False,
    rebuild_director_prompts: bool = False,
) -> int:
    state["status"] = "running"
    state["current_stage"] = "section_processing"
    save_state(run_dir, state)
    profile = state.get("profile", {})
    limits = state.get("limits", {})
    for section in state["sections"]:
        if section.get("status") == "accepted" or (not generate and section.get("status") == "prepared"):
            continue
        section_id = section["section_id"]
        section_dir = run_dir / section["work_dir"]
        if section.get("status") == "needs_review":
            if (
                int(section.get("review_cycles", 0)) >= int(limits.get("max_review_cycles", 2))
                and not allow_extra_review_cycle
            ):
                section["last_error"] = "Automatic review-cycle limit reached; inspect retained audio and QA before another generation."
                state["status"] = "blocked"
                state["current_section"] = section_id
                state["current_stage"] = "human_review_required"
                save_state(run_dir, state)
                append_event(run_dir, "review_cycle_limit_reached", section_id=section_id)
                return 4
            if allow_extra_review_cycle:
                append_event(run_dir, "extra_review_cycle_authorized", section_id=section_id)
            preserve_review_checkpoint(run_dir, section_dir, section_id, int(section.get("attempts", 0)))
            if rebuild_director_prompts:
                state["current_section"] = section_id
                state["current_stage"] = "rebuild_director_prompts"
                save_state(run_dir, state)
                rebuild_cmd = [
                    sys.executable, str(AUDIOBOOK_WORKFLOW),
                    "--story-config", str(run_dir / section["story_config"]),
                    "--resume-partial-run-id", section_id,
                    "--output-root", str(run_dir / "sections"),
                ]
                rebuild = subprocess.run(rebuild_cmd, cwd=ROOT, text=True, capture_output=True)
                logs = run_dir / "runner_logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / f"{section_id}.rebuild.stdout.txt").write_text(rebuild.stdout, encoding="utf-8")
                (logs / f"{section_id}.rebuild.stderr.txt").write_text(rebuild.stderr, encoding="utf-8")
                if rebuild.returncode != 0:
                    section["status"] = "failed"
                    section["last_error"] = rebuild.stderr[-2000:] or f"rebuild_exit_code={rebuild.returncode}"
                    state["status"] = "blocked"
                    save_state(run_dir, state)
                    return rebuild.returncode
                append_event(run_dir, "director_prompts_rebuilt", section_id=section_id)
        section["status"] = "running"
        section["attempts"] = int(section.get("attempts", 0)) + 1
        section["last_error"] = None
        state["current_section"] = section_id
        state["current_stage"] = "generate_and_review" if generate else "director_rewrite"
        save_state(run_dir, state)
        append_event(run_dir, "section_started", section_id=section_id, attempt=section["attempts"], generate=generate)

        if (section_dir / "manifest.json").exists():
            cmd = [
                sys.executable, str(AUDIOBOOK_WORKFLOW),
                "--story-config", str(run_dir / section["story_config"]),
                "--resume-run-id", section_id,
                "--output-root", str(run_dir / "sections"),
            ]
        elif section_dir.exists():
            cmd = [
                sys.executable, str(AUDIOBOOK_WORKFLOW),
                "--story-config", str(run_dir / section["story_config"]),
                "--resume-partial-run-id", section_id,
                "--output-root", str(run_dir / "sections"),
            ]
        else:
            cmd = [
                sys.executable, str(AUDIOBOOK_WORKFLOW),
                "--story-config", str(run_dir / section["story_config"]),
                "--run-id", section_id,
                "--output-root", str(run_dir / "sections"),
            ]
        if generate:
            cmd.extend(
                [
                    "--generate",
                    "--asr-mode", profile.get("asr_mode", "off"),
                    "--performance-mode", profile.get("performance_mode", "balanced"),
                ]
            )
        try:
            result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
        except KeyboardInterrupt:
            section["status"] = "interrupted"
            section["last_error"] = "Interrupted by user; resume will reuse completed artifacts."
            state["status"] = "interrupted"
            state["current_stage"] = "interrupted"
            save_state(run_dir, state)
            append_event(run_dir, "section_interrupted", section_id=section_id)
            raise
        logs = run_dir / "runner_logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"{section_id}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (logs / f"{section_id}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            section["status"] = "failed"
            section["last_error"] = result.stderr[-2000:] or f"exit_code={result.returncode}"
            state["status"] = "blocked"
            save_state(run_dir, state)
            append_event(run_dir, "section_failed", section_id=section_id, returncode=result.returncode)
            return result.returncode
        if not generate:
            section["status"] = "prepared"
            save_state(run_dir, state)
            append_event(run_dir, "section_prepared", section_id=section_id)
            continue
        report = section_result(section_dir)
        section["qa"] = report
        section["final_audio"] = report["final_audio"]
        section["status"] = "accepted" if report["accepted"] else "needs_review"
        if not report["accepted"]:
            section["review_cycles"] = int(section.get("review_cycles", 0)) + 1
        save_state(run_dir, state)
        append_event(run_dir, "section_reviewed", section_id=section_id, accepted=report["accepted"], qa=report)
        if not report["accepted"]:
            state["status"] = "needs_review"
            save_state(run_dir, state)
            return 3

    if generate:
        state["current_section"] = None
        state["current_stage"] = "chapter_stitch"
        save_state(run_dir, state)
        audio_paths = [Path(item["final_audio"]) for item in state["sections"]]
        final = chapter.stitch_chapter(run_dir, audio_paths, f"{state['run_id']}_full.wav")
        state["final_audio"] = str(final)
        state["status"] = "completed"
    else:
        state["status"] = "prepared"
    state["current_section"] = None
    state["current_stage"] = "completed" if generate else "prepared"
    save_state(run_dir, state)
    append_event(run_dir, state["status"], final_audio=state.get("final_audio"))
    return 0


def print_status(run_dir: Path, state: dict) -> None:
    summary = {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "profile": state.get("profile"),
        "current_section": state.get("current_section"),
        "current_stage": state.get("current_stage"),
        "sections": [
            {
                "section_id": item.get("section_id"),
                "status": item.get("status"),
                "attempts": item.get("attempts"),
                "final_audio": item.get("final_audio"),
                "last_error": item.get("last_error"),
            }
            for item in state.get("sections", [])
        ],
        "final_audio": state.get("final_audio"),
        "state_file": str(state_path(run_dir)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumable Seed Audio drama Skill runner.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Initialize if needed, then process remaining sections.")
    run_parser.add_argument("--source-file", required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--source-title")
    run_parser.add_argument("--voice-registry", help="Optional fixed chapter-level voice registry JSON.")
    run_parser.add_argument("--target-chars", type=int, default=2400)
    run_parser.add_argument("--max-chars", type=int, default=2800)
    run_parser.add_argument("--asr-mode", choices=["off", "diagnostic", "required"], default="off")
    run_parser.add_argument("--performance-mode", choices=["off", "diagnostic", "balanced", "required"], default="balanced")
    run_parser.add_argument("--max-review-cycles", type=int, default=2)
    run_parser.add_argument("--prepare-only", action="store_true")
    resume_parser = sub.add_parser("resume", help="Continue an existing run from its state file.")
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--prepare-only", action="store_true")
    resume_parser.add_argument("--allow-extra-review-cycle", action="store_true")
    resume_parser.add_argument("--rebuild-director-prompts", action="store_true")
    status_parser = sub.add_parser("status", help="Print current state without calling providers.")
    status_parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    run_dir = RUN_ROOT / args.run_id
    if args.command == "run":
        run_dir, state = initialize(args)
        code = run_sections(run_dir, state, generate=not args.prepare_only)
        print_status(run_dir, read_json(state_path(run_dir)))
        return code
    if not state_path(run_dir).exists():
        raise SystemExit(f"Unknown run id: {args.run_id}")
    state = read_json(state_path(run_dir))
    if args.command == "resume":
        code = run_sections(
            run_dir,
            state,
            generate=not args.prepare_only,
            allow_extra_review_cycle=args.allow_extra_review_cycle,
            rebuild_director_prompts=args.rebuild_director_prompts,
        )
        print_status(run_dir, read_json(state_path(run_dir)))
        return code
    print_status(run_dir, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
