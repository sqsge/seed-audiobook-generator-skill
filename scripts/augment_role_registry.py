#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SPEAKER_FALLBACKS = ["en_male_ronald_uranus_bigtts", "en_male_hades_uranus_bigtts"]


def key_for(role: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", role.lower()).strip("_")


def main() -> int:
    parser = argparse.ArgumentParser(description="Add discovered speaking roles to a chapter voice registry and section configs.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--role", action="append", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    discovered = {item["role"]: item for item in json.loads(Path(args.discovery).read_text(encoding="utf-8"))["roles"]}
    registry_path = run_dir / "voice_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    added = []
    for index, role in enumerate(args.role):
        if role not in discovered or role in registry.get("roles", {}):
            continue
        item = discovered[role]
        key = key_for(role)
        speaker = SPEAKER_FALLBACKS[index % len(SPEAKER_FALLBACKS)]
        evidence = item.get("evidence", [f"{role} speaks in the chapter"])[0]
        spec = {
            "key": key,
            "label": role,
            "reference_mode": "speaker",
            "default_speaker": speaker,
            "description": item.get("description", "supporting English character voice"),
            "attribution_keywords": [role, role.split()[0], role.split()[-1]],
            "reference_prompt": evidence,
        }
        registry.setdefault("roles", {})[role] = spec
        registry.setdefault("reference_order", []).append(role)
        registry.setdefault("voices", []).append(
            {
                "role": role,
                "key": key,
                "speaker_env": f"SEED_AUDIO_SPEAKER_{key.upper()}",
                "speaker": speaker,
                "speaker_source": "chapter_role_discovery",
                "reference_mode": "speaker",
                "fallback_reference": f"00_voice_references/{key}.wav",
                "description": spec["description"],
            }
        )
        added.append(role)
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for config_path in sorted((run_dir / "inputs").glob("section_*/story_config.json")):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for role in added:
            config.setdefault("roles", {})[role] = registry["roles"][role]
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"added_roles": added, "section_configs_updated": len(list((run_dir / "inputs").glob("section_*/story_config.json")))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
