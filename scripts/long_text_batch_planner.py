#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "outputs" / "long_runs"

CHAPTER_HEADING_RE = re.compile(
    r"^\s*(chapter\s+\d+|chapter\s+[ivxlcdm]+|book\s+\d+|part\s+\d+|第[一二三四五六七八九十百千\d]+[章节回卷部].*)\s*$",
    re.I,
)
SCENE_BREAK_RE = re.compile(r"^\s*(\*\s*){3,}$|^\s*[-=_]{3,}\s*$")
QUOTE_RE = re.compile(r'"([^"\n]{1,500})"|“([^”\n]{1,500})”')
SENTENCE_RE = re.compile(r"[^。！？!?\.]+[。！？!?\.]?")


def safe_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_").lower()
    return slug[:80] or "long_text"


def read_source(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def clean_source(raw: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("{\\rtf"):
        notes.append("detected_rtf_like_input")
        text = re.sub(r"\\'[0-9a-fA-F]{2}", "", text)
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
        text = text.replace("{", "").replace("}", "")
    before = text
    text = re.sub(r"(?m)^[ \t]*Page\s+\d+[ \t]*$", "", text)
    text = re.sub(r"(?m)^[ \t]*\d+[ \t]*$", "", text)
    if text != before:
        notes.append("removed_page_number_like_lines")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = merge_soft_wraps(text)
    return text.strip() + "\n", notes


def merge_soft_wraps(text: str) -> str:
    lines = text.split("\n")
    merged: list[str] = []
    buffer = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if buffer:
                merged.append(buffer.strip())
                buffer = ""
            merged.append("")
            continue
        if CHAPTER_HEADING_RE.match(stripped) or SCENE_BREAK_RE.match(stripped):
            if buffer:
                merged.append(buffer.strip())
                buffer = ""
            merged.append(stripped)
            continue
        if not buffer:
            buffer = stripped
            continue
        if re.search(r"[。！？!?.:：;；”\"]$", buffer):
            merged.append(buffer.strip())
            buffer = stripped
        else:
            buffer = f"{buffer} {stripped}"
    if buffer:
        merged.append(buffer.strip())
    return "\n".join(merged)


def paragraph_units(text: str) -> list[dict]:
    units = []
    current_heading = ""
    for raw in text.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) == 1 and CHAPTER_HEADING_RE.match(lines[0]):
            current_heading = lines[0]
            units.append({"type": "heading", "text": lines[0], "heading": current_heading})
            continue
        if len(lines) == 1 and SCENE_BREAK_RE.match(lines[0]):
            units.append({"type": "scene_break", "text": lines[0], "heading": current_heading})
            continue
        units.append({"type": "paragraph", "text": "\n".join(lines), "heading": current_heading})
    return units


def split_oversized_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        hard_end = min(start + limit, text_len)
        if hard_end >= text_len:
            pieces.append(text[start:].strip())
            break

        window = text[start:hard_end]
        split_at = None
        for match in re.finditer(r"[。！？!?\.][\"”']?\s+", window):
            split_at = start + match.end()
        if split_at is None or split_at <= start:
            space = window.rfind(" ")
            if space > max(80, limit // 3):
                split_at = start + space + 1
        if split_at is None or split_at <= start:
            split_at = hard_end

        pieces.append(text[start:split_at].strip())
        start = split_at
    return pieces


def split_by_clause(text: str, limit: int) -> list[str]:
    clauses = [item.strip() for item in re.split(r"(?<=[,，;；:：])\s*", text) if item.strip()]
    pieces: list[str] = []
    current = ""
    for clause in clauses:
        if len(clause) > limit:
            if current:
                pieces.append(current.strip())
                current = ""
            for index in range(0, len(clause), limit):
                pieces.append(clause[index : index + limit].strip())
            continue
        candidate = f"{current} {clause}".strip() if current else clause
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                pieces.append(current.strip())
            current = clause
    if current:
        pieces.append(current.strip())
    return pieces


def detect_characters(text: str) -> list[str]:
    names: dict[str, int] = {}
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s*:", text):
        names[match.group(1)] = names.get(match.group(1), 0) + 3
    for match in re.finditer(r"\b(?:said|asked|replied|cried|whispered|murmured)\s+([A-Z][a-z]+)\b", text):
        names[match.group(1)] = names.get(match.group(1), 0) + 2
    for match in re.finditer(r"\b([A-Z][a-z]+)\s+(?:said|asked|replied|cried|whispered|murmured)\b", text):
        names[match.group(1)] = names.get(match.group(1), 0) + 2
    common_false = {"The", "A", "An", "I", "He", "She", "It", "They", "Chapter", "Book", "Part"}
    return [name for name, _score in sorted(names.items(), key=lambda item: (-item[1], item[0])) if name not in common_false][:8]


def dialogue_map(text: str, characters: list[str]) -> list[dict]:
    mapping = []
    for idx, match in enumerate(QUOTE_RE.finditer(text), start=1):
        quote = match.group(1) or match.group(2) or ""
        window_start = max(0, match.start() - 140)
        window_end = min(len(text), match.end() + 140)
        context = text[window_start:window_end]
        speaker = None
        for character in characters:
            if re.search(rf"\b{re.escape(character)}\b", context):
                speaker = character
                break
        mapping.append(
            {
                "quote_index": idx,
                "quote_preview": quote[:120],
                "speaker": speaker or "Narrator",
                "confidence": "heuristic" if speaker else "fallback_narrator",
            }
        )
    return mapping


def build_chapters(text: str, target_chars: int, max_chars: int) -> list[dict]:
    units = paragraph_units(text)
    chapters: list[dict] = []
    current: list[str] = []
    current_heading = ""

    def flush(reason: str) -> None:
        nonlocal current
        chapter_text = "\n\n".join(item for item in current if item.strip()).strip()
        if not chapter_text:
            current = []
            return
        for part in split_oversized_text(chapter_text, max_chars):
            chapters.append(make_chapter_entry(len(chapters) + 1, part, current_heading, reason))
        current = []

    for unit in units:
        unit_type = unit["type"]
        unit_text = unit["text"]
        if unit_type == "heading":
            flush("chapter_heading")
            current_heading = unit_text
            continue
        if unit_type == "scene_break":
            flush("scene_break")
            continue
        candidate = "\n\n".join(current + [unit_text]).strip()
        if current and len(candidate) > target_chars:
            flush("target_length")
        current.append(unit_text)
    flush("end_of_source")

    for index, chapter in enumerate(chapters, start=1):
        chapter["index"] = index
        chapter["chapter_id"] = f"chapter_{index:04d}"
        chapter["chapter_file"] = f"chapters/chapter_{index:04d}.txt"
    return chapters


def make_chapter_entry(index: int, text: str, heading: str, split_reason: str) -> dict:
    characters = detect_characters(text)
    return {
        "index": index,
        "chapter_id": f"chapter_{index:04d}",
        "title": heading or f"Scene {index:04d}",
        "semantic_summary": text[:220].replace("\n", " "),
        "source_start_hint": text[:100].replace("\n", " "),
        "source_end_hint": text[-100:].replace("\n", " "),
        "chapter_file": f"chapters/chapter_{index:04d}.txt",
        "char_count": len(text),
        "split_reason": split_reason,
        "characters_detected": characters,
        "dialogue_voice_map": dialogue_map(text, characters),
        "source_text_sha_hint": str(abs(hash(text))),
        "_chapter_text": text,
    }


def build_batches(chapters: list[dict], batch_size: int) -> list[dict]:
    batches = []
    for batch_index, start in enumerate(range(0, len(chapters), batch_size), start=1):
        group = chapters[start : start + batch_size]
        batches.append(
            {
                "batch_id": f"batch_{batch_index:04d}",
                "batch_index": batch_index,
                "chapter_ids": [chapter["chapter_id"] for chapter in group],
                "audio_file": f"batch_audio/batch_{batch_index:04d}.wav",
                "status": "planned",
            }
        )
    return batches


def normalize_for_coverage(text: str) -> str:
    return re.sub(r"\s+", "", text)


def coverage_report(clean: str, chapters: list[dict]) -> dict:
    stitched = "\n\n".join(chapter["_chapter_text"].strip() for chapter in chapters if chapter.get("_chapter_text")).strip()
    clean_body = "\n\n".join(
        unit["text"] for unit in paragraph_units(clean) if unit["type"] == "paragraph"
    ).strip()
    return {
        "coverage_ok": normalize_for_coverage(stitched) == normalize_for_coverage(clean_body),
        "clean_body_character_count": len(clean_body),
        "stitched_chapter_character_count": len(stitched),
        "chapter_count": len(chapters),
    }


def preprocessing_report(source_path: Path, raw: str, clean: str, chapters: list[dict], notes: list[str]) -> dict:
    headings = [unit["text"] for unit in paragraph_units(clean) if unit["type"] == "heading"]
    return {
        "source_path": str(source_path),
        "original_character_count": len(raw),
        "cleaned_character_count": len(clean),
        "removed_or_normalized": notes,
        "detected_chapter_headings": headings,
        "chapter_heading_count": len(headings),
        "semantic_chapter_count": len(chapters),
        "quote_count": len(list(QUOTE_RE.finditer(clean))),
        "paragraph_group_count": len([item for item in clean.split("\n\n") if item.strip()]),
        "risky_transformations_skipped": [
            "semantic rewriting",
            "punctuation style normalization",
            "dialogue wording cleanup",
            "translation",
        ],
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan long-form fiction into semantic chapters and batches.")
    parser.add_argument("--source-file", required=True, help="Long source text file.")
    parser.add_argument("--run-id", help="Optional explicit run id.")
    parser.add_argument("--target-chars", type=int, default=900, help="Preferred chapter character count.")
    parser.add_argument("--max-chars", type=int, default=1800, help="Hard chapter character count before fallback splitting.")
    parser.add_argument("--batch-size", type=int, default=20, help="Number of chapters per batch.")
    parser.add_argument("--out-root", default=str(OUTPUT_ROOT), help="Output root for long runs.")
    args = parser.parse_args()

    source_path = Path(args.source_file)
    if not source_path.exists():
        raise SystemExit(f"Source file does not exist: {source_path}")
    if args.max_chars < 300:
        raise SystemExit("--max-chars must be at least 300.")
    if args.target_chars > args.max_chars:
        raise SystemExit("--target-chars must be <= --max-chars.")

    raw = read_source(source_path)
    clean, notes = clean_source(raw)
    chapters = build_chapters(clean, target_chars=args.target_chars, max_chars=args.max_chars)
    batches = build_batches(chapters, args.batch_size)
    coverage = coverage_report(clean, chapters)

    run_id = args.run_id or f"{safe_slug(source_path.stem)}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = Path(args.out_root) / run_id
    if run_dir.exists():
        raise SystemExit(f"Run directory already exists: {run_dir}")

    write_text(run_dir / "source_clean.txt", clean)
    report = preprocessing_report(source_path, raw, clean, chapters, notes)
    report["coverage"] = coverage
    write_json(run_dir / "preprocessing_report.json", report)
    public_chapters = [{key: value for key, value in chapter.items() if not key.startswith("_")} for chapter in chapters]
    write_json(run_dir / "chapter_plan.json", {"chapters": public_chapters})
    write_json(run_dir / "batch_plan.json", {"batch_size": args.batch_size, "batches": batches})
    for chapter in chapters:
        write_text(run_dir / chapter["chapter_file"], chapter["_chapter_text"].strip() + "\n")
    manifest = {
        "run_id": run_id,
        "status": "planned",
        "source_file": str(source_path),
        "chapter_count": len(chapters),
        "batch_count": len(batches),
        "coverage": coverage,
        "next_step": "Use scripts/audio_drama_skill.py run for each planned chapter; see README.md for the supported command.",
        "outputs": {
            "source_clean": "source_clean.txt",
            "preprocessing_report": "preprocessing_report.json",
            "chapter_plan": "chapter_plan.json",
            "batch_plan": "batch_plan.json",
        },
    }
    write_json(run_dir / "manifest.json", manifest)
    print(json.dumps({"run_dir": str(run_dir), "chapters": len(chapters), "batches": len(batches)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
