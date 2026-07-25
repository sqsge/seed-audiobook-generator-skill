#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
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
import audiobook_workflow as workflow
import resumable_audio_drama as shared
import workflow_input_gate


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "outputs" / "skill_runs"
AUDIOBOOK_WORKFLOW = Path(__file__).resolve().parent / "audiobook_workflow.py"
SEED_AUDIO_CLIENT = Path(__file__).resolve().parent / "seed_audio_client.py"
CHUNK_WORKER = Path(__file__).resolve().parent / "chunk_worker.py"
ACTIVE_STATE: tuple[Path, dict] | None = None
CACHED_STATIC_GATE_RETRY_VERSION = "final_gate_sync_v4"
DEFAULT_MAX_STRUCTURAL_REPLANS = max(1, int(os.getenv("SEED_AUDIO_MAX_STRUCTURAL_REPLANS", "3")))
DEFAULT_MAX_STAGNANT_REPLANS = max(1, int(os.getenv("SEED_AUDIO_MAX_STAGNANT_REPLANS", "2")))
DEFAULT_AUDIO_REPAIR_RETRIES = max(0, int(os.getenv("SEED_AUDIO_REPAIR_RETRIES", "3")))
CONTINUE_ON_REPAIR = os.getenv("SEED_AUDIO_CONTINUE_ON_REPAIR", "false").lower() in {"1", "true", "yes", "on"}


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


def stable_fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def failure_signature(feedback: dict | None) -> str:
    feedback = feedback or {}
    failure = feedback.get("failure") if isinstance(feedback.get("failure"), dict) else feedback
    issues = failure.get("performance", {}).get("issues", []) if isinstance(failure, dict) else []
    objective = failure.get("performance", {}).get("objective_audio", {}) if isinstance(failure, dict) else {}
    stage = feedback.get("stage") or (failure.get("stage") if isinstance(failure, dict) else None)
    signature = {
        "stage": stage,
        "failed_chunk_id": failure.get("failed_chunk_id") if isinstance(failure, dict) else None,
        "summary": re.sub(r"\s+", " ", str(failure.get("summary", "")).lower()).strip() if isinstance(failure, dict) else "",
        "issue_types": sorted(
            str(issue.get("type")) for issue in issues if isinstance(issue, dict) and issue.get("type")
        ),
        "objective_hard_reasons": sorted(str(item) for item in objective.get("hard_reasons", [])),
    }
    return stable_fingerprint(signature)


def failure_convergence_metrics(feedback: dict | None) -> dict:
    feedback = feedback or {}
    failure = feedback.get("failure") if isinstance(feedback.get("failure"), dict) else feedback
    failure = failure if isinstance(failure, dict) else {}
    performance = failure.get("performance", {}) if isinstance(failure.get("performance"), dict) else {}
    issues = performance.get("issues", []) if isinstance(performance.get("issues"), list) else []
    objective = performance.get("objective_audio", {}) if isinstance(performance.get("objective_audio"), dict) else {}
    summary = re.sub(r"\s+", " ", str(failure.get("summary", "")).lower()).strip()
    categories = []
    for category, markers in {
        "clipped_speech": ("clipped", "mid-sentence", "incomplete speech", "truncated"),
        "missing_sound_layer": ("missing music", "missing ambience", "missing sfx", "sound layer"),
        "silence": ("silence", "silent", "dead air"),
        "attribution": ("speaker", "attribution", "wrong voice"),
        "coverage": ("missing dialogue", "source coverage", "omitted"),
    }.items():
        if any(marker in summary for marker in markers):
            categories.append(category)
    issue_types = sorted(str(issue.get("type")) for issue in issues if isinstance(issue, dict) and issue.get("type"))
    hard_reasons = sorted(str(item) for item in objective.get("hard_reasons", []))
    major_count = sum(issue.get("severity") == "major" for issue in issues if isinstance(issue, dict))
    minor_count = sum(issue.get("severity") == "minor" for issue in issues if isinstance(issue, dict))
    internal_max = max(
        (float(item.get("duration_sec") or 0) for item in objective.get("internal_silences", []) if isinstance(item, dict)),
        default=0.0,
    )
    signal_seconds = max(
        float(objective.get("leading_silence_sec") or 0),
        float(objective.get("trailing_silence_sec") or 0),
        internal_max,
    )
    score = major_count * 10.0 + minor_count * 2.0 + len(hard_reasons) * 5.0 + signal_seconds
    family = stable_fingerprint(
        {
            "stage": feedback.get("stage") or failure.get("stage"),
            "categories": categories or ([summary] if summary else []),
            "issue_types": issue_types,
            "objective_hard_reasons": hard_reasons,
        }
    )
    return {"family": family, "score": round(score, 3), "categories": categories, "issue_types": issue_types}


def section_plan_fingerprint(section_dir: Path, first_source_unit_id: str | None = None) -> str | None:
    requests = []
    include = first_source_unit_id is None
    for path in sorted((section_dir / "06_generation_requests").glob("chunk_*.json")):
        request = shared.read_json(path)
        if not include and first_source_unit_id in request.get("source_unit_ids", []):
            include = True
        if not include:
            continue
        prompt = re.sub(r"\[Chunk\s+\d+/\d+\]", "[Chunk]", str(request.get("text_prompt", "")), flags=re.I)
        requests.append(
            {
                "source_unit_ids": request.get("source_unit_ids", []),
                "active_roles": request.get("active_roles", []),
                "text_prompt": re.sub(r"\s+", " ", prompt).strip(),
                "expected_duration_sec": request.get("expected_duration_sec", {}),
                "render_plan_contract": request.get("render_plan_contract", {}),
            }
        )
    return stable_fingerprint(requests) if requests else None


def replan_policy(state: dict) -> dict:
    policy = state.setdefault("replan_policy", {})
    legacy_limit = state.get("pilot", {}).get("auto_replan_limit_per_section")
    policy.setdefault(
        "max_structural_rounds_per_section",
        int(legacy_limit) if legacy_limit is not None else DEFAULT_MAX_STRUCTURAL_REPLANS,
    )
    policy.setdefault("max_stagnant_rounds", DEFAULT_MAX_STAGNANT_REPLANS)
    return policy


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
            "auto_replan_limit_per_section": max(
                1, int(getattr(args, "max_structural_replans", DEFAULT_MAX_STRUCTURAL_REPLANS))
            ),
            "auto_replan_counts": {},
        },
        "replan_policy": {
            "max_structural_rounds_per_section": max(
                1, int(getattr(args, "max_structural_replans", DEFAULT_MAX_STRUCTURAL_REPLANS))
            ),
            "max_stagnant_rounds": max(
                1, int(getattr(args, "max_stagnant_replans", DEFAULT_MAX_STAGNANT_REPLANS))
            ),
        },
        "casting": {
            "required": True,
            "approved": False,
            "approved_at": None,
            "samples": {},
        },
        "final_audio": None,
        "current_stage": "planned",
        "current_item": None,
    }
    save_state(run_dir, state)
    append_event(run_dir, "run_initialized", section_count=len(section_states), schema_version=2)
    return run_dir, state


def run_subprocess(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, env=env)


def casting_registry_sha256(run_dir: Path) -> str:
    return hashlib.sha256((run_dir / "voice_registry.json").read_bytes()).hexdigest()


def casting_is_approved(run_dir: Path, state: dict) -> bool:
    casting = state.get("casting", {})
    return bool(casting.get("approved")) and casting.get("approved_registry_sha256") == casting_registry_sha256(run_dir)


def update_casting_registry(run_dir: Path, state: dict, registry_path: Path) -> list[str]:
    """Replace a run's casting registry without losing the prior casting evidence."""
    assert_no_active_lease(run_dir)
    payload = shared.read_json(registry_path.expanduser().resolve())
    roles = shared.validate_voice_registry(payload.get("roles", payload))
    old_registry = shared.read_json(run_dir / "voice_registry.json")
    old_roles = old_registry.get("roles", {})
    changed_roles = sorted(
        role
        for role in set(old_roles) | set(roles)
        if old_roles.get(role) != roles.get(role)
    )
    if not changed_roles:
        return []

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S%z")
    history_dir = run_dir / "history" / "casting_registry" / stamp
    history_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(run_dir / "voice_registry.json", history_dir / "voice_registry.json")

    samples = state.setdefault("casting", {}).setdefault("samples", {})
    for role in changed_roles:
        old_spec = old_roles.get(role, {})
        key = str(old_spec.get("key") or re.sub(r"[^a-z0-9]+", "_", role.lower()).strip("_"))
        for source in (
            run_dir / "casting_samples" / f"{key}.wav",
            run_dir / "casting_samples" / f"{key}.wav.meta.json",
            run_dir / "planning" / "casting_prompts" / f"{key}.txt",
        ):
            if source.exists():
                relative = source.relative_to(run_dir)
                target = history_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if role not in roles:
            samples.pop(role, None)

    new_registry = {"roles": roles, "speaker_reuse_warnings": chapter.speaker_reuse_warnings(roles)}
    shared.atomic_json(run_dir / "voice_registry.json", new_registry)
    for story_config_path in sorted((run_dir / "inputs").glob("section_*/story_config.json")):
        story_config = shared.read_json(story_config_path)
        story_config["roles"] = roles
        shared.atomic_json(story_config_path, story_config)

    casting = state.setdefault("casting", {})
    casting["approved"] = False
    casting["approved_at"] = None
    casting.pop("approved_registry_sha256", None)
    casting.pop("registry_sha256", None)
    state["status"] = "casting_registry_updated"
    state["current_stage"] = "casting_registry_updated"
    state["current_item"] = None
    save_state(run_dir, state)
    append_event(
        run_dir,
        "casting_registry_updated",
        changed_roles=changed_roles,
        history_path=str(history_dir.relative_to(run_dir)),
    )
    return changed_roles


def import_casting_samples(run_dir: Path, state: dict, source_run_dir: Path) -> list[str]:
    """Reuse locally generated samples from a run with the exact same registry."""
    assert_no_active_lease(run_dir)
    source_run_dir = source_run_dir.expanduser().resolve()
    assert_no_active_lease(source_run_dir)
    target_registry = shared.read_json(run_dir / "voice_registry.json")
    source_registry = shared.read_json(source_run_dir / "voice_registry.json")
    if target_registry.get("roles") != source_registry.get("roles"):
        raise SystemExit("Casting samples can be imported only when source and target role registries are identical.")

    roles = target_registry.get("roles", {})
    target_casting = state.setdefault("casting", {}).setdefault("samples", {})
    source_state = load_state(source_run_dir)
    source_samples = source_state.get("casting", {}).get("samples", {})
    imported_roles = []
    for role, spec in roles.items():
        if target_casting.get(role, {}).get("speaker") == spec.get("default_speaker"):
            continue
        source_sample = source_samples.get(role, {})
        if source_sample.get("speaker") != spec.get("default_speaker"):
            raise SystemExit(f"Source run has no matching casting sample for {role}.")
        key = str(spec.get("key") or re.sub(r"[^a-z0-9]+", "_", role.lower()).strip("_"))
        source_wav = source_run_dir / "casting_samples" / f"{key}.wav"
        source_meta = source_run_dir / "casting_samples" / f"{key}.wav.meta.json"
        if not source_wav.exists() or not source_meta.exists():
            raise SystemExit(f"Source casting artifacts are incomplete for {role}.")
        shutil.copy2(source_wav, run_dir / "casting_samples" / source_wav.name)
        shutil.copy2(source_meta, run_dir / "casting_samples" / source_meta.name)
        target_casting[role] = {
            "audio": f"casting_samples/{key}.wav",
            "speaker": spec.get("default_speaker"),
            "imported_from_run": source_state.get("run_id"),
            "sha256": hashlib.sha256(source_wav.read_bytes()).hexdigest(),
        }
        imported_roles.append(role)

    if not imported_roles:
        return []
    target_casting_state = state.setdefault("casting", {})
    target_casting_state["approved"] = False
    target_casting_state["approved_at"] = None
    target_casting_state["registry_sha256"] = casting_registry_sha256(run_dir)
    target_casting_state.pop("approved_registry_sha256", None)
    state["status"] = "awaiting_casting_approval"
    state["current_stage"] = "awaiting_casting_approval"
    state["current_item"] = None
    save_state(run_dir, state)
    append_event(
        run_dir,
        "casting_samples_imported",
        imported_roles=imported_roles,
        source_run_id=source_state.get("run_id"),
    )
    return imported_roles


def import_frozen_plans(
    run_dir: Path,
    state: dict,
    source_run_dir: Path,
    role_aliases_path: Path,
) -> None:
    """Import only immutable planning artifacts; never carry source audio or QA forward."""
    assert_no_active_lease(run_dir)
    source_run_dir = source_run_dir.expanduser().resolve()
    assert_no_active_lease(source_run_dir)
    source_state = load_state(source_run_dir)
    if source_state.get("source_sha256") != state.get("source_sha256"):
        raise SystemExit("Frozen-plan import requires exactly the same source text hash.")
    if state.get("casting", {}).get("required") and not casting_is_approved(run_dir, state):
        raise SystemExit("Approve the target casting before importing frozen plans.")

    aliases = shared.read_json(role_aliases_path)
    target_roles = shared.read_json(run_dir / "voice_registry.json").get("roles", {})
    source_registry = shared.read_json(source_run_dir / "voice_registry.json")
    for source_role, target_role in aliases.items():
        if source_role not in source_registry.get("roles", {}):
            raise SystemExit(f"Source role missing from role aliases: {source_role}")
        if target_role not in target_roles:
            raise SystemExit(f"Target role missing from role aliases: {target_role}")

    def rewrite_role_value(value: object) -> object:
        if isinstance(value, str):
            rewritten = aliases.get(value, value)
            for source_role, target_role in aliases.items():
                rewritten = rewritten.replace(source_role, target_role)
            return rewritten
        if isinstance(value, list):
            return [rewrite_role_value(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite_role_value(item) for key, item in value.items()}
        return value

    archived = run_dir / "superseded_planner_attempts" / datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    for source_section in source_state.get("sections", []):
        section_id = source_section["section_id"]
        src = source_run_dir / source_section["work_dir"]
        dst = run_dir / source_section["work_dir"]
        if not src.exists():
            raise SystemExit(f"Frozen-plan source section is missing: {src}")
        if dst.exists():
            archived.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(archived / section_id))
        dst.mkdir(parents=True, exist_ok=False)
        for name in (
            "00_reference_prompts", "00_workflow_config.json", "01_source_excerpt.txt",
            "02_source_units.json", "03_scene_parse.json", "05_director_prompt_chunks",
            "06_generation_requests", "09_stage_effect_analysis.md", "manifest.json",
        ):
            item = src / name
            if item.is_dir():
                shutil.copytree(item, dst / name)
            elif item.exists():
                shutil.copy2(item, dst / name)
        prompt_dir = dst / "00_reference_prompts"
        for source_role, target_role in aliases.items():
            source_key = str(source_registry["roles"][source_role].get("key") or source_role)
            target_key = str(target_roles[target_role].get("key") or target_role)
            old_prompt = prompt_dir / f"{source_key}.txt"
            new_prompt = prompt_dir / f"{target_key}.txt"
            if old_prompt.exists() and old_prompt != new_prompt:
                old_prompt.replace(new_prompt)
        for prompt_path in (dst / "05_director_prompt_chunks").glob("chunk_*.txt"):
            prompt_path.write_text(str(rewrite_role_value(prompt_path.read_text(encoding="utf-8"))), encoding="utf-8")
        for request_path in (dst / "06_generation_requests").glob("chunk_*.json"):
            shared.atomic_json(request_path, rewrite_role_value(shared.read_json(request_path)))
        for metadata_path in (dst / "03_scene_parse.json", dst / "manifest.json"):
            if metadata_path.exists():
                shared.atomic_json(metadata_path, rewrite_role_value(shared.read_json(metadata_path)))
        shared.atomic_json(dst / "04_voice_registry.json", {"roles": target_roles, "speaker_reuse_warnings": chapter.speaker_reuse_warnings(target_roles)})

    imported_sections = copy.deepcopy(source_state.get("sections", []))
    for section in imported_sections:
        section["status"] = "prepared"
        section.pop("final_audio", None)
        section.pop("replan_feedback", None)
        section.pop("replan_history", None)
        section.pop("last_error", None)
        for chunk in section.get("chunks", []):
            chunk["status"] = "input_validated"
            chunk["attempts"] = 0
            chunk["audio"] = None
            chunk["last_error"] = None
            for key in ("replan_feedback", "qa_timeline", "local_tail_recovery", "local_tail_recovery_attempts"):
                chunk.pop(key, None)

    state["sections"] = imported_sections
    state["pilot"] = {
        "selected_chunk_keys": [],
        "approved": False,
        "approved_at": None,
        "auto_replan_counts": {},
    }
    state.pop("batch_canaries", None)
    state["final_audio"] = None
    state["status"] = "casting_approved"
    state["current_stage"] = "frozen_plans_imported"
    state["current_item"] = None
    state["last_error"] = None
    save_state(run_dir, state)
    append_event(
        run_dir,
        "frozen_plans_imported",
        source_run_id=source_state.get("run_id"),
        source_run_dir=str(source_run_dir),
        section_count=len(imported_sections),
        role_aliases=aliases,
        archived_previous_planner_artifacts=str(archived.relative_to(run_dir)),
    )


def is_nonblocking_minor_boundary(report: dict, chunk_id: str) -> bool:
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
    return "final spoken" in evidence and not any(marker in evidence for marker in ("mid-sentence", "mid sentence", "mid-word", "mid word", "clipped word", "truncated speech", "incomplete sentence"))


def accept_nonblocking_minor_boundary(run_dir: Path, state: dict, section_id: str, chunk_id: str) -> None:
    """Promote a retained complete take when only a nonblocking score-tail warning remains."""
    assert_no_active_lease(run_dir)
    section = next((item for item in state.get("sections", []) if item.get("section_id") == section_id), None)
    if not section:
        raise SystemExit(f"Unknown section: {section_id}")
    chunk = next((item for item in section.get("chunks", []) if item.get("chunk_id") == chunk_id), None)
    if not chunk or chunk.get("status") != "repair_audio":
        raise SystemExit("Only a retained repair_audio chunk can be promoted.")
    report_path = run_dir / section["work_dir"] / "logs" / "chunk_delivery" / f"{chunk_id}.json"
    report = shared.read_json(report_path)
    performance = report.get("performance", {})
    if not is_nonblocking_minor_boundary(performance, chunk_id):
        raise SystemExit("QA does not prove a nonblocking minor boundary warning.")
    if report.get("technical", {}).get("status") != "pass":
        raise SystemExit("Technical gate must pass before accepting a minor boundary warning.")
    report["status"] = "accepted"
    report["nonblocking_warning"] = "minor_score_or_ambience_tail_boundary"
    shared.atomic_json(report_path, report)
    timeline_path = run_dir / section["work_dir"] / "qa_timeline.json"
    timeline = shared.read_json(timeline_path)
    timeline.setdefault("chunks", {}).setdefault(chunk_id, {})["status"] = "accepted_with_minor_warning"
    timeline["chunks"][chunk_id]["nonblocking_warning"] = report["nonblocking_warning"]
    shared.atomic_json(timeline_path, timeline)
    chunk["status"] = "accepted"
    chunk["audio"] = report["technical"].get("path")
    chunk["last_error"] = None
    chunk["nonblocking_warning"] = report["nonblocking_warning"]
    section["status"] = "prepared"
    section["last_error"] = None
    state["status"] = "casting_approved"
    state["current_stage"] = "minor_boundary_accepted"
    state["current_item"] = chunk["chunk_key"]
    state["last_error"] = None
    save_state(run_dir, state)
    append_event(run_dir, "minor_boundary_accepted", chunk_key=chunk["chunk_key"], report=str(report_path.relative_to(run_dir)))


def prepare_casting_samples(run_dir: Path, state: dict) -> bool:
    registry = shared.read_json(run_dir / "voice_registry.json")
    roles = registry.get("roles", {})
    casting = state.setdefault("casting", {"required": True, "approved": False, "approved_at": None, "samples": {}})
    sample_dir = run_dir / "casting_samples"
    prompt_dir = run_dir / "planning" / "casting_prompts"
    sample_dir.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    casting_timeout = max(10, int(os.getenv("SEED_CASTING_TIMEOUT", "120")))
    for role, spec in roles.items():
        key = str(spec.get("key") or re.sub(r"[^a-z0-9]+", "_", role.lower()).strip("_"))
        output = sample_dir / f"{key}.wav"
        prompt_path = prompt_dir / f"{key}.txt"
        prompt_path.write_text(
            "[Single speaker only. No narrator, music, ambience, or sound effects. Clean dry voice casting sample.]\n"
            + str(spec.get("reference_prompt") or f"This is the casting sample for {role}."),
            encoding="utf-8",
        )
        prior_sample = casting.get("samples", {}).get(role, {})
        prior_speaker = prior_sample.get("speaker") if isinstance(prior_sample, dict) else None
        if not output.exists() or prior_speaker != spec.get("default_speaker"):
            command = [
                    sys.executable,
                    str(SEED_AUDIO_CLIENT),
                    "--text-file",
                    str(prompt_path),
                    "--speaker",
                    str(spec.get("default_speaker")),
                    "--out",
                    str(output),
                    "--format",
                    "wav",
                    "--sample-rate",
                    "24000",
                    "--speech-rate",
                    "2",
                    "--timeout",
                    str(casting_timeout),
                ]
            try:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=casting_timeout,
                )
            except subprocess.TimeoutExpired:
                casting["last_error"] = f"casting_sample_timeout:{role}:{casting_timeout}s"
                casting["failed_role"] = role
                state["status"] = "blocked"
                state["current_stage"] = "casting_samples"
                state["current_item"] = role
                save_state(run_dir, state)
                return False
            if result.returncode != 0:
                casting["last_error"] = result.stderr[-2000:] or f"casting_sample_failed:{role}"
                state["status"] = "blocked"
                save_state(run_dir, state)
                return False
        casting.setdefault("samples", {})[role] = {
            "audio": str(output.relative_to(run_dir)),
            "speaker": spec.get("default_speaker"),
        }
        casting.pop("last_error", None)
        casting.pop("failed_role", None)
        state["current_stage"] = "casting_samples"
        state["current_item"] = role
        save_state(run_dir, state)
    casting["registry_sha256"] = casting_registry_sha256(run_dir)
    state["status"] = "awaiting_casting_approval"
    state["current_stage"] = "awaiting_casting_approval"
    state["current_item"] = None
    save_state(run_dir, state)
    append_event(run_dir, "casting_samples_ready", roles=list(roles))
    return True


def prepare_section(run_dir: Path, state: dict, section: dict, cached_rewrite_only: bool = False) -> bool:
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
    subprocess_env = None
    if cached_rewrite_only:
        subprocess_env = os.environ.copy()
        subprocess_env["SEED_REWRITE_CACHED_ONLY"] = "1"
    result = run_subprocess(command, ROOT, env=subprocess_env)
    logs = run_dir / "runner_logs"
    shared.write_runner_log_attempt(logs, f"{section_id}.prepare", result.stdout, result.stderr)
    if result.returncode != 0:
        error = result.stderr[-2000:] or f"prepare_exit_code={result.returncode}"
        deterministic_compatibility_patterns = (
            "without verifiable adjacent source evidence",
            "missing voice binding token",
            "read-risk prompt text",
        )
        if (
            not cached_rewrite_only
            and any(pattern in error for pattern in deterministic_compatibility_patterns)
            and (section_dir / "logs" / "seed2_rewrite_response.txt").exists()
        ):
            section["cached_static_gate_retry_used"] = True
            section["cached_static_gate_retry_version"] = CACHED_STATIC_GATE_RETRY_VERSION
            section["replan_feedback"] = {
                "stage": "cached_planner_validation",
                "summary": error.strip(),
                "instruction": "Revalidate the cached response with deterministic compatibility repair only.",
            }
            save_state(run_dir, state)
            append_event(run_dir, "cached_planner_validation_retry_started", section_id=section_id)
            return prepare_section(run_dir, state, section, cached_rewrite_only=True)
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
            section["chunks"] = list(section.get("preserved_chunks", [])) + [{
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
        section["chunks"] = list(section.get("preserved_chunks", [])) + [
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
    new_chunks = []
    for path in sorted((section_dir / "06_generation_requests").glob("chunk_*.json")):
        request = shared.read_json(path)
        new_chunks.append(
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
    section["chunks"] = list(section.get("preserved_chunks", [])) + new_chunks
    pending_round = section.pop("pending_replan_round", None)
    if pending_round is not None:
        plan_fingerprint = section_plan_fingerprint(section_dir)
        history = section.setdefault("replan_history", [])
        entry = next((item for item in reversed(history) if item.get("round") == pending_round), None)
        if entry is not None:
            entry["plan_fingerprint"] = plan_fingerprint
            entry["prepared_at"] = now_iso()
            same_plan = bool(plan_fingerprint and plan_fingerprint == entry.get("parent_plan_fingerprint"))
            entry["same_plan_as_parent"] = same_plan
            entry["status"] = "rejected_no_plan_change" if same_plan else "prepared_material_change"
            if same_plan:
                no_progress = {
                    "stage": "replan_no_progress",
                    "summary": "The replanned suffix compiled to the same source mapping, roles, prompt, duration, and render contract.",
                    "instruction": "Stop before another Audio call; human review must provide a materially different structural direction.",
                    "round": pending_round,
                    "plan_fingerprint": plan_fingerprint,
                }
                section["status"] = "needs_replan"
                section["last_error"] = no_progress["summary"]
                section["replan_feedback"] = no_progress
                if new_chunks:
                    new_chunks[0]["status"] = "needs_replan"
                    new_chunks[0]["replan_feedback"] = no_progress
                state["status"] = "human_review_required"
                state["current_stage"] = "replan_no_progress"
                state["current_item"] = section_id
                save_state(run_dir, state)
                append_event(run_dir, "replan_stopped_no_plan_change", section_id=section_id, round=pending_round)
                return False
    section["status"] = "prepared"
    section["last_error"] = None
    save_state(run_dir, state)
    append_event(run_dir, "section_prepared", section_id=section_id, chunk_count=len(section["chunks"]))
    return True


def prepare_all(run_dir: Path, state: dict) -> bool:
    for section in state["sections"]:
        if section["status"] in {"prepared", "accepted", "needs_chunk_repair"} and section.get("chunks"):
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


def select_section_canaries(state: dict) -> list[str]:
    """Choose one high-complexity request per section and reuse its accepted render."""
    selected: list[str] = []
    for section in state.get("sections", []):
        all_section_chunks = section.get("chunks", [])
        chunks = [chunk for chunk in all_section_chunks if not chunk.get("preserved_from_replan")] or all_section_chunks
        if not chunks:
            continue
        candidate = max(
            chunks,
            key=lambda chunk: (
                chunk.get("input_metrics", {}).get("soundscape_event_count", 0) * 3
                + chunk.get("input_metrics", {}).get("dialogue_unit_count", 0) * 2
                + chunk.get("input_metrics", {}).get("source_sfx_cue_count", 0)
            ),
        )
        selected.append(candidate["chunk_key"])
    return selected


def find_chunk(state: dict, chunk_key: str) -> tuple[dict, dict]:
    for section, chunk in all_chunks(state):
        if chunk["chunk_key"] == chunk_key:
            return section, chunk
    raise KeyError(chunk_key)


def generate_chunk(run_dir: Path, state: dict, section: dict, chunk: dict, phase: str = "batch", force: bool = False) -> bool:
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
        "--phase", phase,
    ]
    if force:
        command.append("--force")
    chunk["status"] = "generating"
    chunk["attempts"] = int(chunk.get("attempts") or 0) + 1
    save_state(run_dir, state)
    result = run_subprocess(command, ROOT)
    logs = run_dir / "runner_logs" / "chunks"
    safe_name = chunk_key.replace("/", "__")
    shared.write_runner_log_attempt(logs, safe_name, result.stdout, result.stderr)
    report = shared.read_json(section_dir / "logs" / "chunk_delivery" / f"{chunk['chunk_id']}.json")
    if result.returncode == 0 and report.get("status") == "accepted":
        chunk["status"] = "accepted"
        chunk["audio"] = report.get("technical", {}).get("path")
        chunk["last_error"] = None
        save_state(run_dir, state)
        append_event(run_dir, "chunk_accepted", chunk_key=chunk_key)
        return True
    if report.get("status") == "repair_audio":
        chunk["status"] = "repair_audio"
        chunk["last_error"] = report.get("performance", {}).get("chunks", [{}])[0].get("summary") or "chunk delivery failed"
        chunk["repair_feedback"] = {
            "stage": "pilot" if phase == "pilot" else "batch",
            "failed_chunk_id": chunk["chunk_id"],
            "summary": chunk["last_error"],
            "performance": report.get("performance", {}).get("chunks", [{}])[0],
            "technical": report.get("technical", {}),
        }
        section["status"] = "repair_audio"
        state["status"] = "repair_audio"
        state["current_stage"] = "repair_audio_required"
        state["current_item"] = chunk_key
        save_state(run_dir, state)
        append_event(run_dir, "chunk_repair_audio_required", chunk_key=chunk_key, report=report)
        return False
    if report.get("status") == "needs_chunk_repair":
        chunk["status"] = "repair_audio"
        chunk["chunk_repair_cycles"] = int(chunk.get("chunk_repair_cycles", 0)) + 1
        chunk["last_error"] = (
            report.get("performance", {}).get("chunks", [{}])[0].get("summary")
            or ", ".join(
                report.get("performance", {}).get("objective_audio_by_chunk", {}).get(chunk["chunk_id"], {}).get("hard_reasons", [])
            )
            or "chunk render quality failed"
        )
        section["status"] = "repair_audio"
        state["status"] = "repair_audio"
        state["current_stage"] = "repair_audio_required"
        state["current_item"] = chunk_key
        save_state(run_dir, state)
        append_event(run_dir, "chunk_repair_audio_required", chunk_key=chunk_key, report=report)
        return False
    chunk["status"] = "input_validated"
    chunk["last_error"] = result.stderr[-1000:] or report.get("reason") or "provider_or_worker_failure"
    state["status"] = "blocked"
    state["last_error"] = chunk["last_error"]
    save_state(run_dir, state)
    append_event(run_dir, "chunk_worker_blocked", chunk_key=chunk_key, report=report, returncode=result.returncode)
    return False


def auto_retry_repair_audio(run_dir: Path, state: dict, section: dict, chunk: dict, phase: str = "batch") -> bool:
    """Retry the same frozen chunk audio without changing script, voices, or boundaries."""
    if chunk.get("status") != "repair_audio":
        return False
    attempts = int(chunk.get("audio_repair_retries") or 0)
    if attempts >= DEFAULT_AUDIO_REPAIR_RETRIES:
        return False
    chunk["audio_repair_retries"] = attempts + 1
    append_event(
        run_dir,
        "frozen_audio_auto_retry_started",
        chunk_key=chunk.get("chunk_key"),
        retry=chunk["audio_repair_retries"],
        max_retries=DEFAULT_AUDIO_REPAIR_RETRIES,
    )
    section["status"] = "prepared"
    state["status"] = "running"
    state["current_stage"] = "audio_repair_retry"
    state["current_item"] = chunk.get("chunk_key")
    save_state(run_dir, state)
    ok = generate_chunk(run_dir, state, section, chunk, phase=phase, force=True)
    append_event(
        run_dir,
        "frozen_audio_auto_retry_finished",
        chunk_key=chunk.get("chunk_key"),
        retry=chunk.get("audio_repair_retries"),
        accepted=ok,
        status=chunk.get("status"),
    )
    return ok


def auto_retry_repair_audio_until_exhausted(run_dir: Path, state: dict, section: dict, chunk: dict, phase: str = "batch") -> bool:
    """Run all allowed frozen-audio retries before deferring a bad chunk."""
    while chunk.get("status") == "repair_audio":
        attempts = int(chunk.get("audio_repair_retries") or 0)
        if attempts >= DEFAULT_AUDIO_REPAIR_RETRIES:
            append_event(
                run_dir,
                "frozen_audio_auto_retry_budget_exhausted",
                chunk_key=chunk.get("chunk_key"),
                retries=attempts,
                max_retries=DEFAULT_AUDIO_REPAIR_RETRIES,
            )
            return False
        if auto_retry_repair_audio(run_dir, state, section, chunk, phase=phase):
            return True
    return chunk.get("status") == "accepted"


def recover_local_tail(run_dir: Path, state: dict, section_id: str, chunk_id: str) -> bool:
    """Recover one retained score/ambience-only ending without another Audio render."""
    section = next((item for item in state.get("sections", []) if item.get("section_id") == section_id), None)
    if section is None:
        raise SystemExit(f"Unknown section id: {section_id}")
    chunk = next((item for item in section.get("chunks", []) if item.get("chunk_id") == chunk_id), None)
    if chunk is None:
        raise SystemExit(f"Unknown chunk id in {section_id}: {chunk_id}")
    chunk_key = chunk.get("chunk_key") or f"{section_id}/{chunk_id}"
    is_pilot_chunk = chunk_key in set(state.get("pilot", {}).get("selected_chunk_keys", []))
    is_batch_repair = section.get("status") == "repair_audio" and chunk.get("status") == "repair_audio"
    if is_pilot_chunk:
        if section.get("status") != "needs_replan" or chunk.get("status") != "needs_replan":
            raise SystemExit("Local tail recovery requires a retained needs_replan Pilot chunk.")
    elif not is_batch_repair:
        raise SystemExit("Local tail recovery requires a selected Pilot chunk or a retained Batch repair_audio chunk.")

    heartbeat(run_dir, state, "recover_local_tail", chunk_key)
    section_dir = run_dir / section["work_dir"]
    command = [
        sys.executable,
        str(CHUNK_WORKER),
        "--section-dir", str(section_dir),
        "--story-config", str(run_dir / section["story_config"]),
        "--chunk-id", chunk_id,
        "--performance-mode", state["profile"].get("performance_mode", "balanced"),
        "--phase", "pilot" if is_pilot_chunk else "batch",
        "--recover-local-tail",
    ]
    result = run_subprocess(command, ROOT)
    logs = run_dir / "runner_logs" / "local_tail_recovery"
    safe_name = chunk_key.replace("/", "__")
    shared.write_runner_log_attempt(logs, safe_name, result.stdout, result.stderr)
    report = shared.read_json(section_dir / "logs" / "chunk_delivery" / f"{chunk_id}.json")
    chunk["local_tail_recovery_attempts"] = int(chunk.get("local_tail_recovery_attempts", 0)) + 1
    if result.returncode == 0 and report.get("status") == "accepted":
        chunk["status"] = "accepted"
        chunk["audio"] = report.get("technical", {}).get("path")
        chunk["last_error"] = None
        chunk["local_tail_recovery"] = report.get("local_tail_repair")
        section["status"] = "prepared"
        section["last_error"] = None
        state["status"] = "prepared" if is_pilot_chunk else "pilot_approved"
        state["current_stage"] = "pilot_local_tail_recovered" if is_pilot_chunk else "batch_local_tail_recovered"
        state["current_item"] = chunk_key
        save_state(run_dir, state)
        append_event(
            run_dir,
            "pilot_local_tail_recovered" if is_pilot_chunk else "batch_local_tail_recovered",
            chunk_key=chunk_key,
            recovery=report.get("local_tail_repair"),
            review_attempts=report.get("review_attempts", []),
        )
        return True
    chunk["last_error"] = report.get("performance", {}).get("chunks", [{}])[0].get("summary") or "local tail recovery failed"
    state["status"] = "needs_replan" if is_pilot_chunk else "repair_audio"
    state["current_stage"] = "pilot_local_tail_recovery_failed" if is_pilot_chunk else "batch_local_tail_recovery_failed"
    state["current_item"] = chunk_key
    save_state(run_dir, state)
    append_event(run_dir, "pilot_local_tail_recovery_failed" if is_pilot_chunk else "batch_local_tail_recovery_failed", chunk_key=chunk_key, report=report)
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
            if generate_chunk(run_dir, state, section, chunk, phase="pilot"):
                continue
            if chunk.get("status") == "repair_audio":
                state["status"] = "human_review_required"
                state["current_stage"] = "repair_audio_exhausted"
                state["current_item"] = key
                save_state(run_dir, state)
                append_event(run_dir, "manual_review_required", chunk_key=key, reason="frozen_audio_repair_exhausted")
                return False
            if chunk.get("status") == "needs_replan":
                chunk.setdefault("replan_feedback", {})["stage"] = "pilot"
                section["replan_feedback"] = chunk["replan_feedback"]
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


def promote_exhausted_chunk_repair(run_dir: Path, state: dict, section: dict, chunk: dict, phase: str = "batch") -> bool:
    delivery_path = (
        run_dir
        / section["work_dir"]
        / "logs"
        / "chunk_delivery"
        / f"{chunk['chunk_id']}.json"
    )
    report = shared.read_json(delivery_path)
    review_attempts = report.get("review_attempts", [])
    if report.get("status") != "needs_chunk_repair" or len(review_attempts) < 2:
        return False
    performance = report.get("performance", {}).get("chunks", [{}])[0]
    summary = (
        performance.get("summary")
        or chunk.get("last_error")
        or "Initial render and one provider repair both failed quality review."
    )
    chunk["status"] = "needs_replan"
    chunk["last_error"] = summary
    chunk["replan_feedback"] = {
        "stage": phase,
        "failed_chunk_id": chunk["chunk_id"],
        "summary": summary,
        "performance": performance,
        "technical": report.get("technical", {}),
        "exhausted_chunk_repair_attempts": list(review_attempts),
    }
    section["status"] = "needs_replan"
    section["replan_feedback"] = chunk["replan_feedback"]
    state["status"] = "needs_replan"
    state["current_stage"] = "chunk_repair_promoted_to_replan"
    state["current_item"] = chunk["chunk_key"]
    save_state(run_dir, state)
    append_event(
        run_dir,
        "chunk_repair_promoted_to_replan",
        chunk_key=chunk["chunk_key"],
        review_attempts=review_attempts,
    )
    return True


def run_batch(run_dir: Path, state: dict) -> bool:
    canaries = state.setdefault("batch_canaries", {})
    if CONTINUE_ON_REPAIR:
        canaries["selected_chunk_keys"] = []
        canaries["status"] = "accepted"
    if not CONTINUE_ON_REPAIR and not canaries.get("selected_chunk_keys"):
        canaries["selected_chunk_keys"] = select_section_canaries(state)
        canaries["status"] = "running"
        save_state(run_dir, state)
    if canaries.get("status") != "accepted":
        for key in canaries["selected_chunk_keys"]:
            section, chunk = find_chunk(state, key)
            if chunk["status"] == "accepted":
                continue
            if chunk["status"] == "needs_replan":
                state["status"] = "needs_replan"
                save_state(run_dir, state)
                return False
            if chunk["status"] == "repair_audio":
                if auto_retry_repair_audio_until_exhausted(run_dir, state, section, chunk, phase="batch"):
                    continue
                if chunk.get("status") != "repair_audio":
                    continue
                state["status"] = "human_review_required"
                state["current_stage"] = "repair_audio_exhausted"
                state["current_item"] = key
                save_state(run_dir, state)
                append_event(run_dir, "manual_review_required", chunk_key=key, reason="frozen_audio_repair_exhausted")
                return False
            if not generate_chunk(run_dir, state, section, chunk, phase="batch"):
                return False
        canaries["status"] = "accepted"
        append_event(run_dir, "batch_canaries_accepted", selected=canaries["selected_chunk_keys"])
    for section, chunk in all_chunks(state):
        if chunk["status"] == "accepted":
            continue
        if chunk["status"] == "needs_replan":
            state["status"] = "needs_replan"
            save_state(run_dir, state)
            return False
        if chunk["status"] == "repair_audio":
            if auto_retry_repair_audio_until_exhausted(run_dir, state, section, chunk, phase="batch"):
                stitch_ready_sections(run_dir, state)
                continue
            if chunk.get("status") != "repair_audio":
                continue
            if CONTINUE_ON_REPAIR:
                append_event(run_dir, "frozen_audio_repair_deferred", chunk_key=chunk["chunk_key"])
                continue
            state["status"] = "human_review_required"
            state["current_stage"] = "repair_audio_exhausted"
            state["current_item"] = chunk["chunk_key"]
            save_state(run_dir, state)
            append_event(run_dir, "manual_review_required", chunk_key=chunk["chunk_key"], reason="frozen_audio_repair_exhausted")
            return False
        if not generate_chunk(run_dir, state, section, chunk):
            if chunk.get("status") == "repair_audio" and auto_retry_repair_audio_until_exhausted(run_dir, state, section, chunk, phase="batch"):
                stitch_ready_sections(run_dir, state)
                continue
            if chunk.get("status") == "repair_audio" and CONTINUE_ON_REPAIR:
                append_event(run_dir, "frozen_audio_repair_deferred", chunk_key=chunk["chunk_key"])
                continue
            return False
        stitch_ready_sections(run_dir, state)
    stitch_ready_sections(run_dir, state)
    if CONTINUE_ON_REPAIR:
        repair_chunks = [
            chunk.get("chunk_key")
            for _section, chunk in all_chunks(state)
            if chunk.get("status") == "repair_audio"
        ]
        if repair_chunks:
            state["status"] = "repair_audio"
            state["current_stage"] = "batch_first_pass_completed_with_repairs"
            state["current_item"] = repair_chunks[0]
            save_state(run_dir, state)
            append_event(run_dir, "batch_first_pass_completed_with_repairs", repair_chunks=repair_chunks)
            return False
    section_paths = [Path(section["final_audio"]) for section in state["sections"]]
    final = chapter.stitch_chapter(run_dir, section_paths, f"{state['run_id']}_full.wav")
    state["final_audio"] = str(final)
    section_audits = {
        section["section_id"]: workflow.adaptive_audio_signal_report(Path(section["final_audio"]))
        for section in state["sections"]
    }
    final_audit = workflow.adaptive_audio_signal_report(final)
    audit = {
        "version": "chapter_final_audit_v2",
        "status": "fail" if final_audit.get("hard_reasons") or any(
            item.get("hard_reasons") for item in section_audits.values()
        ) else "pass",
        "sections": section_audits,
        "final_audio": final_audit,
    }
    shared.atomic_json(run_dir / "logs" / "final_chapter_audit.json", audit)
    if audit["status"] != "pass":
        state["status"] = "human_review_required"
        state["current_stage"] = "chapter_final_audit_failed"
        state["current_item"] = str(final)
        save_state(run_dir, state)
        append_event(run_dir, "chapter_final_audit_failed", audit=audit)
        return False
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
            section["chunks"] = list(section.get("preserved_chunks", [])) + [
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
    full_section: bool = False,
) -> None:
    section = next((item for item in state["sections"] if item["section_id"] == section_id), None)
    if not section:
        raise SystemExit(f"Unknown section id: {section_id}")
    section_dir = run_dir / section["work_dir"]
    story_config_path = run_dir / section["story_config"] if section.get("story_config") else None
    story_config = shared.read_json(story_config_path) if story_config_path else {}
    failed_index = next(
        (index for index, chunk in enumerate(section.get("chunks", [])) if chunk.get("status") == "needs_replan"),
        None,
    )
    failed_chunk = section.get("chunks", [])[failed_index] if failed_index is not None else None
    if feedback is None:
        failure = (failed_chunk or {}).get("replan_feedback") or section.get("replan_feedback")
        if failure:
            feedback = {
                "stage": "localized_replan",
                "instruction": (
                    "Replan only the failed suffix. Reduce spoken density and end every chunk after a complete phrase. "
                    "Preserve source order, fixed voices, key dialogue, and a brief natural sound tail."
                ),
                "failure": failure,
            }

    requests: dict[str, dict] = {}
    source_units: list[dict] = []
    if section_dir.exists():
        requests = {
            path.stem: shared.read_json(path)
            for path in sorted((section_dir / "06_generation_requests").glob("chunk_*.json"))
        }
        source_payload = shared.read_json(section_dir / "02_source_units.json")
        if isinstance(source_payload, list):
            source_units = source_payload

    canonical_units = story_config.get("original_source_units")
    if not isinstance(canonical_units, list) or not canonical_units:
        prior_first_id = (section.get("replan_scope") or {}).get("first_failed_source_unit_id")
        if prior_first_id:
            for candidate_path in sorted((run_dir / "history" / "replan").glob(f"*_{section_id}/02_source_units.json")):
                candidate = shared.read_json(candidate_path)
                candidate_ids = [unit.get("source_unit_id") for unit in candidate] if isinstance(candidate, list) else []
                if prior_first_id in candidate_ids:
                    canonical_units = candidate
                    break
    if isinstance(canonical_units, list) and canonical_units:
        source_units = canonical_units
    elif source_units:
        story_config["original_source_units"] = source_units

    prefix_chunks = [] if full_section or failed_index is None else section.get("chunks", [])[:failed_index]
    prefix_chunks = [chunk for chunk in prefix_chunks if chunk.get("status") == "accepted" and chunk.get("audio")]
    failed_request = requests.get(str((failed_chunk or {}).get("chunk_id")), {})
    failed_unit_ids = failed_request.get("source_unit_ids", [])
    first_failed_unit = failed_unit_ids[0] if failed_unit_ids else None
    source_ids = [unit.get("source_unit_id") for unit in source_units]
    prior_first_failed_unit = (section.get("replan_scope") or {}).get("first_failed_source_unit_id")
    if first_failed_unit not in source_ids and prior_first_failed_unit in source_ids:
        first_failed_unit = prior_first_failed_unit
    suffix_units: list[dict] = []
    if first_failed_unit:
        if first_failed_unit in source_ids:
            suffix_units = source_units[source_ids.index(first_failed_unit):]
    parent_plan_fingerprint = (
        section_plan_fingerprint(section_dir, None if full_section else first_failed_unit)
        if section_dir.exists() else None
    )
    if prefix_chunks and not suffix_units:
        prefix_chunks = []

    archive: Path | None = None
    if section_dir.exists():
        archive = run_dir / "history" / "replan" / f"{time.strftime('%Y%m%d-%H%M%S')}_{time.time_ns() % 1_000_000:06d}_{section_id}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(section_dir), str(archive))
        append_event(run_dir, "section_archived_for_replan", section_id=section_id, archive=str(archive.relative_to(run_dir)))
    if story_config_path:
        if "original_source_excerpt" not in story_config:
            story_config["original_source_excerpt"] = story_config.get("source_excerpt", "")
        if "original_source_units" not in story_config and source_units:
            story_config["original_source_units"] = source_units
        if full_section:
            story_config["source_excerpt"] = story_config.get("original_source_excerpt", story_config.get("source_excerpt", ""))
            story_config["source_units_override"] = story_config.get("original_source_units", source_units)
        elif prefix_chunks and suffix_units:
            story_config["source_excerpt"] = "\n".join(
                str(unit.get("source_text", "")).strip() for unit in suffix_units if str(unit.get("source_text", "")).strip()
            )
            story_config["source_units_override"] = suffix_units
    if feedback and story_config_path:
        story_config["replan_feedback"] = feedback
    if story_config_path:
        shared.atomic_json(story_config_path, story_config)

    preserved_chunks: list[dict] = []
    if prefix_chunks:
        for index, chunk in enumerate(prefix_chunks, start=1):
            original_id = str(chunk.get("original_chunk_id") or chunk.get("chunk_id") or f"chunk_{index:03d}")
            request = requests.get(str(chunk.get("chunk_id")), {})
            audio = Path(str(chunk.get("audio")))
            if archive is not None:
                try:
                    audio = archive / audio.relative_to(section_dir)
                except ValueError:
                    pass
            preserved_chunks.append(
                {
                    **chunk,
                    "chunk_id": f"preserved_{index:03d}",
                    "chunk_key": f"{section_id}/preserved_{index:03d}",
                    "original_chunk_id": original_id,
                    "source_unit_ids": chunk.get("source_unit_ids") or request.get("source_unit_ids", []),
                    "audio": str(audio),
                    "status": "accepted",
                    "preserved_from_replan": True,
                }
            )
    section["preserved_chunks"] = preserved_chunks
    section["replan_scope"] = {
        "mode": "full_section" if full_section or not preserved_chunks else "failed_suffix",
        "failed_chunk_id": (failed_chunk or {}).get("chunk_id"),
        "first_failed_source_unit_id": first_failed_unit,
        "preserved_chunk_count": len(preserved_chunks),
    }
    section["replan_feedback"] = feedback
    section["status"] = "planned"
    section["chunks"] = []
    section["final_audio"] = None
    section["last_error"] = None
    pilot = state.setdefault("pilot", {})
    was_approved = bool(pilot.get("approved"))
    approved_at = pilot.get("approved_at")
    selected_chunk_keys = list(pilot.get("selected_chunk_keys", []))
    counts = pilot.setdefault("auto_replan_counts", {})
    history = section.setdefault("replan_history", [])
    round_number = len(history) + 1
    if automatic:
        counts[section_id] = int(counts.get(section_id, 0)) + 1
    convergence = failure_convergence_metrics(feedback)
    history.append(
        {
            "round": round_number,
            "automatic_round": counts.get(section_id) if automatic else None,
            "automatic": automatic,
            "started_at": now_iso(),
            "failure_signature": failure_signature(feedback),
            "failure_family": convergence["family"],
            "failure_score": convergence["score"],
            "feedback": feedback,
            "parent_plan_fingerprint": parent_plan_fingerprint,
            "status": "planning_requested",
        }
    )
    section["pending_replan_round"] = round_number
    policy = replan_policy(state)
    state["pilot"] = {
        "required": True,
        "selected_chunk_keys": selected_chunk_keys if was_approved else [],
        "approved": was_approved,
        "approved_at": approved_at if was_approved else None,
        "auto_replan_limit_per_section": int(policy["max_structural_rounds_per_section"]),
        "auto_replan_counts": counts,
    }
    state["batch_canaries"] = {"selected_chunk_keys": [], "status": "pending"}
    state["status"] = "interrupted"
    state["current_stage"] = "replan_requested"
    state["current_item"] = section_id
    save_state(run_dir, state)
    append_event(
        run_dir,
        "section_replan_requested",
        section_id=section_id,
        automatic=automatic,
        feedback=feedback,
        scope=section["replan_scope"],
    )


def auto_replan_pilot_section(run_dir: Path, state: dict, section: dict, chunk: dict) -> bool:
    pilot = state.setdefault("pilot", {})
    policy = replan_policy(state)
    limit = int(policy["max_structural_rounds_per_section"])
    stagnant_limit = int(policy["max_stagnant_rounds"])
    counts = pilot.setdefault("auto_replan_counts", {})
    section_id = section["section_id"]
    if int(counts.get(section_id, 0)) >= limit:
        state["status"] = "human_review_required"
        state["current_stage"] = "structural_replan_budget_exhausted"
        state["current_item"] = section_id
        save_state(run_dir, state)
        append_event(
            run_dir,
            "structural_replan_budget_exhausted",
            section_id=section_id,
            chunk_key=chunk.get("chunk_key"),
            completed_rounds=int(counts.get(section_id, 0)),
            limit=limit,
        )
        return False
    current_failure = chunk.get("replan_feedback") or {
        "stage": "pilot",
        "failed_chunk_id": chunk.get("chunk_id"),
        "summary": chunk.get("last_error"),
    }
    current_signature = failure_signature(current_failure)
    current_convergence = failure_convergence_metrics(current_failure)
    history = section.setdefault("replan_history", [])
    previous = history[-1] if history else {}
    same_failure_family = bool(previous and previous.get("failure_family") == current_convergence["family"])
    previous_score = float(previous.get("failure_score") or 0)
    materially_improved = same_failure_family and current_convergence["score"] < previous_score - 0.25
    stagnant_rounds = int(section.get("stagnant_replan_rounds", 0))
    stagnant_rounds = stagnant_rounds + 1 if same_failure_family and not materially_improved else 0
    section["stagnant_replan_rounds"] = stagnant_rounds
    if stagnant_rounds >= stagnant_limit:
        state["status"] = "human_review_required"
        state["current_stage"] = "structural_replan_not_converging"
        state["current_item"] = section_id
        save_state(run_dir, state)
        append_event(
            run_dir,
            "structural_replan_stopped_no_improvement",
            section_id=section_id,
            chunk_key=chunk.get("chunk_key"),
            repeated_failure_signature=current_signature,
            failure_family=current_convergence["family"],
            previous_score=previous_score,
            current_score=current_convergence["score"],
            stagnant_rounds=stagnant_rounds,
        )
        return False
    next_round = int(counts.get(section_id, 0)) + 1
    feedback = {
        "stage": current_failure.get("stage", "pilot"),
        "round": next_round,
        "instruction": (
            "Create a materially different failed-suffix plan rather than appending another repair sentence. Change at least "
            "one causal dimension: chunk boundary, spoken density, role allocation, event chronology, or ending timeline. "
            "Preserve source order, fixed voices, key dialogue, and story meaning. Do not return the same source mapping, "
            "prompt, duration contract, and active-role plan as the parent round."
        ),
        "failure": current_failure,
        "prior_rounds": [
            {
                "round": item.get("round"),
                "failure_signature": item.get("failure_signature"),
                "failure_family": item.get("failure_family"),
                "failure_score": item.get("failure_score"),
                "plan_fingerprint": item.get("plan_fingerprint"),
                "status": item.get("status"),
            }
            for item in history[-2:]
        ],
    }
    replan_section(run_dir, state, section_id, feedback=feedback, automatic=True)
    state["status"] = "running"
    state["current_stage"] = "structural_auto_replan"
    state["current_item"] = section_id
    save_state(run_dir, state)
    append_event(run_dir, "structural_auto_replan_started", section_id=section_id, round=next_round, feedback=feedback)
    return True


def resume_pending_pilot_replan(run_dir: Path, state: dict) -> bool:
    for section in state.get("sections", []):
        if section.get("status") != "needs_replan":
            continue
        chunk = next((item for item in section.get("chunks", []) if item.get("status") == "needs_replan"), None)
        feedback = (chunk or {}).get("replan_feedback") or section.get("replan_feedback") or {}
        selected = set(state.get("pilot", {}).get("selected_chunk_keys", []))
        is_legacy_pilot_failure = bool(chunk and chunk.get("chunk_key") in selected and not feedback.get("stage"))
        if (
            chunk
            and (feedback.get("stage") in {"pilot", "batch"} or is_legacy_pilot_failure)
            and auto_replan_pilot_section(run_dir, state, section, chunk)
        ):
            return True
    return False


def retry_cached_static_gate_once(run_dir: Path, state: dict) -> bool:
    attempted = False
    for section in state.get("sections", []):
        feedback = section.get("replan_feedback") or {}
        last_error = str(section.get("last_error") or "")
        retryable_cached_validation = feedback.get("stage") == "static_input_gate" or any(
            pattern in last_error
            for pattern in (
                "without verifiable adjacent source evidence",
                "missing voice binding token",
                "read-risk prompt text",
            )
        )
        if section.get("status") not in {"needs_replan", "failed"} or not retryable_cached_validation:
            continue
        if section.get("cached_static_gate_retry_version") == CACHED_STATIC_GATE_RETRY_VERSION:
            continue
        if feedback.get("stage") != "static_input_gate":
            section["replan_feedback"] = {
                "stage": "cached_planner_validation",
                "summary": last_error[-1200:],
                "instruction": "Revalidate the cached response with the current deterministic compatibility repair only.",
            }
        section["cached_static_gate_retry_used"] = True
        section["cached_static_gate_retry_version"] = CACHED_STATIC_GATE_RETRY_VERSION
        section["status"] = "planned"
        section["chunks"] = []
        save_state(run_dir, state)
        append_event(run_dir, "cached_static_gate_retry_started", section_id=section["section_id"])
        attempted = True
        if not prepare_section(run_dir, state, section, cached_rewrite_only=True):
            return True
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
        "casting": state.get("casting"),
        "pilot": state.get("pilot"),
        "replan_policy": replan_policy(state),
        "structural_replans": {
            section.get("section_id"): {
                "automatic_rounds": state.get("pilot", {}).get("auto_replan_counts", {}).get(section.get("section_id"), 0),
                "history_rounds": len(section.get("replan_history", [])),
                "stagnant_rounds": section.get("stagnant_replan_rounds", 0),
                "latest": {
                    key: (section.get("replan_history") or [{}])[-1].get(key)
                    for key in (
                        "round", "automatic_round", "status", "failure_family", "failure_score",
                        "parent_plan_fingerprint", "plan_fingerprint", "same_plan_as_parent",
                    )
                },
            }
            for section in state.get("sections", [])
            if section.get("replan_history")
        },
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
    run_parser.add_argument(
        "--max-structural-replans",
        type=int,
        default=DEFAULT_MAX_STRUCTURAL_REPLANS,
        help="Maximum automatic structural failed-suffix replans per section (default: 3).",
    )
    run_parser.add_argument(
        "--max-stagnant-replans",
        type=int,
        default=DEFAULT_MAX_STAGNANT_REPLANS,
        help="Comparable non-improving structural rounds allowed before human review (default: 2).",
    )
    run_parser.add_argument("--prepare-only", action="store_true")
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("--run-id", required=True)
    resume_parser.add_argument("--max-structural-replans", type=int)
    resume_parser.add_argument("--max-stagnant-replans", type=int)
    approve_parser = sub.add_parser("approve-pilot")
    approve_parser.add_argument("--run-id", required=True)
    approve_casting_parser = sub.add_parser("approve-casting")
    approve_casting_parser.add_argument("--run-id", required=True)
    update_casting_parser = sub.add_parser("update-casting-registry")
    update_casting_parser.add_argument("--run-id", required=True)
    update_casting_parser.add_argument("--voice-registry", required=True)
    import_casting_parser = sub.add_parser("import-casting-samples")
    import_casting_parser.add_argument("--run-id", required=True)
    import_casting_parser.add_argument("--from-run-dir", required=True)
    import_plans_parser = sub.add_parser("import-frozen-plans")
    import_plans_parser.add_argument("--run-id", required=True)
    import_plans_parser.add_argument("--from-run-dir", required=True)
    import_plans_parser.add_argument("--role-aliases-json", required=True)
    accept_minor_parser = sub.add_parser("accept-minor-boundary")
    accept_minor_parser.add_argument("--run-id", required=True)
    accept_minor_parser.add_argument("--section-id", required=True)
    accept_minor_parser.add_argument("--chunk-id", required=True)
    retry_audio_parser = sub.add_parser("retry-audio")
    retry_audio_parser.add_argument("--run-id", required=True)
    retry_audio_parser.add_argument("--section-id", required=True)
    retry_audio_parser.add_argument("--chunk-id", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--run-id", required=True)
    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--run-id", required=True)
    replan_parser = sub.add_parser("replan")
    replan_parser.add_argument("--run-id", required=True)
    replan_parser.add_argument("--section-id", required=True)
    replan_parser.add_argument(
        "--instruction",
        help="Human structural direction used after automatic convergence stops.",
    )
    replan_parser.add_argument(
        "--full-section",
        action="store_true",
        help="Discard accepted prefix audio and replan the entire section instead of the failed suffix.",
    )
    recover_tail_parser = sub.add_parser("recover-local-tail")
    recover_tail_parser.add_argument("--run-id", required=True)
    recover_tail_parser.add_argument("--section-id", required=True)
    recover_tail_parser.add_argument("--chunk-id", required=True)
    args = parser.parse_args()

    if args.command == "run":
        run_dir, state = initialize(args)
        with run_lease(run_dir, state):
            if state.get("casting", {}).get("required") and not casting_is_approved(run_dir, state):
                prepare_casting_samples(run_dir, state)
                print_status(run_dir, state)
                return 0 if state.get("status") == "awaiting_casting_approval" else 3
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
    if args.command == "resume" and (
        args.max_structural_replans is not None or args.max_stagnant_replans is not None
    ):
        assert_no_active_lease(run_dir)
        policy = replan_policy(state)
        if args.max_structural_replans is not None:
            policy["max_structural_rounds_per_section"] = max(1, int(args.max_structural_replans))
            state.setdefault("pilot", {})["auto_replan_limit_per_section"] = policy["max_structural_rounds_per_section"]
        if args.max_stagnant_replans is not None:
            policy["max_stagnant_rounds"] = max(1, int(args.max_stagnant_replans))
        save_state(run_dir, state)
        append_event(run_dir, "replan_policy_updated", policy=policy)
    if args.command == "status":
        print_status(run_dir, state)
        return 0
    if args.command == "reconcile":
        print_status(run_dir, reconcile(run_dir, state))
        return 0
    if args.command == "update-casting-registry":
        changed_roles = update_casting_registry(run_dir, state, Path(args.voice_registry))
        print(json.dumps({"run_id": args.run_id, "changed_roles": changed_roles}, ensure_ascii=False, indent=2))
        print_status(run_dir, state)
        return 0
    if args.command == "import-casting-samples":
        imported_roles = import_casting_samples(run_dir, state, Path(args.from_run_dir))
        print(json.dumps({"run_id": args.run_id, "imported_roles": imported_roles}, ensure_ascii=False, indent=2))
        print_status(run_dir, state)
        return 0
    if args.command == "import-frozen-plans":
        import_frozen_plans(run_dir, state, Path(args.from_run_dir), Path(args.role_aliases_json))
        print_status(run_dir, state)
        return 0
    if args.command == "accept-minor-boundary":
        accept_nonblocking_minor_boundary(run_dir, state, args.section_id, args.chunk_id)
        print_status(run_dir, state)
        return 0
    if args.command == "retry-audio":
        assert_no_active_lease(run_dir)
        section = next(item for item in state["sections"] if item["section_id"] == args.section_id)
        chunk = next(item for item in section["chunks"] if item["chunk_id"] == args.chunk_id)
        if chunk.get("status") != "repair_audio":
            raise SystemExit("Only repair_audio chunks may receive an explicitly approved extra retry.")
        with run_lease(run_dir, state):
            ok = generate_chunk(run_dir, state, section, chunk, phase="batch", force=True)
            if ok:
                section["status"] = "prepared"
                state["status"] = "pilot_approved"
                state["current_stage"] = "extra_audio_retry_accepted"
                save_state(run_dir, state)
        print_status(run_dir, state)
        return 0 if ok else 3
    if args.command == "replan":
        assert_no_active_lease(run_dir)
        manual_feedback = None
        if args.instruction:
            section = next((item for item in state.get("sections", []) if item.get("section_id") == args.section_id), {})
            failed_chunk = next(
                (item for item in section.get("chunks", []) if item.get("status") == "needs_replan"),
                {},
            )
            manual_feedback = {
                "stage": "manual_structural_replan",
                "instruction": args.instruction.strip(),
                "failure": failed_chunk.get("replan_feedback") or section.get("replan_feedback") or {},
            }
        replan_section(
            run_dir,
            state,
            args.section_id,
            feedback=manual_feedback,
            full_section=args.full_section,
        )
        print_status(run_dir, state)
        return 0
    if args.command == "recover-local-tail":
        assert_no_active_lease(run_dir)
        with run_lease(run_dir, state):
            recovered = recover_local_tail(run_dir, state, args.section_id, args.chunk_id)
        print_status(run_dir, state)
        return 0 if recovered else 3
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
    if args.command == "approve-casting":
        assert_no_active_lease(run_dir)
        if state.get("status") != "awaiting_casting_approval":
            raise SystemExit("Casting can be approved only after all dry voice samples are ready.")
        state.setdefault("casting", {})["approved"] = True
        state["casting"]["approved_at"] = now_iso()
        state["casting"]["approved_registry_sha256"] = casting_registry_sha256(run_dir)
        state["status"] = "casting_approved"
        state["current_stage"] = "casting_approved"
        save_state(run_dir, state)
        append_event(run_dir, "casting_approved")
        print_status(run_dir, state)
        return 0
    if state.get("schema_version") != 2:
        raise SystemExit("This run uses the retired workflow schema; start a new final-workflow run id.")
    if state.get("casting", {}).get("required") and not casting_is_approved(run_dir, state):
        with run_lease(run_dir, state):
            prepare_casting_samples(run_dir, state)
        print_status(run_dir, state)
        return 0 if state.get("status") == "awaiting_casting_approval" else 3
    with run_lease(run_dir, state):
        cached_static_retry_attempted = retry_cached_static_gate_once(run_dir, state)
        if any(
            section.get("status") in {"needs_replan", "failed"}
            and (section.get("replan_feedback") or {}).get("stage") in {
                "static_input_gate",
                "cached_planner_validation",
            }
            and section.get("cached_static_gate_retry_version") == CACHED_STATIC_GATE_RETRY_VERSION
            for section in state["sections"]
        ):
            print_status(run_dir, state)
            return 3
        if any(section.get("status") == "needs_replan" for section in state["sections"]):
            if not resume_pending_pilot_replan(run_dir, state):
                raise SystemExit(
                    "A section is marked needs_replan but automatic structural recovery is unavailable, exhausted, or not converging; "
                    "inspect replan_history and provide an explicit replan direction before resuming providers."
                )
        state["status"] = "running"
        save_state(run_dir, state)
        if not all(
            section.get("chunks")
            and section.get("status") in {"prepared", "accepted", "needs_chunk_repair"}
            for section in state["sections"]
        ):
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
