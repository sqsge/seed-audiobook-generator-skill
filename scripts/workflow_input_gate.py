#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


BASE_PROMPT_BUDGET = int(os.getenv("SEED_AUDIO_BASE_PROMPT_BUDGET", "2700"))


def read_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def narrator_quotes(prompt: str) -> list[str]:
    return re.findall(
        r'Narrator\s*\([^\n]*actor is <<TGT_SPK\d+>>[^\n]*\):\s*"([^"]+)"',
        prompt,
        flags=re.I,
    )


def quote_is_complete(quote: str) -> bool:
    words = re.findall(r"[A-Za-z']+", quote.lower())
    dangling = {
        "a", "an", "the", "and", "but", "or", "to", "of", "for", "from", "with",
        "against", "into", "through", "that", "which", "his", "her", "their",
    }
    return bool(words) and words[-1] not in dangling and bool(re.search(r"[.!?]\s*$", quote))


def evaluate(section_dir: Path) -> dict:
    source_units = read_json(section_dir / "02_source_units.json")
    registry = read_json(section_dir / "04_voice_registry.json")
    roles = {item["role"]: item for item in registry.get("voices", [])}
    requests = [read_json(path) for path in sorted((section_dir / "06_generation_requests").glob("chunk_*.json"))]
    expected_ids = [item["source_unit_id"] for item in source_units]
    actual_ids = [unit_id for request in requests for unit_id in request.get("source_unit_ids", [])]
    section_failures: list[str] = []
    if actual_ids != expected_ids:
        section_failures.append("source_unit_coverage_or_order_mismatch")

    chunks: list[dict] = []
    for request in requests:
        prompt = str(request.get("text_prompt", ""))
        metrics = request.get("input_metrics", {})
        active_roles = request.get("active_roles", [])
        speakers: dict[str, list[str]] = {}
        missing_speakers: list[str] = []
        for role in active_roles:
            speaker = str(roles.get(role, {}).get("speaker", ""))
            if not speaker:
                missing_speakers.append(role)
            speakers.setdefault(speaker, []).append(role)
        conflicts = {speaker: names for speaker, names in speakers.items() if speaker and len(names) > 1}
        quotes = narrator_quotes(prompt)
        incomplete_quotes = [quote for quote in quotes if not quote_is_complete(quote)]
        failures: list[str] = []
        if len(prompt) > BASE_PROMPT_BUDGET:
            failures.append("base_prompt_exceeds_repair_reserve_budget")
        if len(active_roles) > 3:
            failures.append("more_than_three_active_roles")
        if conflicts:
            failures.append("active_roles_share_provider_speaker")
        if missing_speakers:
            failures.append("active_role_missing_provider_speaker")
        if incomplete_quotes:
            failures.append("incomplete_narrator_line")
        required_narration = min(4, int(metrics.get("narrator_unit_count", 0)))
        if metrics.get("dialogue_unit_count", 0) == 0 and required_narration >= 3 and len(quotes) < required_narration:
            failures.append("insufficient_narration_coverage")
        if metrics.get("unbound_quoted_dialogue_line_count", 0) > 0:
            failures.append("unbound_quoted_dialogue")
        if metrics.get("must_keep_dialogue_count", 0) > metrics.get("must_keep_dialogue_present_count", 0):
            failures.append("missing_must_keep_dialogue")
        for marker in (
            "All audible speech must be English only. Do not translate or speak Chinese.",
            "Voice continuity:",
            "one voice at a time",
            "no overlapping narration and dialogue",
        ):
            if marker not in prompt:
                failures.append(f"missing_required_marker:{marker}")
        expected = request.get("expected_duration_sec", {})
        ceiling = metrics.get("audio_drama_estimated_duration_ceiling_sec")
        warnings: list[str] = []
        if ceiling and expected.get("max") and expected["max"] > ceiling * 1.8:
            warnings.append("declared_duration_is_inconsistent_with_playable_content")
        chunks.append(
            {
                "chunk_id": request.get("chunk_id"),
                "status": "pass" if not failures else "fail",
                "prompt_chars": len(prompt),
                "base_prompt_budget": BASE_PROMPT_BUDGET,
                "source_unit_ids": request.get("source_unit_ids", []),
                "active_roles": active_roles,
                "speaker_conflicts": conflicts,
                "active_roles_missing_speaker": missing_speakers,
                "narrator_line_count": len(quotes),
                "incomplete_narrator_lines": incomplete_quotes,
                "failures": failures,
                "warnings": warnings,
            }
        )
    failed_chunks = [item["chunk_id"] for item in chunks if item["status"] == "fail"]
    return {
        "status": "pass" if not section_failures and not failed_chunks else "fail",
        "section_failures": section_failures,
        "failed_chunk_ids": failed_chunks,
        "policy": {
            "base_prompt_budget": BASE_PROMPT_BUDGET,
            "repair_reserve_chars": 3000 - BASE_PROMPT_BUDGET,
            "provider_calls_allowed_on_fail": False,
        },
        "chunks": chunks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Static pre-generation gate for prepared Audio 1.0 inputs.")
    parser.add_argument("--section-dir", required=True)
    args = parser.parse_args()
    section_dir = Path(args.section_dir).expanduser().resolve()
    report = evaluate(section_dir)
    write_json(section_dir / "logs" / "pre_generation_input_gate.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())
