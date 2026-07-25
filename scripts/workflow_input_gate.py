#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


BASE_PROMPT_BUDGET = int(os.getenv("SEED_AUDIO_BASE_PROMPT_BUDGET", "2700"))
MAX_PROMPT_CHARS = int(os.getenv("SEED_AUDIO_MAX_CHARS", "3000"))
SAFE_DURATION_CEILING_SEC = float(os.getenv("SEED_AUDIO_SAFE_DURATION_CEILING_SEC", "60"))


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
    # Terminal punctuation is authoritative. Complete questions may
    # legitimately end in words such as "that" or "they".
    return bool(re.search(r"[.!?]\s*$", quote))


def is_dialogue_attribution_text(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip(" \t\r\n.,!?;:\"'")
    verb = r"said|says|yelled|shouted|cried|asked|answered|whispered|murmured|called|replied"
    name = r"[A-Z][A-Za-z' -]*(?:\s+[A-Z][A-Za-z' -]*)?"
    return bool(
        re.fullmatch(rf"(?:{verb})\s+{name}", cleaned, flags=re.I)
        or re.fullmatch(rf"{name}\s+(?:{verb})", cleaned, flags=re.I)
    )


def same_quote_turn(left: dict, right: dict) -> bool:
    """Return true when two sentence units came from one original quotation."""
    if left.get("source_kind") != "quoted_text" or right.get("source_kind") != "quoted_text":
        return False
    if left.get("paragraph_id") != right.get("paragraph_id"):
        return False
    left_quote = left.get("quote_span_id")
    right_quote = right.get("quote_span_id")
    if left_quote and right_quote:
        return left_quote == right_quote
    left_span = left.get("source_span") or {}
    right_span = right.get("source_span") or {}
    try:
        spans = sorted(
            (
                (int(left_span["char_start"]), int(left_span["char_end"])),
                (int(right_span["char_start"]), int(right_span["char_end"])),
            )
        )
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= spans[1][0] - spans[0][1] <= 1


def named_speaker_has_source_evidence(
    parsed_units: list[dict], index: int, role_specs: dict, _visited: set[int] | None = None
) -> bool:
    visited = set(_visited or set())
    if index in visited:
        return False
    visited.add(index)
    unit = parsed_units[index]
    role = str(unit.get("speaker") or "")
    if unit.get("source_kind") != "quoted_text" or role == "Narrator":
        return True
    spec = role_specs.get(role, {})
    keywords = [role, str(spec.get("label") or ""), *(spec.get("attribution_keywords", []) or [])]
    keywords.extend(part for part in role.split() if len(part) >= 3)
    attribution_stopwords = {"said", "asked", "yelled", "shouted", "cried", "answered", "whispered", "murmured", "called", "replied", "snapped", "demanded"}
    keywords.extend(
        part
        for keyword in spec.get("attribution_keywords", []) or []
        for part in re.findall(r"[A-Za-z']+", str(keyword))
        if len(part) >= 4 and part.lower() not in attribution_stopwords
    )
    patterns = [re.compile(rf"\b{re.escape(str(keyword))}\b", re.I) for keyword in keywords if str(keyword).strip()]
    evidence_texts = [str(unit.get("quote_attribution_text") or "")]
    for neighbor_index in (index - 1, index + 1):
        if (
            0 <= neighbor_index < len(parsed_units)
            and (
                not unit.get("paragraph_id")
                or not parsed_units[neighbor_index].get("paragraph_id")
                or parsed_units[neighbor_index].get("paragraph_id") == unit.get("paragraph_id")
            )
        ):
            evidence_texts.append(str(parsed_units[neighbor_index].get("source_text") or ""))
    if any(pattern.search(text) for pattern in patterns for text in evidence_texts):
        return True
    if not re.search(r"[A-Za-z0-9]", str(unit.get("source_text") or "")):
        return False
    paragraph_id = unit.get("paragraph_id")
    for prior_index in range(index - 1, max(-1, index - 4), -1):
        prior = parsed_units[prior_index]
        if paragraph_id and prior.get("paragraph_id") and prior.get("paragraph_id") != paragraph_id:
            break
        if prior.get("source_kind") == "quoted_text":
            break
        if any(pattern.search(str(prior.get("source_text") or "")) for pattern in patterns):
            return True
    previous_quote_index = index - 1
    if index > 1 and parsed_units[index - 1].get("source_kind") == "narrative_text":
        previous_quote_index = index - 2
    if previous_quote_index >= 0 and parsed_units[previous_quote_index].get("source_kind") == "quoted_text":
        previous_quote = parsed_units[previous_quote_index]
        tail = str(previous_quote.get("source_text") or "")[-80:]
        addressed_roles = []
        for candidate_role, candidate_spec in role_specs.items():
            if candidate_role == "Narrator":
                continue
            candidate_keywords = [
                candidate_role,
                str(candidate_spec.get("label") or ""),
                *(candidate_spec.get("attribution_keywords", []) or []),
            ]
            if any(
                re.search(rf"\b{re.escape(str(keyword))}\b", tail, re.I)
                for keyword in candidate_keywords
                if str(keyword).strip()
            ):
                addressed_roles.append(candidate_role)
        if (
            addressed_roles == [role]
            and previous_quote.get("speaker") != role
            and named_speaker_has_source_evidence(parsed_units, previous_quote_index, role_specs, visited)
        ):
            return True
    if index > 0:
        previous = parsed_units[index - 1]
        if previous.get("source_kind") == "quoted_text" and previous.get("speaker") == role:
            return named_speaker_has_source_evidence(parsed_units, index - 1, role_specs, visited)
    if index > 1:
        bridge = parsed_units[index - 1]
        previous_quote = parsed_units[index - 2]
        if (
            bridge.get("source_kind") == "narrative_text"
            and previous_quote.get("source_kind") == "quoted_text"
            and previous_quote.get("speaker") == role
        ):
            return named_speaker_has_source_evidence(parsed_units, index - 2, role_specs, visited)
    previous_quote_index = next(
        (candidate for candidate in range(index - 1, max(-1, index - 7), -1)
         if parsed_units[candidate].get("source_kind") == "quoted_text"
         and not same_quote_turn(unit, parsed_units[candidate])),
        None,
    )
    next_quote_index = next(
        (candidate for candidate in range(index + 1, min(len(parsed_units), index + 7))
         if parsed_units[candidate].get("source_kind") == "quoted_text"
         and not same_quote_turn(unit, parsed_units[candidate])),
        None,
    )
    if previous_quote_index is not None and next_quote_index is not None:
        previous_role = parsed_units[previous_quote_index].get("speaker")
        next_role = parsed_units[next_quote_index].get("speaker")
        nearby_context = " ".join(
            str(parsed_units[candidate].get("source_text") or "")
            for candidate in range(max(0, previous_quote_index - 3), min(len(parsed_units), next_quote_index + 5))
            if not paragraph_id
            or not parsed_units[candidate].get("paragraph_id")
            or parsed_units[candidate].get("paragraph_id") == paragraph_id
        )
        if (
            previous_role == next_role
            and previous_role not in {None, "Narrator", role}
            and any(pattern.search(nearby_context) for pattern in patterns)
            and named_speaker_has_source_evidence(parsed_units, previous_quote_index, role_specs, visited)
            and named_speaker_has_source_evidence(parsed_units, next_quote_index, role_specs, visited)
        ):
            return True
    recent_verified_roles: list[str] = []
    nearest_quote_role = None
    for prior_index in range(index - 1, max(-1, index - 9), -1):
        prior = parsed_units[prior_index]
        if paragraph_id and prior.get("paragraph_id") and prior.get("paragraph_id") != paragraph_id:
            break
        if prior.get("source_kind") != "quoted_text" or prior.get("speaker") in {None, "Narrator"}:
            continue
        prior_role = str(prior.get("speaker"))
        if nearest_quote_role is None:
            nearest_quote_role = prior_role
        if named_speaker_has_source_evidence(parsed_units, prior_index, role_specs, visited):
            recent_verified_roles.append(prior_role)
    verified_set = set(recent_verified_roles)
    if nearest_quote_role and nearest_quote_role != role and verified_set == {nearest_quote_role, role}:
        return True
    return False


def evaluate(section_dir: Path) -> dict:
    source_units = read_json(section_dir / "02_source_units.json")
    scene_parse_path = section_dir / "03_scene_parse.json"
    scene_parse = read_json(scene_parse_path) if scene_parse_path.exists() else {"parsed_source_units": []}
    workflow_config_path = section_dir / "00_workflow_config.json"
    workflow_config = read_json(workflow_config_path) if workflow_config_path.exists() else {"roles": {}}
    registry = read_json(section_dir / "04_voice_registry.json")
    roles = {item["role"]: item for item in registry.get("voices", [])}
    requests = [read_json(path) for path in sorted((section_dir / "06_generation_requests").glob("chunk_*.json"))]
    expected_ids = [item["source_unit_id"] for item in source_units]
    actual_ids = [unit_id for request in requests for unit_id in request.get("source_unit_ids", [])]
    section_failures: list[str] = []
    if actual_ids != expected_ids:
        section_failures.append("source_unit_coverage_or_order_mismatch")
    parsed_units = scene_parse.get("parsed_source_units", [])
    unsupported_attributions = [
        unit.get("source_unit_id")
        for index, unit in enumerate(parsed_units)
        if unit.get("source_kind") == "quoted_text"
        and unit.get("speaker") != "Narrator"
        and (
            unit.get("speaker_confidence") != "high"
            or not named_speaker_has_source_evidence(parsed_units, index, workflow_config.get("roles", {}))
        )
    ]
    if unsupported_attributions:
        section_failures.append("unsupported_specific_speaker_attribution")

    parsed_by_id = {item.get("source_unit_id"): item for item in parsed_units}
    dialogue_opening_failures: set[str] = set()
    for request_index in range(1, len(requests)):
        previous_ids = requests[request_index - 1].get("source_unit_ids", [])
        current_ids = requests[request_index].get("source_unit_ids", [])
        if len(previous_ids) <= 1 or not current_ids:
            continue
        previous_last = parsed_by_id.get(previous_ids[-1], {})
        current_first = parsed_by_id.get(current_ids[0], {})
        previous_text = str(previous_last.get("adapted_text") or previous_last.get("source_text") or "")
        if (
            previous_last.get("speaker") == "Narrator"
            and current_first.get("speaker") not in {None, "Narrator"}
            and not is_dialogue_attribution_text(previous_text)
        ):
            dialogue_opening_failures.add(str(requests[request_index].get("chunk_id")))

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
        warnings: list[str] = []
        if len(prompt) > MAX_PROMPT_CHARS:
            failures.append("prompt_exceeds_provider_limit")
        elif len(prompt) > BASE_PROMPT_BUDGET:
            warnings.append("reduced_repair_reserve")
        if len(active_roles) > 3:
            failures.append("more_than_three_active_roles")
        if conflicts:
            failures.append("active_roles_share_provider_speaker")
        if missing_speakers:
            failures.append("active_role_missing_provider_speaker")
        if str(request.get("chunk_id")) in dialogue_opening_failures:
            failures.append("dialogue_opening_missing_narrative_setup")
        if incomplete_quotes:
            failures.append("incomplete_narrator_line")
        required_narration = min(3, int(metrics.get("narrator_unit_count", 0)))
        if metrics.get("dialogue_unit_count", 0) == 0 and required_narration >= 3 and len(quotes) < required_narration:
            failures.append("insufficient_narration_coverage")
        if metrics.get("unbound_quoted_dialogue_line_count", 0) > 0:
            failures.append("unbound_quoted_dialogue")
        if metrics.get("must_keep_dialogue_count", 0) > metrics.get("must_keep_dialogue_present_count", 0):
            failures.append("missing_must_keep_dialogue")
        if not re.search(r"\b(?:music|score|cello|strings|piano|drone|choir|orchestra)\b", prompt, re.I):
            failures.append("missing_natural_music_description")
        expected = request.get("expected_duration_sec", {})
        ceiling = metrics.get("audio_drama_estimated_duration_ceiling_sec")
        if ceiling and float(ceiling) > SAFE_DURATION_CEILING_SEC:
            failures.append("estimated_duration_exceeds_safe_delivery_window")
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
        "unsupported_attribution_unit_ids": unsupported_attributions,
        "failed_chunk_ids": failed_chunks,
        "policy": {
            "base_prompt_budget": BASE_PROMPT_BUDGET,
            "provider_prompt_limit": MAX_PROMPT_CHARS,
            "repair_reserve_chars": MAX_PROMPT_CHARS - BASE_PROMPT_BUDGET,
            "safe_duration_ceiling_sec": SAFE_DURATION_CEILING_SEC,
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
