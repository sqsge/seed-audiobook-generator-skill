#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import os
from pathlib import Path

import common
import long_text_batch_planner as planner


common.load_tool_env()

ROOT = Path(__file__).resolve().parents[1]
CHAPTER_OUTPUT_ROOT = ROOT / "outputs" / "chapter_runs"
AUDIOBOOK_WORKFLOW = ROOT / "scripts" / "audiobook_workflow.py"


def safe_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_").lower()
    return slug[:80] or "chapter_audio_drama"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: object) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_latest_attempt(case_dir: Path, payload: dict, *, mode: str) -> None:
    write_json(case_dir / "latest_attempt.json", payload)
    if mode == "prepare":
        write_json(case_dir / "latest_prepare_attempt.json", payload)
    elif mode == "rewrite":
        write_json(case_dir / "latest_rewrite_attempt.json", payload)
    else:
        write_json(case_dir / "latest_generation_attempt.json", payload)


def env_present(*names: str) -> bool:
    return any(os.getenv(name, "").strip() for name in names)


def roles_need_tts_references(roles: dict) -> bool:
    return any(str(spec.get("reference_mode", "speaker")).strip() == "tts_audio" for spec in roles.values())


def speaker_reuse_warnings(roles: dict) -> list[dict]:
    by_speaker: dict[str, list[str]] = {}
    for role, spec in roles.items():
        if str(spec.get("reference_mode", "speaker")).strip() != "speaker":
            continue
        speaker = str(spec.get("default_speaker", "")).strip()
        if not speaker:
            continue
        by_speaker.setdefault(speaker, []).append(role)
    return [
        {
            "speaker": speaker,
            "roles": role_names,
            "severity": "warning",
            "reason": "These roles share a provider speaker id. They should not appear as separate active speakers in the same Seed Audio request unless the speaker mapping is changed.",
        }
        for speaker, role_names in sorted(by_speaker.items())
        if len(role_names) > 1
    ]


def preflight_environment(roles: dict, mode: str) -> dict:
    needs_tts = roles_need_tts_references(roles)
    checks = [
        {
            "name": "seed2_rewrite",
            "ok": env_present("LLM_API_KEY", "ARK_API_KEY", "BYTEPLUS_API_KEY", "OPENAI_API_KEY"),
            "required": "LLM_API_KEY or ARK_API_KEY or BYTEPLUS_API_KEY or OPENAI_API_KEY",
        },
    ]
    if mode == "generate":
        checks.extend(
            [
                {
                    "name": "tts_voice_references",
                    "ok": True if not needs_tts else env_present("TTS_API_KEY"),
                    "required": "TTS_API_KEY only when any role uses reference_mode=tts_audio",
                    "skipped": not needs_tts,
                },
                {
                    "name": "seed_audio_generation",
                    "ok": env_present("SEED_AUDIO_API_KEY", "TTS_API_KEY", "BYTEPLUS_API_KEY")
                    or (env_present("SEED_AUDIO_APP_ID") and env_present("SEED_AUDIO_ACCESS_KEY")),
                    "required": "SEED_AUDIO_API_KEY or TTS_API_KEY or BYTEPLUS_API_KEY, or SEED_AUDIO_APP_ID plus SEED_AUDIO_ACCESS_KEY",
                },
                {
                    "name": "asr_language_gate",
                    "ok": env_present("LAS_API_KEY", "ASR_API_KEY"),
                    "required": "LAS_API_KEY or ASR_API_KEY",
                },
            ]
        )
    missing = [item for item in checks if not item["ok"]]
    return {
        "status": "pass" if not missing else "fail",
        "mode": mode,
        "checks": checks,
        "missing": missing,
        "speaker_reuse_warnings": speaker_reuse_warnings(roles),
    }


def chapter28_roles() -> dict:
    return {
        "Narrator": {
            "key": "narrator",
            "label": "Narrator",
            "reference_mode": "speaker",
            "default_speaker": "en_male_knightley_uranus_bigtts",
            "description": "English audio-drama narrator, clear, cinematic, urgent but controlled, not mechanical",
            "attribution_keywords": [],
            "reference_prompt": (
                "The castle was no longer a school in the dark; it was a maze of shouting, smoke, "
                "running feet, and broken stone, and Harry followed the only thing left in his mind."
            ),
        },
        "Harry Potter": {
            "key": "harry_potter",
            "label": "Harry Potter",
            "reference_mode": "speaker",
            "default_speaker": "en_male_josh_uranus_bigtts",
            "description": "teenage male, breathless, furious, grief-struck, reckless, voice cracking under rage",
            "attribution_keywords": ["Harry", "Potter"],
            "reference_prompt": (
                "Move. Get out of the way. I have to catch him before he reaches the gates. "
                "Fight back, you coward."
            ),
        },
        "Severus Snape": {
            "key": "severus_snape",
            "label": "Severus Snape",
            "reference_mode": "speaker",
            "default_speaker": "en_male_hades_uranus_bigtts",
            "description": "adult male, low, controlled, cutting, cold fury held under precise diction",
            "attribution_keywords": ["Snape"],
            "reference_prompt": (
                "Blocked again and again, Potter, until you learn to keep your mouth shut and your mind closed."
            ),
        },
        "Draco Malfoy": {
            "key": "draco_malfoy",
            "label": "Draco Malfoy",
            "reference_mode": "speaker",
            "default_speaker": "en_male_ronald_uranus_bigtts",
            "description": "teenage male, frightened, strained, trying to sound hard but shaken",
            "attribution_keywords": ["Draco", "Malfoy"],
            "reference_prompt": "He sounds shaken, breath catching as he tries to keep moving through the dark.",
        },
        "Hagrid": {
            "key": "hagrid",
            "label": "Hagrid",
            "reference_mode": "speaker",
            "default_speaker": "en_male_ronald_uranus_bigtts",
            "description": "large warm adult male, rough, emotional, loud with panic and grief",
            "attribution_keywords": ["Hagrid"],
            "reference_prompt": "Harry, where are yeh? Fang, stay back. The hut is burning, move, move!",
        },
        "Ginny Weasley": {
            "key": "ginny_weasley",
            "label": "Ginny Weasley",
            "reference_mode": "speaker",
            "default_speaker": "en_female_rachel_p1_uranus_bigtts",
            "description": "teenage female, brave, quick, tense, fighting through fear",
            "attribution_keywords": ["Ginny"],
            "reference_prompt": "Harry, where did you come from? Keep your head down and move.",
        },
        "Minerva McGonagall": {
            "key": "minerva_mcgonagall",
            "label": "Minerva McGonagall",
            "reference_mode": "speaker",
            "default_speaker": "en_female_rachel_p1_uranus_bigtts",
            "description": "older female professor, sharp, authoritative, urgent, disciplined",
            "attribution_keywords": ["McGonagall", "Professor McGonagall"],
            "reference_prompt": "Harry, no. Get back. The castle is not clear, and you are not to run into the dark alone.",
        },
        "Death Eater": {
            "key": "death_eater",
            "label": "Death Eater",
            "reference_mode": "speaker",
            "default_speaker": "en_female_rachel_p1_uranus_bigtts",
            "description": "adult villain voice, harsh, dangerous, sometimes jeering, never cartoonish",
            "attribution_keywords": ["Death Eater", "Amycus", "Alecto", "Fenrir"],
            "reference_prompt": "Run, Draco. Keep moving. The Ministry will be here soon.",
        },
    }


def build_story_config(section_id: str, title: str, source_text: str) -> dict:
    return {
        "scene_id": section_id,
        "production_mode": "audio_drama_adaptation",
        "prompt_language": "en",
        "auto_planning": True,
        "lock_roles": True,
        "source_title": title,
        "source_url": "local:chapter_source_section",
        "source_excerpt": source_text,
        "roles": chapter28_roles(),
    }


def audio_duration(path: Path) -> float | None:
    result = run(["ffmpeg", "-hide_banner", "-i", str(path), "-f", "null", "-"])
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def find_final_audio(section_dir: Path) -> Path | None:
    candidates = sorted((section_dir / "08_stitched").glob("*.wav"))
    return candidates[0] if candidates else None


def request_speaker_conflicts(request: dict, roles: dict) -> dict[str, list[str]]:
    by_speaker: dict[str, list[str]] = {}
    for role in request.get("active_roles", []):
        spec = roles.get(role, {})
        if str(spec.get("reference_mode", "speaker")).strip() != "speaker":
            continue
        speaker = str(spec.get("default_speaker", "")).strip()
        if speaker:
            by_speaker.setdefault(speaker, []).append(role)
    return {speaker: role_names for speaker, role_names in by_speaker.items() if len(role_names) > 1}


def section_status(section_dir: Path, *, mode: str, roles: dict) -> dict:
    request_paths = sorted((section_dir / "06_generation_requests").glob("chunk_*.json"))
    requests = [read_json(path) for path in request_paths]
    prompt_paths = sorted((section_dir / "05_director_prompt_chunks").glob("chunk_*.txt"))
    rewrite_ok = bool((section_dir / "manifest.json").exists() and request_paths and prompt_paths)
    prompt_lengths = [len(path.read_text(encoding="utf-8")) for path in prompt_paths if path.exists()]
    speaker_conflicts = [
        {
            "request": str(path.relative_to(ROOT)),
            "conflicts": request_speaker_conflicts(request, roles),
        }
        for path, request in zip(request_paths, requests)
        if request_speaker_conflicts(request, roles)
    ]
    quality = read_json(section_dir / "logs" / "audio_quality_report.json")
    asr = read_json(section_dir / "logs" / "asr_language_report.json")
    final_audio = find_final_audio(section_dir)
    if mode == "rewrite":
        quality_status = "planned"
        asr_status = "planned"
        passed = rewrite_ok and not speaker_conflicts
    else:
        quality_status = quality.get("status", "missing")
        asr_status = asr.get("status", "missing")
        passed = quality.get("status") == "pass" and asr.get("status") == "pass" and bool(final_audio)
    return {
        "section_id": section_dir.name,
        "rewrite_status": "pass" if rewrite_ok else "missing",
        "prompt_count": len(prompt_paths),
        "request_count": len(request_paths),
        "prompt_lengths": prompt_lengths,
        "speaker_conflicts": speaker_conflicts,
        "final_audio": str(final_audio.relative_to(ROOT)) if final_audio else None,
        "duration_sec": audio_duration(final_audio) if final_audio else None,
        "quality_status": quality_status,
        "quality_fail_reasons": quality.get("fail_reasons", []),
        "asr_status": asr_status,
        "asr_fail_reasons": asr.get("fail_reasons", []),
        "pass": passed,
    }


def section_is_reusable(section_dir: Path, *, mode: str, roles: dict) -> tuple[bool, dict]:
    status = section_status(section_dir, mode=mode, roles=roles)
    if mode == "rewrite":
        return bool(status["pass"]), status
    return bool(status["pass"] and status.get("final_audio") and status.get("quality_status") == "pass" and status.get("asr_status") == "pass"), status


def archive_section_dir(section_dir: Path, reason: str) -> Path:
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "_", reason.strip()).strip("_").lower() or "incomplete"
    archived = section_dir.with_name(f"{section_dir.name}.{suffix}_{time.strftime('%Y%m%d-%H%M%S')}")
    counter = 1
    while archived.exists():
        archived = section_dir.with_name(f"{section_dir.name}.{suffix}_{time.strftime('%Y%m%d-%H%M%S')}_{counter}")
        counter += 1
    section_dir.rename(archived)
    return archived


def stitch_chapter(attempt_dir: Path, section_audios: list[Path], output_name: str) -> Path:
    stitched_dir = attempt_dir / "stitched"
    stitched_dir.mkdir(parents=True, exist_ok=True)
    concat = stitched_dir / "concat.txt"
    write_text(concat, "".join(f"file '{path.as_posix()}'\n" for path in section_audios))
    output = stitched_dir / output_name
    result = run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-ar",
            "24000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    )
    if result.returncode != 0:
        raise SystemExit(f"Chapter stitch failed: {result.stderr.strip()}")
    return output


def build_chapter_plan(source_path: Path, target_chars: int, max_chars: int) -> tuple[str, list[dict], dict]:
    raw = planner.read_source(source_path)
    clean, notes = planner.clean_source(raw)
    chapters = planner.build_chapters(clean, target_chars=target_chars, max_chars=max_chars)
    coverage = planner.coverage_report(clean, chapters)
    report = planner.preprocessing_report(source_path, raw, clean, chapters, notes)
    report["coverage"] = coverage
    return clean, chapters, report


def section_char_summary(chapters: list[dict]) -> dict:
    counts = [len(chapter.get("_chapter_text", "")) for chapter in chapters]
    return {
        "section_count": len(counts),
        "min_chars": min(counts) if counts else 0,
        "max_chars": max(counts) if counts else 0,
        "total_chars": sum(counts),
        "counts": counts,
    }


def chapter_acceptance(
    *,
    case_dir: Path,
    preprocessing: dict,
    roles: dict,
    section_reports: list[dict],
    chapters: list[dict],
    final_audio: str | None,
    generate: bool,
    prepare_only: bool,
) -> dict:
    return {
        "source_coverage_ok": preprocessing.get("coverage", {}).get("coverage_ok") is True,
        "source_coverage": preprocessing.get("coverage", {}),
        "section_char_summary": section_char_summary(chapters),
        "speaker_reuse_warnings": speaker_reuse_warnings(roles),
        "all_sections_generated": len(section_reports) == len(chapters),
        "all_sections_quality_pass": None if prepare_only else bool(section_reports) and all(item["quality_status"] == "pass" for item in section_reports),
        "all_sections_asr_pass": None if prepare_only else bool(section_reports) and all(item["asr_status"] == "pass" for item in section_reports),
        "stitched_only_after_pass": bool(final_audio) if generate else None,
        "fixed_voice_registry": True,
        "stable_case_directory": str(case_dir.relative_to(ROOT)),
    }


def aggregate_chapter_asr(attempt_dir: Path, section_reports: list[dict]) -> dict:
    chunks = []
    fail_reasons: list[str] = []
    planned_count = 0
    for section in section_reports:
        section_id = section.get("section_id")
        if not section_id:
            continue
        if section.get("asr_status") == "planned":
            planned_count += 1
        report_path = attempt_dir / "chunks" / section_id / "logs" / "asr_language_report.json"
        report = read_json(report_path)
        item = {
            "section_id": section_id,
            "section_asr_status": section.get("asr_status"),
            "report_path": str(report_path.relative_to(ROOT)) if report_path.exists() else None,
            "fail_reasons": section.get("asr_fail_reasons", []),
            "chunks": report.get("chunks", []),
        }
        if section.get("asr_status") not in {"pass", "planned"}:
            fail_reasons.append(f"{section_id}: {section.get('asr_status')}")
        chunks.append(item)
    if not chunks or planned_count == len(chunks):
        status = "planned"
    elif fail_reasons:
        status = "fail"
    else:
        status = "pass"
    return {
        "status": status,
        "fail_reasons": fail_reasons,
        "section_count": len(chunks),
        "policy": {
            "language": "English",
            "gate": "Every section must pass its local ASR language report before chapter stitching.",
            "final_full_audio_asr": "Not run from local stitched wav unless a public audio URL is available; section ASR reports are aggregated here.",
        },
        "sections": chunks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a full chapter through the audio-drama workflow.")
    parser.add_argument("--source-file", required=True, help="Complete chapter source text.")
    parser.add_argument("--case-id", help="Stable case id under outputs/chapter_runs.")
    parser.add_argument("--attempt-id", help="Optional attempt id. Defaults to timestamp.")
    parser.add_argument("--target-chars", type=int, default=2400, help="Preferred source section size.")
    parser.add_argument("--max-chars", type=int, default=2800, help="Hard section size.")
    parser.add_argument("--section-limit", type=int, help="Only process the first N sections, for debugging.")
    parser.add_argument("--only-section", action="append", default=[], help="Only process selected section ids, e.g. chunk_003. Can be repeated.")
    parser.add_argument("--start-section", help="Start processing from this section id, e.g. chunk_003.")
    parser.add_argument("--generate", action="store_true", help="Call Seed Audio and ASR for every section.")
    parser.add_argument("--prepare-only", action="store_true", help="Only write chapter plan, fixed voice registry, and per-section story configs.")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip API environment preflight for debugging.")
    parser.add_argument("--skip-existing", action="store_true", help="Reuse an existing section directory when present.")
    parser.add_argument("--section-retries", type=int, default=2, help="Retry a failed section this many times before failing the chapter.")
    args = parser.parse_args()

    source_path = Path(args.source_file)
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    if not source_path.exists():
        raise SystemExit(f"Source file does not exist: {source_path}")
    if args.target_chars > args.max_chars:
        raise SystemExit("--target-chars must be <= --max-chars.")

    case_id = args.case_id or safe_slug(source_path.stem)
    attempt_id = args.attempt_id or time.strftime("%Y%m%d-%H%M%S")
    case_dir = CHAPTER_OUTPUT_ROOT / case_id
    attempt_dir = case_dir / "attempts" / attempt_id
    if attempt_dir.exists() and not args.skip_existing:
        raise SystemExit(f"Attempt directory already exists: {attempt_dir.relative_to(ROOT)}")
    attempt_dir.mkdir(parents=True, exist_ok=True)

    clean, chapters, preprocessing = build_chapter_plan(source_path, args.target_chars, args.max_chars)
    if args.section_limit:
        chapters = chapters[: args.section_limit]
    indexed_chapters = []
    for index, chapter in enumerate(chapters, start=1):
        item = dict(chapter)
        item["_section_id"] = f"chunk_{index:03d}"
        indexed_chapters.append(item)
    full_indexed_chapters = list(indexed_chapters)
    if args.start_section:
        start_index = next((idx for idx, item in enumerate(indexed_chapters) if item["_section_id"] == args.start_section), None)
        if start_index is None:
            raise SystemExit(f"Unknown --start-section: {args.start_section}")
        indexed_chapters = indexed_chapters[start_index:]
    if args.only_section:
        wanted = set(args.only_section)
        known = {item["_section_id"] for item in indexed_chapters}
        unknown = sorted(wanted - known)
        if unknown:
            raise SystemExit(f"Unknown --only-section values: {unknown}")
        indexed_chapters = [item for item in indexed_chapters if item["_section_id"] in wanted]
    chapters = indexed_chapters

    roles = chapter28_roles()
    run_mode = "prepare" if args.prepare_only else ("generate" if args.generate else "rewrite")

    write_text(case_dir / "source_chapter.txt", clean)
    write_json(case_dir / "voice_registry.json", {"roles": roles, "speaker_reuse_warnings": speaker_reuse_warnings(roles)})
    write_json(case_dir / "chapter_plan.json", {"chapters": [{k: v for k, v in item.items() if not k.startswith("_")} for item in full_indexed_chapters]})
    write_json(attempt_dir / "preprocessing_report.json", preprocessing)
    write_json(attempt_dir / "chapter_plan.json", {"chapters": [{k: v for k, v in item.items() if not k.startswith("_")} for item in chapters]})
    write_json(attempt_dir / "voice_registry.json", {"roles": roles, "speaker_reuse_warnings": speaker_reuse_warnings(roles)})

    preflight = preflight_environment(roles, run_mode) if run_mode in {"rewrite", "generate"} else {"status": "skipped", "mode": run_mode}
    write_json(attempt_dir / "preflight.json", preflight)
    if run_mode in {"rewrite", "generate"} and not args.skip_preflight and preflight["status"] != "pass":
        chunk_manifest = {
            "case_id": case_id,
            "attempt_id": attempt_id,
            "source_file": str(source_path),
            "generate": args.generate,
            "prepare_only": args.prepare_only,
            "run_mode": run_mode,
            "section_count": len(chapters),
            "target_chars": args.target_chars,
            "max_chars": args.max_chars,
            "only_section": args.only_section,
            "start_section": args.start_section,
            "sections": [],
            "preflight": preflight,
            "source_coverage": preprocessing.get("coverage", {}),
            "section_char_summary": section_char_summary(chapters),
            "speaker_reuse_warnings": speaker_reuse_warnings(roles),
        }
        chapter_report = {
            "status": "fail",
            "fail_reasons": [f"missing_api_environment:{item['name']} requires {item['required']}" for item in preflight["missing"]],
            "final_audio": None,
            "final_duration_sec": None,
            "section_count": len(chapters),
            "section_duration_sec": 0,
            "preflight": preflight,
            "acceptance": chapter_acceptance(
                case_dir=case_dir,
                preprocessing=preprocessing,
                roles=roles,
                section_reports=[],
                chapters=chapters,
                final_audio=None,
                generate=args.generate,
                prepare_only=False,
            ),
        }
        write_json(attempt_dir / "chunk_manifest.json", chunk_manifest)
        write_json(attempt_dir / "reports" / "chapter_quality_report.json", chapter_report)
        write_json(attempt_dir / "reports" / "chapter_asr_report.json", aggregate_chapter_asr(attempt_dir, []))
        write_latest_attempt(
            case_dir,
            {"attempt_id": attempt_id, "attempt_dir": str(attempt_dir.relative_to(ROOT)), **chapter_report},
            mode=run_mode,
        )
        print(json.dumps({"attempt_dir": str(attempt_dir.relative_to(ROOT)), **chapter_report}, ensure_ascii=False, indent=2))
        return 2

    section_reports: list[dict] = []
    for index, chapter in enumerate(chapters, start=1):
        section_id = chapter.get("_section_id") or f"chunk_{index:03d}"
        section_dir = attempt_dir / "chunks" / section_id
        input_dir = attempt_dir / "inputs" / section_id
        source_text = chapter["_chapter_text"].strip() + "\n"
        source_file = input_dir / "source.txt"
        config_file = input_dir / "story_config.json"
        write_text(source_file, source_text)
        write_json(
            config_file,
            build_story_config(
                section_id=f"{case_id}_{section_id}",
                title=f"Chapter 28: Flight of the Prince - section {index:03d}",
                source_text=source_text,
            ),
        )
        if args.prepare_only:
            section_reports.append(
                {
                    "section_id": section_id,
                    "source_file": str(source_file.relative_to(ROOT)),
                    "story_config": str(config_file.relative_to(ROOT)),
                    "char_count": len(source_text),
                    "quality_status": "planned",
                    "asr_status": "planned",
                    "pass": False,
                }
            )
            continue
        section_mode = "generate" if args.generate else "rewrite"
        if args.skip_existing and section_dir.exists():
            reusable, existing_status = section_is_reusable(section_dir, mode=section_mode, roles=roles)
            if reusable:
                section_reports.append(existing_status)
                continue
            archive_section_dir(section_dir, f"not_reusable_{existing_status.get('quality_status', 'missing')}_{existing_status.get('asr_status', 'missing')}")

        last_result: subprocess.CompletedProcess[str] | None = None
        last_status: dict | None = None
        max_attempts = max(1, args.section_retries + 1)
        for section_attempt in range(1, max_attempts + 1):
            if section_dir.exists():
                archive_section_dir(section_dir, f"retry_{section_attempt}")
            cmd = [
                sys.executable,
                str(AUDIOBOOK_WORKFLOW),
                "--story-config",
                str(config_file),
                "--run-id",
                section_id,
                "--output-root",
                str(attempt_dir / "chunks"),
            ]
            if args.generate:
                cmd.append("--generate")
            result = run(cmd)
            last_result = result
            write_text(section_dir / "logs" / "chapter_driver_stdout.txt", result.stdout)
            write_text(section_dir / "logs" / "chapter_driver_stderr.txt", result.stderr)
            if result.returncode == 0:
                last_status = section_status(section_dir, mode=section_mode, roles=roles)
                if last_status["pass"]:
                    break
            if section_attempt < max_attempts and section_dir.exists():
                reason = "returncode" if result.returncode != 0 else f"quality_{(last_status or {}).get('quality_status', 'missing')}_asr_{(last_status or {}).get('asr_status', 'missing')}"
                archive_section_dir(section_dir, reason)
                time.sleep(2 * section_attempt)
                continue
            break
        if last_result is not None and last_result.returncode != 0:
            chapter_report = {
                "status": "fail",
                "failed_section": section_id,
                "returncode": last_result.returncode,
                "stderr": last_result.stderr[-4000:],
                "completed_sections": section_reports,
                "preflight": preflight,
            }
            write_json(attempt_dir / "reports" / "chapter_quality_report.json", chapter_report)
            write_latest_attempt(
                case_dir,
                {"attempt_id": attempt_id, "attempt_dir": str(attempt_dir.relative_to(ROOT)), **chapter_report},
                mode="generate" if args.generate else "rewrite",
            )
            raise SystemExit(f"Section {section_id} failed: {last_result.stderr.strip()[-1200:]}")
        if last_status is None:
            last_status = section_status(section_dir, mode=section_mode, roles=roles)
        section_reports.append(last_status)

    section_audios = []
    failed = [] if args.prepare_only else [item for item in section_reports if not item["pass"]]
    if args.generate and not args.prepare_only and not failed:
        for item in section_reports:
            audio = ROOT / item["final_audio"]
            section_audios.append(audio)
        full = stitch_chapter(attempt_dir, section_audios, f"{case_id}_full.wav")
        final_audio = str(full.relative_to(ROOT))
        final_duration = audio_duration(full)
    else:
        final_audio = None
        final_duration = None

    chunk_manifest = {
        "case_id": case_id,
        "attempt_id": attempt_id,
        "source_file": str(source_path),
            "generate": args.generate,
            "prepare_only": args.prepare_only,
            "run_mode": run_mode,
        "section_count": len(chapters),
        "target_chars": args.target_chars,
        "max_chars": args.max_chars,
        "only_section": args.only_section,
        "start_section": args.start_section,
        "sections": section_reports,
        "source_coverage": preprocessing.get("coverage", {}),
        "section_char_summary": section_char_summary(chapters),
        "speaker_reuse_warnings": speaker_reuse_warnings(roles),
    }
    chapter_report = {
        "status": "planned" if args.prepare_only else ("fail" if failed else "pass"),
        "fail_reasons": [
            f"{item['section_id']}: quality={item['quality_status']} asr={item['asr_status']}"
            for item in failed
        ],
        "final_audio": final_audio,
        "final_duration_sec": final_duration,
        "section_count": len(section_reports),
        "section_duration_sec": sum((item.get("duration_sec") or 0) for item in section_reports),
        "acceptance": chapter_acceptance(
            case_dir=case_dir,
            preprocessing=preprocessing,
            roles=roles,
            section_reports=section_reports,
            chapters=chapters,
            final_audio=final_audio,
            generate=args.generate,
            prepare_only=args.prepare_only,
        ),
    }
    chapter_asr_report = aggregate_chapter_asr(attempt_dir, section_reports)
    write_json(attempt_dir / "chunk_manifest.json", chunk_manifest)
    write_json(attempt_dir / "reports" / "chapter_quality_report.json", chapter_report)
    write_json(attempt_dir / "reports" / "chapter_asr_report.json", chapter_asr_report)
    write_latest_attempt(
        case_dir,
        {"attempt_id": attempt_id, "attempt_dir": str(attempt_dir.relative_to(ROOT)), **chapter_report},
        mode=run_mode,
    )
    print(json.dumps({"attempt_dir": str(attempt_dir.relative_to(ROOT)), **chapter_report}, ensure_ascii=False, indent=2))
    if args.prepare_only:
        return 0
    if failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
