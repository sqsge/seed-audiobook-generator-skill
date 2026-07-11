#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import llm_chat


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover speaking roles for one complete fiction chapter.")
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--known-registry")
    args = parser.parse_args()

    source = Path(args.source_file).read_text(encoding="utf-8")
    known = {}
    if args.known_registry:
        registry = json.loads(Path(args.known_registry).read_text(encoding="utf-8"))
        known = {item["role"]: item.get("description", "") for item in registry.get("voices", [])}
    prompt = f"""Identify every character who audibly speaks in this English fiction chapter.
Return compact JSON only. Do not include characters who are only mentioned or make nonverbal sounds.
Resolve dialogue attribution from nearby prose, including attribution after a quote.
Keep known role names unchanged. Add missing named speakers such as students or minor characters when the text proves they speak.

Known roles:
{json.dumps(known, ensure_ascii=False, separators=(',', ':'))}

Output schema:
{{"roles":[{{"role":"canonical character name","description":"gender, approximate age, voice color, emotional baseline","evidence":["one short exact dialogue excerpt"],"aliases":[]}}]}}

Chapter:
{source}
"""
    response = llm_chat.chat_text(
        prompt,
        system="You are a precise literary dialogue-attribution analyst. Return valid JSON only.",
        model="seed-2-0-pro-260328",
        temperature=0.0,
        max_tokens=5000,
    )
    payload = json.loads(response)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "role_count": len(payload.get("roles", []))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
