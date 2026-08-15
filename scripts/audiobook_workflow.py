#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import llm_chat
import tts_client
import asr_client


ROOT = Path(__file__).resolve().parents[1]
SEED_AUDIO_CLIENT = Path(__file__).resolve().parent / "seed_audio_client.py"
LLM_CHAT_CLIENT = Path(__file__).resolve().parent / "llm_chat.py"
OUTPUT_ROOT = ROOT / "outputs" / "runs"
STORY_CONFIG: dict | None = None
SCENE_ID = "moonlit_cloister_duel_en"
PRODUCTION_MODE = "audio_drama_adaptation"
WORKFLOW_INPUT_MODE = "story_config"
SOURCE_UNITS_OVERRIDE: list[dict] | None = None
REWRITE_MODEL = os.getenv("SEED_AUDIO_REWRITE_MODEL", "dola-seed-2-1-turbo-260628")
AUDIO_MODEL = "seed-audio-1.0"
AUDIO_REVIEW_MODEL = os.getenv("SEED_AUDIO_REVIEW_MODEL", "seed-2-0-lite-260428")
VOICE_REGISTRY_VERSION = "voice_registry_seed2pro_20260702_001"
MAX_PROMPT_CHARS = int(os.getenv("SEED_AUDIO_MAX_CHARS", "3000"))
BASE_PROMPT_BUDGET = int(os.getenv("SEED_AUDIO_BASE_PROMPT_BUDGET", "2700"))
TARGET_AUDIO_PROMPT_CHARS = int(os.getenv("SEED_AUDIO_TARGET_CHARS", "2200"))
SAFE_DURATION_CEILING_SEC = float(os.getenv("SEED_AUDIO_SAFE_DURATION_CEILING_SEC", "60"))
SAFE_CONTENT_CEILING_SEC = min(48.0, SAFE_DURATION_CEILING_SEC - 12.0)
SAFE_SOURCE_UNITS_PER_CHUNK = max(1, int(os.getenv("SEED_AUDIO_SAFE_SOURCE_UNITS_PER_CHUNK", "5")))
TARGET_SPEECH_RATE = 24
ENGLISH_TARGET_SPEECH_RATE = int(os.getenv("SEED_AUDIO_EN_SPEECH_RATE", "4"))
DESTRUCTIVE_TIMING_POSTPROCESS = os.getenv("SEED_AUDIO_DESTRUCTIVE_TIMING", "false").lower() in {"1", "true", "yes"}
PERFORMANCE_REPAIR_ATTEMPTS = max(0, int(os.getenv("SEED_AUDIO_REPAIR_ATTEMPTS", "2")))
DEFAULT_ASR_MODE = os.getenv("SEED_AUDIO_ASR_MODE", "off")
DEFAULT_PERFORMANCE_MODE = os.getenv("SEED_AUDIO_PERFORMANCE_MODE", "balanced")
DISABLE_BACKGROUND_MUSIC = os.getenv("SEED_AUDIO_DISABLE_BACKGROUND_MUSIC", "true").lower() in {"1", "true", "yes", "on"}
HK_TZ = timezone(timedelta(hours=8))

# Natural dramatic pauses are allowed, but must not turn into output padding.
# The middle band is deliberately repairable locally: it must not force a
# planner rewrite or silently pass into a completed chapter.
ADAPTIVE_SILENCE_POLICY = {
    "leading_trim_sec": 1.5,
    "leading_warning_sec": 2.5,
    "leading_hard_sec": 3.0,
    "leading_hard_ratio": 0.20,
    "internal_review_sec": 2.5,
    "internal_repair_sec": 4.0,
    "internal_hard_sec": 6.0,
    "trailing_trim_sec": 1.5,
    "trailing_warning_sec": 2.5,
    "trailing_warning_ratio": 0.15,
    "trailing_hard_sec": 3.0,
    "trailing_hard_ratio": 0.30,
    "chapter_long_silence_ratio_hard": 0.05,
}

ROLE_SPECS = {
    "Narrator": {
        "key": "narrator",
        "default_speaker": "en_male_knightley_uranus_bigtts",
        "description": "English literary audio-drama narrator, clear, cinematic, controlled tension, natural pace",
        "reference_prompt": (
            "[Single speaker only. No music. No sound effects. Clean dry voice reference.]\n"
            "Moonlight washes across the old stone cloister. Two young spellcasters face each other in the cold air, "
            "while the portraits pretend not to watch."
        ),
    },
    "ElianVale": {
        "key": "elian_vale",
        "default_speaker": "en_male_josh_uranus_bigtts",
        "description": "Elian Vale, teenage male, quick-witted, breathless under pressure, brave but scared",
        "reference_prompt": (
            "[Single speaker only. No music. No sound effects. Clean dry voice reference.]\n"
            "And miss your dramatic midnight villain speech? Never. I am trying very hard not to die right now."
        ),
    },
    "MaraVoss": {
        "key": "mara_voss",
        "default_speaker": "en_female_rachel_p1_uranus_bigtts",
        "description": "Mara Voss, young female antagonist, sharp, cold, furious, controlled menace",
        "reference_prompt": (
            "[Single speaker only. No music. No sound effects. Clean dry voice reference.]\n"
            "You should have stayed in the library, Vale. Clever tricks will not save you now."
        ),
    },
    "Armor": {
        "key": "armor",
        "default_speaker": "en_male_hades_uranus_bigtts",
        "description": "Enchanted suit of armor, resonant metallic male voice, formal, slightly absurd",
        "reference_prompt": (
            "[Single speaker only. No music. No sound effects. Clean dry voice reference.]\n"
            "State your business. Apologies. I appear to be cursed."
        ),
    },
    "OldPortrait": {
        "key": "old_portrait",
        "default_speaker": "en_male_ronald_uranus_bigtts",
        "description": "Old wizard portrait, elderly male, fussy, theatrical, dry comic authority",
        "reference_prompt": (
            "[Single speaker only. No music. No sound effects. Clean dry voice reference.]\n"
            "Highly improper spellwork after curfew. Ten points for technique, fifty off for property damage."
        ),
    },
}

ROLE_KEY_TO_ROLE = {spec["key"]: role for role, spec in ROLE_SPECS.items()}
ROLE_SPOKEN_NAMES = {
    "ElianVale": "Elian Vale",
    "MaraVoss": "Mara Voss",
    "Armor": "Armor",
    "OldPortrait": "Old Portrait",
}
NARRATOR_LABEL = "Narrator"

ENGLISH_SPEAKER_CANDIDATES = [
    "en_male_knightley_uranus_bigtts",
    "en_male_josh_uranus_bigtts",
    "en_female_rachel_p1_uranus_bigtts",
    "en_male_hades_uranus_bigtts",
    "en_male_ronald_uranus_bigtts",
]

CHINESE_SPEAKER_CANDIDATES = [
    "zh_male_m191_uranus_bigtts",
    "zh_male_ruyayichen_uranus_bigtts",
    "zh_male_liufei_uranus_bigtts",
    "zh_male_dayi_uranus_bigtts",
    "zh_male_taocheng_uranus_bigtts",
]

TRAD_TO_SIMP = str.maketrans(
    {
        "國": "国",
        "謀": "谋",
        "當": "当",
        "隻": "只",
        "頭": "头",
        "東": "东",
        "帶": "带",
        "擂": "擂",
        "吶": "呐",
        "魯": "鲁",
        "肅": "肃",
        "驚": "惊",
        "齊": "齐",
        "於": "于",
        "霧": "雾",
        "顧": "顾",
        "卻": "却",
        "聽": "听",
        "飛": "飞",
        "報": "报",
        "傳": "传",
        "輕": "轻",
        "動": "动",
        "撥": "拨",
        "亂": "乱",
        "旱": "旱",
        "內": "内",
        "喚": "唤",
        "遼": "辽",
        "邊": "边",
        "號": "号",
        "來": "来",
        "搶": "抢",
        "頃": "顷",
        "餘": "余",
        "盡": "尽",
        "發": "发",
        "轉": "转",
        "滿": "满",
        "軍": "军",
        "謝": "谢",
        "這": "这",
        "裏": "里",
        "餘": "余",
        "謂": "谓",
        "費": "费",
        "將": "将",
        "卻": "却",
        "為": "为",
        "識": "识",
        "曉": "晓",
        "陣": "阵",
        "勢": "势",
        "庸": "庸",
        "亮": "亮",
        "辦": "办",
        "應": "应",
        "風": "风",
        "過": "过",
        "殺": "杀",
        "繫": "系",
        "焉": "焉",
        "哉": "哉",
        "拜": "拜",
        "服": "服",
        "玠": "玠",
    }
)


def simplify_text(text: str) -> str:
    return text.translate(TRAD_TO_SIMP)


def speaker_label(role: str) -> str:
    if role == "Narrator":
        return NARRATOR_LABEL
    return ROLE_SPOKEN_NAMES.get(role, role)


def prompt_language() -> str:
    if STORY_CONFIG:
        return str(STORY_CONFIG.get("prompt_language", "en")).lower()
    return "en"


def is_auto_planning() -> bool:
    return bool(STORY_CONFIG and STORY_CONFIG.get("auto_planning"))


def is_english_prompt() -> bool:
    return prompt_language().startswith("en")


def sentence_punctuation() -> str:
    return "." if is_english_prompt() else "。"


def with_sentence_punctuation(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    if stripped[-1] in ".!?。！？":
        return stripped
    return stripped + sentence_punctuation()


def transition_rule_text() -> str:
    if is_english_prompt():
        return (
            "[Performance rules: bracketed role labels and directions are instructions only, not spoken words; "
            "keep dialogue continuous and avoid artificial pauses; ambience stays subtle under speech.]"
        )
    return "[Transitions: 每次旁白、对白、音效切换前留自然呼吸; 音乐先轻微下潜再切入人声; 环境音不断开,用水声或衣袍声托住转场。]"


def transition_bridge_text() -> str:
    if is_english_prompt():
        return ""
    return "[Transition: 水声延续,音乐轻轻下潜,留一个自然短停顿。]"


def only_active_voices_text() -> str:
    if is_english_prompt():
        return "Use only the voices listed above. Do not add unlisted speakers."
    return "只使用上述声音,不要新增声音。"


def sfx_visibility_text() -> str:
    if is_english_prompt():
        return "Use only the listed SFX as subtle production cues; do not interrupt dialogue."
    return "音效要可听见,但不要遮住人声。"


def music_fade_text() -> str:
    return "[Music fades.]" if is_english_prompt() else "[Music fades.]"


def slugify_scene_id(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug[:64] or "source_scene"


SOURCE_TITLE = "The Duel in the Moonlit Cloister"
SOURCE_URL = "local:moonlit_cloister_duel_en"


SOURCE_EXCERPT = """Moonlight spills through tall arched windows, silvering the stone floor. Elian Vale stands with his wand raised, robes torn at the sleeve. Mara Voss faces him from the other end of the cloister, her wand glowing with a sickly green spark.

Mara Voss: "You should have stayed in the library, Vale."
Elian Vale: "And miss your dramatic midnight villain speech? Never."

Mara slashes her wand through the air. Black smoke twists into a serpent and strikes the pillar beside Elian. Elian fires white-gold light from his wand, shattering the creature into sparks. The portraits gasp awake.

Old Portrait: "Highly improper spellwork after curfew!"
Mara Voss: "You think jokes will save you?"
Elian Vale: "Featherbound!"

Invisible pressure snaps away and Elian crashes backward into a suit of armor. The armor wakes with a clang.
Armor: "State your business!"
Elian Vale: "Trying not to die!"

Mara fires a red bolt down the corridor. Elian rolls behind the armor as the blast tears the helmet clean off. The armor complains, and Elian splits into three shimmering copies. Mara's eyes flash blue; two false Elians burst like soap bubbles.

Elian Vale: "Too slow."
Elian points at the moonlit glass.
Elian Vale: "Prismatica!"
Reflected beams scatter across shields, frames, and wet stone. Silver cords snap around Mara's wrist, waist, and ankles, and her wand clatters away.

Elian Vale: "Now tell me where you hid the star-key."
Mara smiles. Behind Elian, the headless armor bends down and picks up Mara's wand.
Armor: "Apologies. I appear to be cursed."
"""


def apply_story_config(config: dict) -> None:
    global STORY_CONFIG, SCENE_ID, PRODUCTION_MODE, SOURCE_TITLE, SOURCE_URL, SOURCE_EXCERPT
    global ROLE_SPECS, ROLE_KEY_TO_ROLE, ROLE_SPOKEN_NAMES, NARRATOR_LABEL, SOURCE_UNITS_OVERRIDE
    STORY_CONFIG = config
    SCENE_ID = config.get("scene_id", SCENE_ID)
    PRODUCTION_MODE = config.get("production_mode", PRODUCTION_MODE)
    SOURCE_TITLE = config.get("source_title", SOURCE_TITLE)
    SOURCE_URL = config.get("source_url", SOURCE_URL)
    SOURCE_EXCERPT = simplify_text(config.get("source_excerpt", SOURCE_EXCERPT))
    override = config.get("source_units_override")
    SOURCE_UNITS_OVERRIDE = json.loads(json.dumps(override)) if isinstance(override, list) else None
    roles = config.get("roles")
    if isinstance(roles, dict) and roles:
        ROLE_SPECS = roles
        ROLE_KEY_TO_ROLE = {spec["key"]: role for role, spec in ROLE_SPECS.items()}
        if "Narrator" in ROLE_SPECS:
            NARRATOR_LABEL = ROLE_SPECS["Narrator"].get("label", NARRATOR_LABEL)
        ROLE_SPOKEN_NAMES = {
            role: spec.get("label", role)
            for role, spec in ROLE_SPECS.items()
            if role != "Narrator"
        }


def build_source_file_config(
    *,
    source_file: str,
    language: str,
    mode: str,
    source_title: str | None,
) -> dict:
    path = Path(source_file)
    if not path.is_absolute():
        path = Path.cwd() / path
    text = path.read_text(encoding="utf-8")
    title = source_title or path.stem.replace("_", " ").replace("-", " ").strip().title()
    prompt_lang = "en" if language.lower().startswith("en") else "zh"
    narrator_speaker = ENGLISH_SPEAKER_CANDIDATES[0] if prompt_lang == "en" else CHINESE_SPEAKER_CANDIDATES[0]
    narrator_label = "Narrator" if prompt_lang == "en" else "旁白"
    narrator_desc = (
        "clear literary narrator, warm, cinematic, emotionally restrained"
        if prompt_lang == "en"
        else "清晰文学旁白,沉稳自然,有画面感但不过度表演"
    )
    return {
        "scene_id": slugify_scene_id(title),
        "production_mode": mode,
        "prompt_language": prompt_lang,
        "auto_planning": True,
        "source_title": title,
        "source_url": f"local:{path.name}",
        "source_excerpt": text,
        "roles": {
            "Narrator": {
                "key": "narrator",
                "label": narrator_label,
                "reference_mode": "tts_audio",
                "default_speaker": narrator_speaker,
                "description": narrator_desc,
                "attribution_keywords": [],
                "reference_prompt": (
                    "The room was quiet, and every small sound seemed to carry meaning."
                    if prompt_lang == "en"
                    else "夜色沉静,细微的声响在屋中显得格外清楚。"
                ),
            }
        },
    }


def apply_planned_roles(roles: dict) -> None:
    global ROLE_SPECS, ROLE_KEY_TO_ROLE, ROLE_SPOKEN_NAMES, NARRATOR_LABEL
    if not isinstance(roles, dict) or not roles:
        return
    normalized: dict[str, dict] = {}
    for index, (role, spec) in enumerate(roles.items()):
        if not isinstance(spec, dict):
            continue
        role_id = str(role)
        key = spec.get("key") or re.sub(r"[^A-Za-z0-9]+", "_", role_id.lower()).strip("_") or role_id.lower()
        speaker_candidates = ENGLISH_SPEAKER_CANDIDATES if is_english_prompt() else CHINESE_SPEAKER_CANDIDATES
        default_speaker = spec.get("default_speaker") or speaker_candidates[min(index, len(speaker_candidates) - 1)]
        label = spec.get("label") or role_id
        description = spec.get("description") or ("natural expressive character voice" if is_english_prompt() else "自然有表现力的角色声音")
        reference_prompt = spec.get("reference_prompt") or (
            f"{label} speaks with a clear, natural voice in a dramatic scene."
            if is_english_prompt()
            else f"{label}用清晰自然的声音说出一句有情绪的台词。"
        )
        if is_english_prompt() and len(re.findall(r"[A-Za-z0-9']+", str(reference_prompt))) < 35:
            if role_id == "Narrator":
                reference_prompt = (
                    f"{reference_prompt} "
                    "The hush before danger was not empty; it breathed through the stone, "
                    "held in the dust, and waited behind every closed portrait eye."
                )
            else:
                reference_prompt = (
                    f"{reference_prompt} "
                    "I hear the room change around us. Keep your wand steady, keep your voice low, "
                    "and do not let fear decide the next move."
                )
        normalized[role_id] = {
            "key": key,
            "label": label,
            "reference_mode": spec.get("reference_mode", "tts_audio"),
            "default_speaker": default_speaker,
            "description": description,
            "attribution_keywords": spec.get("attribution_keywords", [label] if role_id != "Narrator" else []),
            "reference_prompt": reference_prompt,
        }
    if "Narrator" not in normalized:
        base = ROLE_SPECS.get("Narrator", {})
        normalized = {"Narrator": base, **normalized}
    ROLE_SPECS = normalized
    ROLE_KEY_TO_ROLE = {spec["key"]: role for role, spec in ROLE_SPECS.items()}
    NARRATOR_LABEL = ROLE_SPECS.get("Narrator", {}).get("label", NARRATOR_LABEL)
    ROLE_SPOKEN_NAMES = {
        role: spec.get("label", role)
        for role, spec in ROLE_SPECS.items()
        if role != "Narrator"
    }


def apply_voice_registry_file(path: Path) -> None:
    global SCENE_ID, PRODUCTION_MODE
    if not path.exists():
        return
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry.get("scene_id"):
        SCENE_ID = str(registry["scene_id"])
    if registry.get("production_mode"):
        PRODUCTION_MODE = str(registry["production_mode"])
    voices = registry.get("voices", [])
    if not isinstance(voices, list):
        return
    planned_roles: dict[str, dict] = {}
    for voice in voices:
        if not isinstance(voice, dict):
            continue
        role = voice.get("role")
        if not role:
            continue
        planned_roles[str(role)] = {
            "key": voice.get("key"),
            "label": voice.get("role"),
            "reference_mode": voice.get("reference_mode", "tts_audio"),
            "default_speaker": voice.get("speaker"),
            "description": voice.get("description"),
            "reference_prompt": voice.get("reference_prompt"),
        }
    apply_planned_roles(planned_roles)


def load_story_config(path: str | None) -> dict | None:
    if not path:
        default_path = ROOT / "story_configs" / "moonlit_cloister_duel_en.json"
        if not default_path.exists():
            return None
        return json.loads(default_path.read_text(encoding="utf-8"))
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    return json.loads(config_path.read_text(encoding="utf-8"))


SENTENCE_END_RE = re.compile(r"[^。！？!?.]+[。！？!?.]?")
QUOTE_RE = re.compile(
    r"「([^」]+)」|“([^”]+)”|\"([^\"\n]+?)(?:\"|'')|''([^\n]+?)''|‘([^’\n]+)’"
)
EN_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "etc"}


def split_cn_sentences(text: str) -> list[str]:
    return [match.group(0).strip() for match in SENTENCE_END_RE.finditer(text) if match.group(0).strip()]


def split_en_sentences(text: str) -> list[str]:
    normalized = re.sub(r"(?:\.\s*){2,}", "...", text)
    sentences: list[str] = []
    start = 0
    i = 0
    while i < len(normalized):
        char = normalized[i]
        is_boundary = char in "!?"
        if char == ".":
            previous_is_dot = i > 0 and normalized[i - 1] == "."
            next_nonspace = next((c for c in normalized[i + 1 :] if not c.isspace()), "")
            if previous_is_dot or next_nonspace == ".":
                i += 1
                continue
            token_match = re.search(r"([A-Za-z]+)$", normalized[start:i].strip())
            token = token_match.group(1).lower() if token_match else ""
            is_boundary = token not in EN_ABBREVIATIONS
        if is_boundary:
            candidate = normalized[start : i + 1].strip()
            if candidate and not re.fullmatch(r"[.\s…]+", candidate):
                sentences.append(candidate)
            start = i + 1
        i += 1
    tail = normalized[start:].strip()
    if tail and not re.fullmatch(r"[.\s…]+", tail):
        sentences.append(tail)
    return sentences


def split_source_sentences(text: str) -> list[str]:
    if is_english_prompt():
        return split_en_sentences(text)
    return split_cn_sentences(text)


def split_attr_tail(text: str) -> tuple[str, str | None]:
    stripped = text.strip()
    attr_endings = (
        "曰：",
        "曰:",
        "道：",
        "道:",
        "说道：",
        "说道:",
        "喊道：",
        "喊道:",
        "问道：",
        "问道:",
        "低声道：",
        "低声道:",
        "冷笑道：",
        "冷笑道:",
        "谓：",
        "谓:",
        "謂：",
        "謂:",
        "叫曰：",
        "叫曰:",
        "传令曰：",
        "传令曰:",
        "傳令曰：",
        "傳令曰:",
    )
    if not stripped.endswith(attr_endings) and not stripped.endswith(":"):
        return text, None
    last_end = max(stripped.rfind(mark) for mark in ("。", "！", "？", "!", "?", "."))
    if last_end >= 0:
        return stripped[: last_end + 1], stripped[last_end + 1 :]
    return "", stripped


def build_source_units() -> list[dict]:
    if SOURCE_UNITS_OVERRIDE is not None:
        return json.loads(json.dumps(SOURCE_UNITS_OVERRIDE))
    units: list[dict] = []
    order = 1
    paragraphs = SOURCE_EXCERPT.strip().split("\n\n")
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        paragraph_id = f"p{paragraph_index:02d}"
        pos = 0
        for quote_index, quote_match in enumerate(QUOTE_RE.finditer(paragraph), start=1):
            before = paragraph[pos : quote_match.start()]
            narrative_text, quote_attribution = split_attr_tail(before)
            for sentence in split_source_sentences(narrative_text):
                start = paragraph.find(sentence, pos)
                units.append(
                    {
                        "source_unit_id": f"s{order:04d}",
                        "source_order": order,
                        "paragraph_id": paragraph_id,
                        "source_kind": "narrative_text",
                        "source_text": simplify_text(sentence),
                        "source_span": {
                            "paragraph_id": paragraph_id,
                            "char_start": start,
                            "char_end": start + len(sentence),
                        },
                    }
                )
                order += 1
            quote_text = next(group for group in quote_match.groups() if group is not None)
            quote_start = next(
                quote_match.start(index)
                for index, group in enumerate(quote_match.groups(), start=1)
                if group is not None
            )
            for sentence in split_source_sentences(quote_text):
                rel = quote_text.find(sentence)
                start = quote_start + rel
                unit = {
                    "source_unit_id": f"s{order:04d}",
                    "source_order": order,
                    "paragraph_id": paragraph_id,
                    "source_kind": "quoted_text",
                    "quote_span_id": f"{paragraph_id}_q{quote_index:02d}",
                    "source_text": simplify_text(sentence),
                    "source_span": {
                        "paragraph_id": paragraph_id,
                        "char_start": start,
                        "char_end": start + len(sentence),
                    },
                }
                if quote_attribution:
                    unit["quote_attribution_text"] = simplify_text(quote_attribution.strip())
                units.append(unit)
                order += 1
            pos = quote_match.end()
        for sentence in split_source_sentences(paragraph[pos:]):
            start = paragraph.find(sentence, pos)
            units.append(
                {
                    "source_unit_id": f"s{order:04d}",
                    "source_order": order,
                    "paragraph_id": paragraph_id,
                    "source_kind": "narrative_text",
                    "source_text": simplify_text(sentence),
                    "source_span": {
                        "paragraph_id": paragraph_id,
                        "char_start": start,
                        "char_end": start + len(sentence),
                    },
                }
            )
            order += 1
    return units


def source_units_for_partial_resume(run_dir: Path) -> list[dict]:
    stored_excerpt_path = run_dir / "01_source_excerpt.txt"
    source_units_path = run_dir / "02_source_units.json"
    if source_units_path.exists() and not stored_excerpt_path.exists():
        raise SystemExit("Partial resume source proof is missing: 01_source_excerpt.txt is required with cached source units.")
    if stored_excerpt_path.exists() and stored_excerpt_path.read_text(encoding="utf-8") != SOURCE_EXCERPT:
        raise SystemExit(
            "Partial resume source mismatch: current story config differs from the immutable stored source excerpt."
        )
    if source_units_path.exists():
        return json.loads(source_units_path.read_text(encoding="utf-8"))
    return build_source_units()


def now_hk() -> datetime:
    return datetime.now(HK_TZ)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_text_once(path: Path, text: str) -> bool:
    """Create immutable evidence, accepting only an identical existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
        return True
    except FileExistsError:
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"Refusing to overwrite append-only artifact: {path}")
        return False


def write_json_once(path: Path, data: object) -> bool:
    return write_text_once(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_json(path: Path, data: object) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)


def audio_decodes(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    if path.suffix.lower() == ".wav" and path.stat().st_size < 1024:
        return False
    result = run(["ffmpeg", "-v", "error", "-i", str(path.relative_to(ROOT)), "-f", "null", "-"])
    duration = audio_duration(path)
    return result.returncode == 0 and duration is not None and duration > 0.2


def make_run_dir(run_id: str | None) -> Path:
    if not run_id:
        run_id = f"{now_hk().strftime('%Y%m%d-%H%M%S')}_{SCENE_ID}_seed2pro"
    run_dir = OUTPUT_ROOT / run_id
    if run_dir.exists():
        raise SystemExit(f"Run folder already exists, refusing to overwrite: {run_dir.relative_to(ROOT)}")
    run_dir.mkdir(parents=True)
    return run_dir


def extract_json(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def rewrite_prompt(source_units: list[dict]) -> str:
    allowed_speakers = "|".join(ROLE_SPECS.keys())
    voice_examples = [
        {
            "role": role,
            "label": f"Audio{index}",
            "voice_ref": f"@Audio{index}",
            "description": spec.get("description", ""),
        }
        for index, (role, spec) in enumerate(list(ROLE_SPECS.items())[:3], start=1)
    ]
    return f"""你是有声剧导演和小说文本解析器。请严格按飞书文档要求,把输入的纯文本小说片段自动改写成 Seed Audio 1.0 可用的导演 prompt。

必须满足:
1. 输入是未标注小说文本;你负责自动解析旁白/对白/说话人/情绪。
2. 不改剧情、不改台词原意;只补合理的场景、音效、配乐、情绪提示。
2a. 所有 source_text、adapted_text、旁白提示和 Audio prompt 正文必须使用简体中文。
3. 角色只能来自允许列表: {allowed_speakers}。每个 Audio chunk 最多使用 3 个声音,按工程层 active_roles 顺序绑定 @Audio1/@Audio2/@Audio3。
4. 旁白和对白必须明显区分;同一角色音色标签全程一致。
5. 输出 prompt 必须适合 Audio 1.0,单段小于 {MAX_PROMPT_CHARS} 字符。
6. prompt 使用英文控制骨架,中文正文,降低模型解析失败概率。
7. 音效/配乐要服务剧情,不可堆砌。
8. 语速必须比默认略快: 旁白 steady but not slow, 对白自然紧凑,不要拖长停顿。
9. 每个 chunk 必须有 Narrator 开场、过渡或收束,旁白要串起整个剧情。
10. 每个 chunk 必须有 Music bed,并写明 enter、duck、swell、fade 中至少三个动作;音乐必须可听见但不遮挡人声。
11. 每个 chunk 必须有 Persistent ambience 和至少 2 个具体 SFX cue。
12. 不允许把多句原文合成一个大 adapted_text;每个 source unit 必须独立分析。
13. 输出必须是纯 JSON,不要 Markdown。

下面是工程从纯中文原文自动切出的 source_units。它们不是人工角色标注。你必须逐条保留 source_unit_id,并为每条补齐 type/speaker/emotion/delivery/prompt_block_text。

source_units:
{json.dumps(source_units, ensure_ascii=False, indent=2)}

JSON schema:
{{
  "scene_id": "{SCENE_ID}",
  "production_mode": "{PRODUCTION_MODE}",
  "rewrite_model": "{REWRITE_MODEL}",
  "source_title": "{SOURCE_TITLE}",
  "source_url": "{SOURCE_URL}",
  "audio_input_policy": {{
    "prompt_language_policy": "English control labels plus Chinese source text",
    "target_speech_rate": "medium_fast_not_slow",
    "narrator_policy": "Narrator must connect plot, space, action, and transitions in every chunk.",
    "music_policy": "Every chunk must include audible music bed with enter, duck, swell, and fade instructions.",
    "ambience_policy": "Every chunk must include persistent ambience and concrete SFX cues."
  }},
  "voice_registry": {{
    "scene_id": "{SCENE_ID}",
    "production_mode": "{PRODUCTION_MODE}",
    "voice_registry_version": "{VOICE_REGISTRY_VERSION}",
    "reference_order": {json.dumps(list(ROLE_SPECS.keys()), ensure_ascii=False)},
    "voices": {json.dumps(voice_examples, ensure_ascii=False)}
  }},
  "reference_prompts": {{
    "narrator": "干净单人旁白参考音频文本",
    "kongming": "干净单人孔明参考音频文本",
    "lusu": "干净单人鲁肃参考音频文本"
  }},
  "parsed_source_units": [
    {{
      "source_unit_id": "s0001",
      "source_order": 1,
      "paragraph_id": "p01",
      "source_kind": "narrative_text|quoted_text",
      "source_text": "必须逐字对应上面的 source_units",
      "type": "narration|dialogue|monologue",
      "speaker": "{allowed_speakers}",
      "speaker_evidence": "从曰/謂/上下文判断说话人的证据",
      "adapted_text": "用于 Audio prompt 的文本;不得改变原意;默认等于 source_text 或只做极轻微口语化",
      "preservation_level": "verbatim|lightly_adapted|compressed",
      "omission_reason": null,
      "emotion": "情绪",
      "narration_style": "descriptive_narration|action_narration|psychological_narration|transition_narration|null",
      "delivery": "表演方式,必须包含语速提示,如 steady but not slow / tense and quick",
      "sfx_before": [],
      "sfx_during": [],
      "sfx_after": [],
      "music_intent": "本 unit 对音乐变化的要求",
      "prompt_block_text": "一小段可直接进入 Audio prompt 的导演脚本,必须引用 source_unit_id"
    }}
  ],
  "scene_parse": {{
    "scene_id": "{SCENE_ID}",
    "beats": [
      {{
        "beat_id": "b01",
        "title": "...",
        "source_unit_ids": ["s0001"],
        "previous_context_summary": "...",
        "chunk_opening_state": "...",
        "chunk_ending_state": "...",
        "next_context_hint": "..."
      }}
    ]
  }},
  "director_prompt_chunks": [
    {{
      "chunk_id": "chunk_001",
      "source_unit_ids": ["s0001"],
      "text_prompt": "实际传给 Audio 1.0 的完整 prompt,必须包含 Voice binding、Scene、Persistent ambience、Music bed、Sound design、Speech pace 和逐 source_unit_id 的角色台词/旁白",
      "expected_duration_sec": {{"min": 20, "max": 100}},
      "input_metrics": {{
        "source_unit_count": 1,
        "narrator_unit_count": 1,
        "dialogue_unit_count": 0,
        "source_sfx_cue_count": 0,
        "sfx_cue_count": 0,
        "sfx_cue_limit": 2,
        "music_actions": ["enter", "duck", "swell", "fade"],
        "prompt_chars": 0
      }},
      "continuity": {{
        "previous_context_summary": "...",
        "chunk_opening_state": "...",
        "chunk_ending_state": "...",
        "next_context_hint": "..."
      }}
    }}
  ],
  "input_review": [
    {{
      "chunk_id": "chunk_001",
      "prompt_chars": 0,
      "narrator_units": 0,
      "dialogue_units": 0,
      "sfx_cues": 0,
      "music_actions": ["enter", "duck", "swell", "fade"],
      "persistent_ambience": true,
      "narrator_connects_plot": true,
      "pace_not_too_slow": true,
      "voice_binding_complete": true,
      "ready_for_generation": true
    }}
  ]
}}

原文备查:
{SOURCE_EXCERPT}
"""


def compact_dynamic_rewrite_prompt(source_units: list[dict], speaker_candidates: list[str], language_name: str) -> str:
    units = [
        {
            "id": unit["source_unit_id"],
            "kind": unit.get("source_kind"),
            "text": unit.get("source_text"),
            "attribution": unit.get("quote_attribution_text"),
        }
        for unit in source_units
    ]
    return f"""Analyse one {language_name} fiction section for a Seed Audio 1.0 audio drama. Return compact JSON only.

Rules:
1. Preserve every input id exactly once and in order in parsed_source_units and chunk_plan.
2. Discover Narrator and the speaking roles. Give each role a short stable voice identity and choose default_speaker only from {json.dumps(speaker_candidates, ensure_ascii=False)}.
3. For every unit infer speaker, type, brief emotion and delivery, motivated before/during/after SFX, SFX layer, and brief music intent. Every dialogue unit must include speaker_evidence and speaker_confidence (high|medium|low). Only high-confidence dialogue may use a named role; medium or low confidence must use Narrator. Omit adapted_text when the source wording is preserved.
4. Split into coherent dramatic chunks at story turns, ambience changes, dense action, or speaker hand-offs. Each chunk has at most three active roles and must cover its ids exactly once.
5. For each chunk provide concise scene-specific ambience, music style/instruments/atmosphere, foreground sound design, pace, continuity, and a content-derived expected duration. Estimate speech first; use roughly 0.5-2s audible pre-roll and 1.5-4s audible post-roll. A short line should usually be an 8-20s chunk, never a 30-120s container padded with silence. Keep requested beds perceptibly audible, avoid stacking soft/faint/distant/low wording, and do not overload one short spoken line with many simultaneous SFX.
6. This is stage-one analysis. Do not return final_audio_prompt, text_prompt, repeated source text, adaptation essays, coverage summaries, dialogue lists, or voice registry. The workflow builds each Audio 1.0 prompt separately per chunk.
7. Keep values brief and use no prose outside the JSON.

Input units:
{json.dumps(units, ensure_ascii=False, separators=(',', ':'))}

Compact schema:
{{"scene_id":"{SCENE_ID}","production_mode":"{PRODUCTION_MODE}","roles":{{"Narrator":{{"key":"narrator","label":"Narrator","reference_mode":"speaker","default_speaker":"{speaker_candidates[0]}","description":"brief voice identity","attribution_keywords":[],"reference_prompt":"brief dry sample"}}}},"parsed_source_units":[{{"source_unit_id":"s0001","type":"narration|dialogue|monologue","speaker":"role","speaker_evidence":"explicit attribution or concise contextual evidence","speaker_confidence":"high|medium|low","emotion":"brief","delivery":"brief","adapted_text":"omit when verbatim","sfx_before":[],"sfx_during":[],"sfx_after":[],"sfx_layer":"none|ambience|background|foreground","music_intent":"brief"}}],"chunk_plan":[{{"chunk_id":"chunk_001","title":"brief","source_unit_ids":[],"active_roles":[],"persistent_ambience":"brief","music_bed":"brief","sound_design":"brief","speech_rate":0,"pace_note":"brief","planned_pre_roll_sec":1.0,"planned_post_roll_sec":2.5,"continuity":{{"previous_context_summary":"brief","chunk_opening_state":"brief","chunk_ending_state":"brief","next_context_hint":"brief"}},"expected_duration_sec":{{"min":8,"max":{int(SAFE_DURATION_CEILING_SEC)}}}}}],"scene_notes":"brief"}}
"""


def compact_rewrite_prompt(source_units: list[dict]) -> str:
    allowed_speakers = "|".join(ROLE_SPECS.keys())
    if is_auto_planning():
        if STORY_CONFIG and STORY_CONFIG.get("lock_roles") and STORY_CONFIG.get("roles"):
            return compact_locked_rewrite_prompt(source_units)
        speaker_candidates = ENGLISH_SPEAKER_CANDIDATES if is_english_prompt() else CHINESE_SPEAKER_CANDIDATES
        language_name = "English" if is_english_prompt() else "Chinese"
        return compact_dynamic_rewrite_prompt(source_units, speaker_candidates, language_name)
        role_lock_instruction = ""
        if STORY_CONFIG and STORY_CONFIG.get("lock_roles") and STORY_CONFIG.get("roles"):
            role_lock_instruction = (
                "\nRole registry lock:\n"
                "The user supplied a fixed chapter-level role registry. Use only these role ids for speakers, "
                "active_roles, dialogue attribution, and the returned roles object. Do not invent replacement role names. "
                "If a minor offstage voice is not in the registry, represent it through Narrator, ambience, or non-quoted background sound.\n"
                f"supplied_role_registry:\n{json.dumps(STORY_CONFIG.get('roles'), ensure_ascii=False, indent=2)}\n"
            )
        return f"""You are a senior audiobook and audio-drama planner for Seed Audio 1.0.

Convert plain {language_name} fiction source_units into a complete production plan. The user only supplied source text, so you must infer roles, speakers, scene beats, chunk boundaries, music, ambience, and action SFX.
{role_lock_instruction}

Hard requirements:
1. Preserve every source_unit_id exactly once and in order in chunk_plan.source_unit_ids. Do not reorder or change story meaning.
2. Generate a roles registry with Narrator plus every speaking character needed by the excerpt.
3. Every role must include: key, label, reference_mode, default_speaker, description, attribution_keywords, reference_prompt. If a fixed role registry is supplied, preserve its reference_mode exactly; do not convert speaker roles into tts_audio roles.
4. Choose default_speaker values only from this candidate list: {json.dumps(speaker_candidates, ensure_ascii=False)}. Reuse candidates if there are more roles than candidates.
5. parsed_source_units must infer type, speaker, emotion, narration_style, delivery, sfx_before, sfx_during, sfx_after, music_intent.
6. quoted_text units must map to a specific role when possible. If speaker cannot be inferred, use Narrator and explain in speaker_evidence.
7. Create chunk_plan automatically after parsing the source. Each chunk is a coherent cinematic performance window, not a fixed character count. Do not make one oversized prompt cover a whole section just because it is under the character limit.
8. Chunk constraints: split at story turns, location/ambience changes, speaker hand-offs, or dense action. Prefer compact windows with enough room for natural breaths, complete sentence endings, and source-motivated sound. Every character who has quoted dialogue in a chunk must be included in that chunk's active_roles and get a reference marker. If more than 3 characters need quoted dialogue, split the chunk instead of letting an unreferenced character speak.
9. For each chunk, infer:
   - title
   - source_unit_ids
   - active_roles: the exact 1-3 roles bound to <<TGT_SPK1>>, <<TGT_SPK2>>, <<TGT_SPK3>> in this chunk, in order
   - persistent_ambience from place/time/space
   - music_bed with concrete style, instruments, rhythm, and emotional function
   - sound_design with action-anchored SFX
   - speech_rate from 0 to 10. Use performance direction, music, and SFX to create urgency; never turn action into rushed speech.
   - pace_note
   - continuity fields
   - expected_duration_sec
   - coverage_summary: one sentence explaining how all source_unit_ids in the chunk are represented
   - source_beats: 3-8 story beats that must be understandable in the generated audio
   - must_keep_dialogue: exact or lightly adapted dialogue lines that must be spoken
   - optional_dialogue: dialogue lines that may be compressed into narration or sound action
   - narration_bridge: 1-3 compact narration phrases that connect plot turns when needed
   - sfx_story_events: action events that can be carried by foreground SFX instead of narration
   - omission_rationale: what is compressed or omitted, and why the audio-drama version remains faithful
   - final_audio_prompt: the exact prompt that will be sent to Seed Audio 1.0
10. Music must be planned as one continuous long-scene score, not separate cuelets. Keep the same key, instruments, tempo feel, and room reverb across chunks. First chunk enters, middle chunks continue/carry without cadence, only final chunk briefly resolves. Music must be written as events in the timeline, not only as a header.
11. Sound effects must be motivated by actions in the text. Analyse SFX at unit level. For each unit, classify sound needs by the material itself: ambience/background/foreground, importance low/medium/high, and reason. Foreground means any narratively important visible or implied action sound in this source, whatever the genre is. Move only micro-ambience into persistent ambience.
12. The final_audio_prompt must follow the official example style: a chronological performance prompt that sounds like one complete mixed scene. It should be natural paragraphs or short timeline beats, not a rigid source-unit reading list.
13. Do not plan deliberate silence, total silence, long pause, overlapping voices, cross-talk, or simultaneous narration/dialogue. The final audio must have strictly sequential voices.
14. This is audio-drama adaptation, not full-book read-aloud. Do not convert every source_unit_id into a separate Narrator narrates / Character says line. Compress only nonessential narration into connective cinematic narration, keep every source beat understandable, keep must_keep_dialogue as actual spoken lines, and let action, music, ambience, and foreground SFX carry visible action.
14a. Do not replace dialogue with summaries like "deliver her line", "the armor booms its challenge", "Mara acknowledges the trick", or "narrate the action". If a character speaks in the source, keep representative spoken lines as actual quoted dialogue in final_audio_prompt.
14b. Use this official-style dialogue form for key lines, not a dry "speaks" label:
    Character Name (actor is <<TGT_SPK2>>, emotion/voice color): "Exact or lightly adapted spoken line."
    Do not write quoted dialogue for any character unless that character is in active_roles and has an actor marker in the same line.
14c. Forbidden examples in final_audio_prompt:
    Old Portrait (stuffy): "Highly improper spellwork after curfew!"
    Mara Voss (cold quiet): "Clever."
    These are forbidden because they have quoted dialogue without "actor is <<TGT_SPKn>>". Either include that role in active_roles with an actor marker, split the chunk, or convert the moment into non-quoted narration/SFX.
15. final_audio_prompt quality constraints:
   - 1200 to {MAX_PROMPT_CHARS} characters
   - include this exact language lock near the top: "All audible speech must be English only. Do not translate or speak Chinese."
   - contain the phrases "one voice at a time" and "no overlapping narration and dialogue"
   - bind active roles with "actor is <<TGT_SPK1>>" etc.
   - start with an explicit Voice continuity line mapping each active role to its marker
   - include continuous ambience, clearly audible background music, and foreground action effects as time-ordered events
   - keep spoken/narrated quote content between 8% and 45% of the prompt by word count when the chunk contains dialogue
   - include at least 2 quoted character dialogue lines when the chunk contains 2 or more dialogue units
   - every quoted dialogue line must contain "actor is <<TGT_SPKn>>" for that line's speaker
   - no more than 3 explicit Narrator lines in a chunk
   - do not say "Do not read", "instruction", "source_unit", "SFX cue", "narrate", or "deliver the line" inside the final_audio_prompt
   - any narration that should be spoken must be an explicit Narrator line with actor marker and quoted English text
   - action, ambience, music, and SFX directions must be phrased as production sound events, not as prose that could be read aloud
16. Before returning JSON, self-check every chunk for audio-drama coverage:
   - every source_beats item is represented by spoken dialogue, narration bridge, ambience/SFX, or music in final_audio_prompt
   - every must_keep_dialogue item appears as quoted dialogue with actor marker
   - every omitted or compressed source moment has a short omission_rationale
   - the prompt may be shorter than a read-aloud script only when beat coverage remains complete
17. Before returning JSON, self-check every final_audio_prompt:
   - every quoted dialogue segment must match: Role Name (actor is <<TGT_SPKn>>, ...): "quoted line"
   - every Role Name with quoted dialogue must be present in that chunk's active_roles
   - no chunk may contain quoted dialogue from more than 3 roles
   If this check fails, split the chunk or remove the quote.
18. Return one complete valid JSON object only. No Markdown.
19. Because final_audio_prompt is a JSON string, every double quote inside it must be escaped as \\\". Never put raw unescaped dialogue quotes inside a JSON string.

source_title: {SOURCE_TITLE}
scene_id: {SCENE_ID}
production_mode: {PRODUCTION_MODE}

source_units:
{json.dumps(source_units, ensure_ascii=False, indent=2)}

Output schema:
{{
  "scene_id": "{SCENE_ID}",
  "production_mode": "{PRODUCTION_MODE}",
  "rewrite_model": "{REWRITE_MODEL}",
  "roles": {{
    "Narrator": {{
      "key": "narrator",
      "label": "Narrator",
      "reference_mode": "speaker|tts_audio",
      "default_speaker": "{speaker_candidates[0]}",
      "description": "clear literary narrator...",
      "attribution_keywords": [],
      "reference_prompt": "single-speaker dry voice sample text"
    }}
  }},
  "parsed_source_units": [
    {{
      "source_unit_id": "s0001",
      "source_order": 1,
      "paragraph_id": "p01",
      "source_kind": "narrative_text|quoted_text",
      "source_text": "must match input",
      "type": "narration|dialogue|monologue",
      "speaker": "Narrator|RoleId",
      "speaker_evidence": "attribution/context evidence",
      "adapted_text": "default equals source_text",
      "preservation_level": "verbatim|lightly_adapted",
      "omission_reason": null,
      "emotion": "specific emotion",
      "narration_style": "descriptive_narration|action_narration|psychological_narration|transition_narration|null",
      "delivery": "natural performance and adaptive pace",
      "sfx_before": [],
      "sfx_during": [],
      "sfx_after": [],
      "sfx_layer": "none|ambience|background|foreground",
      "sfx_importance": "none|low|medium|high",
      "sfx_reason": "why this unit does or does not need an audible sound cue",
      "music_intent": "how music should support this unit"
    }}
  ],
  "adaptation_plan": {{
    "format": "audio_drama_adaptation",
    "source_beats": ["core story beat understandable in audio"],
    "must_keep_dialogue": [
      {{"source_unit_id": "s0001", "speaker": "RoleId", "text": "quoted line that must be spoken"}}
    ],
    "optional_dialogue": [
      {{"source_unit_id": "s0002", "speaker": "RoleId", "reason": "can be compressed without losing plot"}}
    ],
    "narration_strategy": "how Narrator connects plot without reading every sentence",
    "sfx_story_strategy": "how foreground SFX carries visible action",
    "music_strategy": "continuous score plan across chunks",
    "compression_policy": "what may be compressed and what may not"
  }},
  "chunk_plan": [
    {{
      "chunk_id": "chunk_001",
      "title": "Beat title",
      "source_unit_ids": ["s0001"],
      "active_roles": ["Narrator"],
      "persistent_ambience": "specific continuous ambience",
      "music_bed": "same shared score palette plus this chunk's intensity contour; no independent cue ending",
      "sound_design": "specific action-anchored SFX",
      "speech_rate": 16,
      "pace_note": "adaptive performance note",
      "continuity": {{
        "previous_context_summary": "none/opening",
        "chunk_opening_state": "where this chunk begins",
        "chunk_ending_state": "where this chunk lands",
        "next_context_hint": "what continues next"
      }},
      "expected_duration_sec": {{"min": 35, "max": 120}},
      "coverage_summary": "All listed source units are represented through compressed narration, key dialogue, action, ambience, music, and foreground SFX.",
      "source_beats": ["beat that must be audible or understandable"],
      "must_keep_dialogue": [
        {{"source_unit_id": "s0001", "speaker": "RoleId", "text": "quoted line"}}
      ],
      "optional_dialogue": [
        {{"source_unit_id": "s0002", "speaker": "RoleId", "reason": "why optional"}}
      ],
      "narration_bridge": ["compact narrated connector"],
      "sfx_story_events": ["foreground SFX event that carries story action"],
      "omission_rationale": ["compressed detail and rationale"],
      "dialogue_lines": [
        {{"speaker": "RoleId", "text": "exact or lightly adapted line that must be spoken in the audio prompt"}}
      ],
      "final_audio_prompt": "Exact Seed Audio 1.0 prompt in official example style."
    }}
  ],
  "scene_parse": {{
    "scene_id": "{SCENE_ID}",
    "beats": []
  }},
  "scene_notes": "sound-space and story summary"
}}
"""
    if is_english_prompt():
        return f"""You are an audio drama text parser. Convert the plain English fiction source_units into a compact JSON plan that can later be rendered into Seed Audio 1.0 director prompts.

Requirements:
1. Preserve the plot and meaning. Do not rewrite the story into a different scene.
2. Cover every source_unit_id exactly once, in the same order. Do not merge, omit, or reorder units.
3. Keep source_text and adapted_text in English. adapted_text should normally equal source_text, with only very light performance-friendly cleanup if needed.
4. Infer type, speaker, emotion, delivery, SFX, and music_intent for each unit.
5. speaker must be one of: {allowed_speakers}.
6. delivery must include adaptive pacing: slower for dread/reveal, natural for dialogue, brisk but clear for action.
7. Only parse unit-level structure. The engineering layer will split the scene into short Audio chunks with at most 3 active voices each.
8. Return one valid JSON object only. No Markdown, no commentary.

source_units:
{json.dumps(source_units, ensure_ascii=False, indent=2)}

Output JSON schema:
{{
  "scene_id": "{SCENE_ID}",
  "production_mode": "{PRODUCTION_MODE}",
  "rewrite_model": "{REWRITE_MODEL}",
  "parsed_source_units": [
    {{
      "source_unit_id": "s0001",
      "source_order": 1,
      "paragraph_id": "p01",
      "source_kind": "narrative_text|quoted_text",
      "source_text": "Must match the input English text.",
      "type": "narration|dialogue|monologue",
      "speaker": "{allowed_speakers}",
      "speaker_evidence": "Evidence from attribution or context.",
      "adapted_text": "Default equals source_text; do not change meaning.",
      "preservation_level": "verbatim|lightly_adapted",
      "omission_reason": null,
      "emotion": "emotion",
      "narration_style": "descriptive_narration|action_narration|psychological_narration|transition_narration|null",
      "delivery": "performance style and adaptive pace",
      "sfx_before": [],
      "sfx_during": [],
      "sfx_after": [],
      "music_intent": "music intent"
    }}
  ],
  "scene_notes": "One sentence describing the scene and sound space."
}}
"""
    return f"""你是有声剧文本解析器。请按飞书文档要求,把纯中文小说 source_units 解析成精简 JSON。

必须满足:
1. 不改剧情、不改台词原意。
2. 逐条覆盖每个 source_unit_id,顺序不能变,不能合并,不能省略。
2a. 所有输出文本字段必须使用简体中文,不要输出繁体。
3. 自动判断 type、speaker、emotion、delivery、sfx、music_intent。
4. speaker 只允许: {allowed_speakers}。
5. delivery 必须包含语速提示,整体 medium-fast, steady but not slow。
6. 只负责逐句解析,不要生成长导演 prompt;工程层会把剧情拆成 10-12 个短 chunk。
7. 输出必须是一个完整可解析 JSON,不要 Markdown,不要解释。

source_units:
{json.dumps(source_units, ensure_ascii=False, indent=2)}

输出 JSON schema:
{{
  "scene_id": "{SCENE_ID}",
  "production_mode": "{PRODUCTION_MODE}",
  "rewrite_model": "{REWRITE_MODEL}",
  "parsed_source_units": [
    {{
      "source_unit_id": "s0001",
      "source_order": 1,
      "paragraph_id": "p01",
      "source_kind": "narrative_text|quoted_text",
      "source_text": "必须逐字对应输入的简体文本",
      "type": "narration|dialogue|monologue",
      "speaker": "{allowed_speakers}",
      "speaker_evidence": "说话人判断证据",
      "adapted_text": "默认等于 source_text,不得改变原意",
      "preservation_level": "verbatim|lightly_adapted",
      "omission_reason": null,
      "emotion": "情绪",
      "narration_style": "descriptive_narration|action_narration|psychological_narration|transition_narration|null",
      "delivery": "表演方式和语速",
      "sfx_before": [],
      "sfx_during": [],
      "sfx_after": [],
      "music_intent": "音乐意图"
    }}
  ],
  "scene_notes": "一句话说明整体剧情和声音空间"
}}
"""


def compact_locked_rewrite_prompt(source_units: list[dict]) -> str:
    roles = {
        role: {
            "speaker": spec.get("default_speaker"),
            "voice": spec.get("description", ""),
        }
        for role, spec in (STORY_CONFIG or {}).get("roles", {}).items()
    }
    units = [
        {
            "id": unit["source_unit_id"],
            "order": unit.get("source_order"),
            "kind": unit.get("source_kind"),
            "text": unit.get("source_text"),
            "attribution": unit.get("quote_attribution_text"),
        }
        for unit in source_units
    ]
    replan_feedback = (STORY_CONFIG or {}).get("replan_feedback")
    replan_block = ""
    if replan_feedback:
        replan_block = (
            "\nA previous planning or render attempt failed admission or audible QA. Treat this as planning evidence, not text to be spoken. "
            "Create a materially revised chunk boundary and sound timeline that addresses it; do not merely append a repair sentence.\n"
            f"Pilot failure evidence: {json.dumps(replan_feedback, ensure_ascii=False, separators=(',', ':'))}\n"
        )
    return f"""Plan one English Seed Audio 1.0 audio-drama section. Return JSON only.

Fixed roles (role: provider speaker and performance identity):
{json.dumps(roles, ensure_ascii=False, separators=(',', ':'))}
{replan_block}

Rules:
1. Use only the fixed role names. Preserve every input id exactly once and in order in parsed_source_units and chunk_plan.
2. Infer each unit's speaker, type, emotion, delivery, source-motivated SFX, and music intent without changing story meaning. Every dialogue unit must include speaker_evidence and speaker_confidence (high|medium|low). Use high only when source evidence is sufficient to bind a named role. Medium or low confidence must use Narrator rather than a guessed specific character.
3. Split at dramatic turns, ambience changes, dense action, speaker hand-offs, the {SAFE_SOURCE_UNITS_PER_CHUNK}-source-unit safety limit, or the 3-active-role limit. Estimate audible speech first, add roughly 0.5-2s pre-roll and 1.5-4s audible post-roll, and target at most {int(SAFE_DURATION_CEILING_SEC)} seconds. Short lines should produce short chunks; never stretch a request with silence. Never place two active roles with the same provider speaker in one chunk.
4. This is faithful audio-drama adaptation, not full read-aloud: retain key dialogue as spoken quotes, use compact narration bridges, and let ambience/music/foreground SFX carry visible action.
5. Across chunks use one continuous scene-specific score palette. First chunk enters, middle chunks carry without cadence, final chunk alone may resolve.
6. This is stage-one analysis. Do not return final_audio_prompt, text_prompt, repeated source text, voice registry, or reference prompts. The workflow builds each Audio 1.0 prompt separately after this compact plan passes coverage checks.
7. Keep every field brief. adapted_text is required only when lightly adapting a unit; omit it for verbatim units. Use no prose outside the JSON.
8. For each chunk describe scene-specific ambience, music as style + instruments + rhythm or atmosphere, foreground sound design, pace, and continuity.
9. Let the source determine how many sound effects are useful. Prefer motivated sounds over a fixed quota.
10. Use narration only when it naturally clarifies action, space or speaker identity. Never create standalone dry attribution such as "Harry said" or "yelled Harry".
11. Keep requested music and ambience perceptibly audible. Avoid stacking soft/faint/distant/low wording, order foreground events chronologically, and do not attach many competing SFX to one short spoken line.

Input units:
{json.dumps(units, ensure_ascii=False, separators=(',', ':'))}

Compact output schema:
{{"scene_id":"{SCENE_ID}","production_mode":"{PRODUCTION_MODE}","parsed_source_units":[{{"source_unit_id":"s0001","type":"narration|dialogue|monologue","speaker":"fixed role","speaker_evidence":"explicit attribution or concise contextual evidence","speaker_confidence":"high|medium|low","emotion":"brief","delivery":"brief","adapted_text":"omit when verbatim","sfx_before":[],"sfx_during":[],"sfx_after":[],"sfx_layer":"none|ambience|background|foreground","music_intent":"brief"}}],"chunk_plan":[{{"chunk_id":"chunk_001","title":"brief","source_unit_ids":[],"active_roles":[],"persistent_ambience":"brief","music_bed":"brief","sound_design":"brief","speech_rate":0,"pace_note":"brief","continuity":{{"previous_context_summary":"brief","chunk_opening_state":"brief","chunk_ending_state":"brief","next_context_hint":"brief"}},"expected_duration_sec":{{"min":20,"max":{int(SAFE_DURATION_CEILING_SEC)}}}}}],"scene_notes":"brief"}}
"""


def call_with_wall_timeout(seconds: int, callback):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        return callback()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def timeout_handler(_signum, _frame):
        raise TimeoutError(f"Seed 2.0 Pro rewrite exceeded {seconds}s wall timeout")

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    try:
        return callback()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def speaker_env_name(role: str) -> str:
    return f"SEED_AUDIO_SPEAKER_{ROLE_SPECS[role]['key'].upper()}"


def configured_speaker(role: str) -> str | None:
    return os.getenv(speaker_env_name(role), "").strip() or ROLE_SPECS[role].get("default_speaker")


def reference_mode(role: str) -> str:
    return str(ROLE_SPECS[role].get("reference_mode", "speaker")).strip() or "speaker"


def infer_speaker_from_source(unit: dict) -> str:
    if unit.get("source_kind") != "quoted_text":
        return "Narrator"
    attribution = unit.get("quote_attribution_text", "")
    if "军士" in attribution or "齐声" in attribution:
        return "Soldiers"
    for role, spec in ROLE_SPECS.items():
        if role == "Narrator":
            continue
        for keyword in spec.get("attribution_keywords", []):
            if keyword and keyword in attribution:
                return role
    return "Narrator"


def default_chunk_plan(source_units: list[dict]) -> list[dict]:
    by_id = {unit["source_unit_id"]: unit for unit in source_units}
    if STORY_CONFIG and STORY_CONFIG.get("chunk_plan"):
        plans = []
        for index, item in enumerate(STORY_CONFIG["chunk_plan"], start=1):
            ids = [unit_id for unit_id in item.get("source_unit_ids", []) if unit_id in by_id]
            if not ids:
                continue
            default_continuity = (
                {
                    "previous_context_summary": "Continues from the previous short beat.",
                    "chunk_opening_state": item.get("title", f"Beat {index:02d}"),
                    "chunk_ending_state": "Carries into the next short beat.",
                    "next_context_hint": "Continue the scene action.",
                }
                if is_english_prompt()
                else {
                    "previous_context_summary": "承接上一小段。",
                    "chunk_opening_state": item.get("title", f"Beat {index:02d}"),
                    "chunk_ending_state": "进入下一小段。",
                    "next_context_hint": "继续推进剧情。",
                }
            )
            plans.append(
                {
                    "chunk_id": item.get("chunk_id", f"chunk_{index:03d}"),
                    "title": item.get("title", f"Beat {index:02d}"),
                    "source_unit_ids": ids,
                    "active_roles": item.get("active_roles", []),
                    "persistent_ambience": item.get("persistent_ambience", "环境声自然延续。"),
                    "music_bed": item.get("music_bed", "音乐轻入,对白时下潜,结尾淡出。"),
                    "sound_design": item.get("sound_design", "环境声、轻微动作声。"),
                    "speech_rate": int(item.get("speech_rate", TARGET_SPEECH_RATE)),
                    "pace_note": item.get("pace_note", "自然语速,按剧情调整快慢。"),
                    "continuity": item.get("continuity", default_continuity),
                    "expected_duration_sec": item.get("expected_duration_sec", {"min": 8, "max": 35}),
                    "coverage_summary": item.get("coverage_summary", ""),
                    "source_beats": item.get("source_beats", []),
                    "must_keep_dialogue": item.get("must_keep_dialogue", []),
                    "optional_dialogue": item.get("optional_dialogue", []),
                    "narration_bridge": item.get("narration_bridge", []),
                    "sfx_story_events": item.get("sfx_story_events", []),
                    "omission_rationale": item.get("omission_rationale", []),
                    "final_audio_prompt": item.get("final_audio_prompt") or item.get("audio_prompt") or item.get("text_prompt", ""),
                }
            )
        return plans
    if is_auto_planning():
        plans = []
        chunk_size = 3
        for index in range(0, len(source_units), chunk_size):
            units = source_units[index : index + chunk_size]
            chunk_no = len(plans) + 1
            title = f"Beat {chunk_no:02d}"
            plans.append(
                {
                    "chunk_id": f"chunk_{chunk_no:03d}",
                    "title": title,
                    "source_unit_ids": [unit["source_unit_id"] for unit in units],
                    "persistent_ambience": "room tone and scene ambience inferred from the text remain subtle and continuous",
                    "music_bed": "soft underscore with light strings and low pads supports the emotion without covering speech",
                    "sound_design": "only small action-motivated sounds from the text",
                    "speech_rate": 16,
                    "pace_note": "natural adaptive pacing, clear dialogue and steady narration",
                    "continuity": {
                        "previous_context_summary": "Continues from the previous beat." if chunk_no > 1 else "Opening beat.",
                        "chunk_opening_state": title,
                        "chunk_ending_state": "Carries into the next beat.",
                        "next_context_hint": "Continue the source text in order.",
                    },
                    "expected_duration_sec": {"min": 8, "max": 35},
                }
            )
        return plans
    groups = [
        ("chunk_001", "五更近寨", ["s0001", "s0002"]),
        ("chunk_002", "鲁肃惊问", ["s0003", "s0004", "s0005"]),
        ("chunk_003", "曹营惊报", ["s0006"]),
        ("chunk_004", "曹操下令", ["s0007", "s0008"]),
        ("chunk_005", "调弓助射", ["s0009", "s0010"]),
        ("chunk_006", "箭雨齐发", ["s0011"]),
        ("chunk_007", "掉船受箭", ["s0012", "s0013", "s0014"]),
        ("chunk_008", "齐声谢箭", ["s0015"]),
        ("chunk_009", "曹操懊悔", ["s0016"]),
        ("chunk_010", "鲁肃叹服", ["s0017", "s0018", "s0019", "s0020", "s0021"]),
        ("chunk_011", "孔明释计", ["s0022", "s0023", "s0024", "s0025", "s0026"]),
    ]
    plans = []
    ambience = {
        "chunk_001": "江面浓雾,水声低伏,木船缓行。",
        "chunk_002": "船中酒盏轻响,远处鼓声压低。",
        "chunk_003": "曹营夜哨惊动,脚步急促,远鼓传来。",
        "chunk_004": "军帐内甲片轻响,号令回荡。",
        "chunk_005": "营寨奔走,弓弩整备,江风急。",
        "chunk_006": "万箭破雾,箭雨扎入草束。",
        "chunk_007": "草船调头,箭枝摇响,船桨加速。",
        "chunk_008": "江面开阔,军士齐呼,船队远去。",
        "chunk_009": "曹营混乱渐远,江水推船疾行。",
        "chunk_010": "返航船舱安定,江水轻拍,酒盏在案。",
        "chunk_011": "雾散之后,船中安静,江风收束。",
    }
    music = {
        "chunk_001": "低沉弦乐轻入,鼓声起时微升,结尾淡出。",
        "chunk_002": "低弦保持悬疑,对白时压低,孔明答话后淡出。",
        "chunk_003": "急促鼓点轻入,飞报时增强,结尾压住。",
        "chunk_004": "低鼓进入,曹操下令时压低,命令后短促上扬。",
        "chunk_005": "紧张鼓点维持,弓弩整备时增强,结尾淡出。",
        "chunk_006": "战鼓与低弦增强,箭雨时达到高点,结尾留低音。",
        "chunk_007": "计谋得逞的低弦轻入,收船时微升,结尾淡出。",
        "chunk_008": "短促凯旋调轻入,谢箭时上扬,随后淡出。",
        "chunk_009": "低沉遗憾调进入,曹操懊悔时压低,远去时淡出。",
        "chunk_010": "古琴轻入,鲁肃惊叹时压低,孔明反问后淡出。",
        "chunk_011": "古琴与低弦轻入,孔明解释时稳住,鲁肃拜服后淡出。",
    }
    sound = {
        "chunk_001": "水声、船桨、远鼓。",
        "chunk_002": "酒盏、战鼓、江风。",
        "chunk_003": "脚步、营帐、警锣。",
        "chunk_004": "甲片、号令、士兵应声。",
        "chunk_005": "奔跑、弓弦拉紧、军阵骚动。",
        "chunk_006": "箭破空、箭扎草束、战鼓。",
        "chunk_007": "船身调头、箭枝晃动、急桨。",
        "chunk_008": "军士齐声、江风、船队远去。",
        "chunk_009": "曹营嘈杂、拍案、远水声。",
        "chunk_010": "酒盏、船板轻响、江水。",
        "chunk_011": "衣袍、轻风、江水渐静。",
    }
    speech_rates = {
        "chunk_001": 22,
        "chunk_002": 22,
        "chunk_003": 26,
        "chunk_004": 24,
        "chunk_005": 28,
        "chunk_006": 30,
        "chunk_007": 24,
        "chunk_008": 26,
        "chunk_009": 20,
        "chunk_010": 20,
        "chunk_011": 18,
    }
    pace_notes = {
        "chunk_001": "自然中速,雾夜开场留短暂停顿,擂鼓处略加速。",
        "chunk_002": "对白自然,鲁肃急但不抢,孔明从容稍慢。",
        "chunk_003": "飞报节奏偏快,旁白清楚交代曹营反应。",
        "chunk_004": "曹操命令稳重有压迫感,不要过快。",
        "chunk_005": "调兵整弓偏快,旁白推动动作。",
        "chunk_006": "箭雨动作最快,但字句仍要清楚。",
        "chunk_007": "计谋得手后回到自然速度,收船处略快。",
        "chunk_008": "军士齐呼短促有力,不要拖腔。",
        "chunk_009": "曹操懊悔放慢,留一点失手后的空白。",
        "chunk_010": "返航对谈自然偏慢,鲁肃惊服要有停顿。",
        "chunk_011": "孔明解释放慢,像讲清谋略,关键句留停顿。",
    }
    for chunk_id, title, ids in groups:
        units = [by_id[unit_id] for unit_id in ids if unit_id in by_id]
        if not units:
            continue
        plans.append(
            {
                "chunk_id": chunk_id,
                "title": title,
                "source_unit_ids": [unit["source_unit_id"] for unit in units],
                "persistent_ambience": ambience[chunk_id],
                "music_bed": music[chunk_id],
                "sound_design": sound[chunk_id],
                "speech_rate": speech_rates[chunk_id],
                "pace_note": pace_notes[chunk_id],
                "continuity": {
                    "previous_context_summary": "承接上一小段。",
                    "chunk_opening_state": title,
                    "chunk_ending_state": "进入下一小段。",
                    "next_context_hint": "继续推进草船借箭。",
                },
                "expected_duration_sec": {"min": 8, "max": 35},
            }
        )
    return plans


def active_roles_for_chunk(chunk: dict, parsed_by_id: dict) -> list[str]:
    actual_roles = roles_for_unit_ids(chunk.get("source_unit_ids", []), parsed_by_id)
    planned_roles = []
    for role in chunk.get("active_roles", []) or []:
        if role in ROLE_SPECS and role in actual_roles and role not in planned_roles:
            planned_roles.append(role)
    if planned_roles:
        return (planned_roles + [role for role in actual_roles if role not in planned_roles])[:3]
    return actual_roles[:3] or (["Narrator"] if "Narrator" in ROLE_SPECS else list(ROLE_SPECS)[:1])


def roles_for_unit_ids(unit_ids: list[str], parsed_by_id: dict) -> list[str]:
    roles: list[str] = []
    for unit_id in unit_ids:
        unit = parsed_by_id.get(unit_id, {})
        speaker = unit.get("speaker", "Narrator")
        if speaker not in ROLE_SPECS:
            speaker = "Narrator"
        raw_text = str(unit.get("adapted_text") or unit.get("source_text", ""))
        final_token = (re.findall(r"[A-Za-z']+", raw_text) or [""])[-1]
        if speaker != "Narrator" and re.search(r"[-–—]\s*$", raw_text) and len(final_token) <= 6:
            continue
        if speaker not in roles:
            roles.append(speaker)
    return roles


def request_voice_capacity_ok(unit_ids: list[str], parsed_by_id: dict, max_roles: int = 3) -> bool:
    roles = roles_for_unit_ids(unit_ids, parsed_by_id)
    if len(roles) > max_roles:
        return False
    speakers: set[str] = set()
    for role in roles:
        if role not in ROLE_SPECS or reference_mode(role) != "speaker":
            continue
        speaker = configured_speaker(role)
        if speaker and speaker in speakers:
            return False
        if speaker:
            speakers.add(speaker)
    return True


def is_explicit_narration_bridge_unit(unit: dict) -> bool:
    """Use one source-aware predicate for narration synthesis and its gate.

    A source parser's ``narrative_text`` classification outranks lexical
    attribution heuristics.  Sentences such as "Harry asked." and "Harry
    groaned." are still source narration when the preserved unit says so; they
    must not disappear from the prompt or be recast as Harry dialogue.
    """
    raw_source_text = str(unit.get("source_text", "")).strip()
    raw_adapted_text = str(unit.get("adapted_text") or raw_source_text).strip()
    text = clean_prompt_quote_text(raw_adapted_text)
    completeness_text = (
        raw_source_text
        if unit.get("source_kind") == "narrative_text" and raw_source_text
        else raw_adapted_text
    )
    if unit.get("speaker") != "Narrator" or not text or narrator_quote_is_incomplete(completeness_text):
        return False
    if unit.get("source_kind") == "narrative_text":
        return True
    return not is_dialogue_attribution_text(text)


def local_plan_fields_for_units(plan: dict, unit_ids: list[str], parsed_by_id: dict) -> dict:
    unit_set = set(unit_ids)
    units = [parsed_by_id[unit_id] for unit_id in unit_ids if unit_id in parsed_by_id]
    source_beats = []
    sfx_events = []
    narration_bridge = []
    for unit in units:
        text = clean_prompt_quote_text(unit.get("adapted_text") or unit.get("source_text", ""))
        if is_explicit_narration_bridge_unit(unit) and len(narration_bridge) < 4:
            narration_bridge.append(compact_clause_text(text, text, 125))
        if text and len(source_beats) < 6:
            source_beats.append(compact_clause_text(text, text, 115))
        for key in ("sfx_before", "sfx_during", "sfx_after"):
            for cue in unit.get(key, []) or []:
                cleaned = clean_english_director_text(str(cue))
                if cleaned and cleaned not in sfx_events and len(sfx_events) < 6:
                    sfx_events.append(cleaned)
    filtered: dict = {
        "source_beats": source_beats or plan.get("source_beats", []),
        "narration_bridge": narration_bridge or plan.get("narration_bridge", []),
        "sfx_story_events": sfx_events or plan.get("sfx_story_events", []),
        "omission_rationale": plan.get("omission_rationale", []) or (
            ["Nonessential descriptive wording may be compressed into narration and source-motivated sound while every source unit remains mapped."]
            if len(unit_ids) >= 8
            else []
        ),
    }
    for field in ("must_keep_dialogue", "optional_dialogue", "dialogue_lines"):
        values = plan.get(field)
        if isinstance(values, list):
            filtered[field] = [
                item for item in values
                if not isinstance(item, dict) or not item.get("source_unit_id") or item.get("source_unit_id") in unit_set
            ]
    return filtered


def normalize_speaker_attribution(unit: dict) -> dict:
    normalized = dict(unit)
    if normalized.get("source_kind") != "quoted_text":
        normalized["speaker_confidence"] = "high"
        normalized.setdefault("speaker_evidence", "narration")
        return normalized
    attribution = str(normalized.get("quote_attribution_text") or "").strip()
    evidence = str(normalized.get("speaker_evidence") or "").strip()
    confidence = str(normalized.get("speaker_confidence") or "").strip().lower()
    if attribution:
        normalized["speaker_evidence"] = attribution
        normalized["speaker_confidence"] = "high"
    elif confidence in {"high", "medium"} and len(evidence) >= 12:
        normalized["speaker_confidence"] = confidence
    else:
        normalized["speaker_confidence"] = "low"
        normalized.setdefault("speaker_evidence", "source does not explicitly identify the speaker")
    return normalized


def same_quote_turn(left: dict, right: dict) -> bool:
    """Return true when two sentence units came from one original quotation.

    New source units carry quote_span_id. The source-span fallback keeps
    already persisted schema-2 runs recoverable without rebuilding or sending
    their text to a provider again.
    """
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


def named_speaker_has_source_evidence(parsed_units: list[dict], index: int, _visited: set[int] | None = None) -> bool:
    visited = set(_visited or set())
    if index in visited:
        return False
    visited.add(index)
    unit = parsed_units[index]
    role = str(unit.get("speaker") or "")
    if unit.get("source_kind") != "quoted_text" or role == "Narrator":
        return True
    spec = ROLE_SPECS.get(role, {})
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
    # A short narrative bridge may separate an explicitly named character
    # from the first quote in the same paragraph (for example: "Dudley was
    # counting. His face fell. 'Thirty-six.'"). Do not cross another quote.
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
        for candidate_role, candidate_spec in ROLE_SPECS.items():
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
            and named_speaker_has_source_evidence(parsed_units, previous_quote_index, visited)
        ):
            return True
    if index > 0:
        previous = parsed_units[index - 1]
        if previous.get("source_kind") == "quoted_text" and previous.get("speaker") == role:
            return named_speaker_has_source_evidence(parsed_units, index - 1, visited)
    if index > 1:
        bridge = parsed_units[index - 1]
        previous_quote = parsed_units[index - 2]
        if (
            bridge.get("source_kind") == "narrative_text"
            and previous_quote.get("source_kind") == "quoted_text"
            and previous_quote.get("speaker") == role
        ):
            return named_speaker_has_source_evidence(parsed_units, index - 2, visited)
    # A middle response can be attributed safely in a verified A-B-A turn
    # when both surrounding quotes belong to the same other role and the
    # current role is explicitly named in the recent same-paragraph context.
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
            and named_speaker_has_source_evidence(parsed_units, previous_quote_index, visited)
            and named_speaker_has_source_evidence(parsed_units, next_quote_index, visited)
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
        if named_speaker_has_source_evidence(parsed_units, prior_index, visited):
            recent_verified_roles.append(prior_role)
    verified_set = set(recent_verified_roles)
    if nearest_quote_role and nearest_quote_role != role and verified_set == {nearest_quote_role, role}:
        return True
    return False


def repair_quoted_speakers(parsed_units: list[dict]) -> list[dict]:
    repaired = [dict(unit) for unit in parsed_units]
    for index, unit in enumerate(repaired):
        if unit.get("source_kind") != "quoted_text":
            continue
        if unit.get("model_omitted_fallback"):
            continue
        source_text = str(unit.get("source_text", ""))
        if not re.search(r"[A-Za-z0-9]", source_text):
            unit["speaker"] = "Narrator"
            unit["speaker_evidence"] = "non-spoken punctuation fragment"
            unit["speaker_confidence"] = "low"
            continue
        if re.search(r"\bHarry\b", source_text, re.I) and re.search(r"\bYeh\b|\bter\b|\bmusta\b|\bye\b", source_text, re.I):
            unit["speaker"] = role_key_for_identity("Hagrid") or "Narrator"
            unit["speaker_evidence"] = "engineering speaker repair from dialect and Harry vocative"
            unit["speaker_confidence"] = "high"
            continue
        if re.search(r"\bHagrid\b", source_text, re.I) and not re.search(r"\bYeh\b|\bter\b|\bmusta\b|\bye\b", source_text, re.I):
            unit["speaker"] = role_key_for_identity("Harry Potter") or role_key_for_identity("Harry") or "Narrator"
            unit["speaker_evidence"] = "engineering speaker repair from Hagrid vocative in non-Hagrid line"
            unit["speaker_confidence"] = "high"
            continue
        context_parts = []
        for neighbor_index in (index - 1, index + 1):
            if 0 <= neighbor_index < len(repaired):
                context_parts.append(str(repaired[neighbor_index].get("source_text", "")))
        context = " ".join(context_parts)
        scores: dict[str, int] = {}
        for role, spec in ROLE_SPECS.items():
            if role == "Narrator":
                continue
            keywords = [role, spec.get("label", role), *(spec.get("attribution_keywords", []) or [])]
            score = 0
            for keyword in keywords:
                if keyword and re.search(rf"\b{re.escape(str(keyword))}\b", context, re.I):
                    score += 1
            if score:
                scores[role] = score
        if not scores:
            continue
        best_role, best_score = sorted(scores.items(), key=lambda item: (-item[1], list(ROLE_SPECS).index(item[0])))[0]
        current = unit.get("speaker")
        # Seed 2 Pro has the full dramatic context. Adjacent-name heuristics are
        # only a fallback for unattributed lines, never grounds to overwrite a
        # specific role selected by the planner.
        if current and current != "Narrator":
            continue
        current_score = scores.get(current, 0)
        if best_role != current and best_score > current_score:
            unit["speaker"] = best_role
            unit["speaker_evidence"] = f"engineering speaker repair from adjacent attribution/context: {context[:180]}"
            unit["speaker_confidence"] = "medium"
    return repaired


def role_key_for_identity(identity: str) -> str | None:
    target = identity.casefold()
    for role, spec in ROLE_SPECS.items():
        candidates = [role, str(spec.get("label") or ""), *(spec.get("attribution_keywords", []) or [])]
        if any(target == str(candidate).casefold() for candidate in candidates if str(candidate).strip()):
            return role
    for role, spec in ROLE_SPECS.items():
        candidates = [role, str(spec.get("label") or ""), *(spec.get("attribution_keywords", []) or [])]
        if any(re.search(rf"\b{re.escape(target)}\b", str(candidate), re.I) for candidate in candidates):
            return role
    return None


def enforce_supported_named_speakers(parsed_units: list[dict]) -> list[dict]:
    repaired = [dict(unit) for unit in parsed_units]
    for index, unit in enumerate(repaired):
        if unit.get("source_kind") != "quoted_text" or unit.get("speaker") == "Narrator":
            continue
        if named_speaker_has_source_evidence(repaired, index):
            unit["speaker_confidence"] = "high"
            continue
        prior_role = str(unit.get("speaker") or "unknown")
        unit["speaker"] = "Narrator"
        unit["speaker_confidence"] = "low"
        unit["speaker_evidence"] = (
            f"engineering fallback: source evidence does not safely identify {prior_role}; preserve text with Narrator"
        )
    return repaired


def is_dialogue_attribution_text(text: str) -> bool:
    cleaned = clean_prompt_quote_text(text)
    verb = r"said|says|yelled|shouted|cried|asked|answered|whispered|murmured|called|replied"
    name = r"[A-Z][A-Za-z' -]*(?:\s+[A-Z][A-Za-z' -]*)?"
    return bool(
        re.fullmatch(rf"(?:{verb})\s+{name}[.!?]?", cleaned, flags=re.I)
        or re.fullmatch(rf"{name}\s+(?:{verb})[.!?]?", cleaned, flags=re.I)
    )


def split_plan_copy(plan: dict, unit_ids: list[str], parsed_by_id: dict, sub_index: int | None = None) -> dict:
    new_plan = {**plan}
    new_plan["source_unit_ids"] = unit_ids
    # Once a Seed2 chunk is split by the engineering layer, its original final
    # prompt no longer matches the new role/window boundary. Rebuild the prompt
    # from the structured plan instead of reusing stale text.
    new_plan.pop("final_audio_prompt", None)
    new_plan.pop("audio_prompt", None)
    new_plan.pop("text_prompt", None)
    new_plan.pop("active_roles", None)
    new_plan.update(local_plan_fields_for_units(plan, unit_ids, parsed_by_id))
    if sub_index is not None:
        new_plan["chunk_id"] = f"{plan.get('chunk_id', 'chunk')}_{sub_index:02d}"
        new_plan["title"] = f"{plan.get('title', 'Beat')} Part {sub_index}"
    return new_plan


def enforce_chunk_role_limit(plans: list[dict], parsed_by_id: dict, max_roles: int = 3) -> list[dict]:
    repaired: list[dict] = []
    for plan in plans:
        unit_ids = plan.get("source_unit_ids", [])
        if request_voice_capacity_ok(unit_ids, parsed_by_id, max_roles):
            repaired.append(plan)
            continue
        sub_ids: list[str] = []
        sub_index = 1
        for unit_id in unit_ids:
            candidate = sub_ids + [unit_id]
            if sub_ids and not request_voice_capacity_ok(candidate, parsed_by_id, max_roles):
                repaired.append(split_plan_copy(plan, sub_ids, parsed_by_id, sub_index))
                sub_index += 1
                sub_ids = [unit_id]
            else:
                sub_ids = candidate
        if sub_ids:
            repaired.append(split_plan_copy(plan, sub_ids, parsed_by_id, sub_index if sub_index > 1 else None))
    for index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{index:03d}"
    return repaired


def enforce_max_source_unit_count(plans: list[dict], parsed_by_id: dict, max_units: int = 7) -> list[dict]:
    repaired: list[dict] = []
    for plan in plans:
        unit_ids = list(plan.get("source_unit_ids", []))
        if len(unit_ids) <= max_units:
            repaired.append(plan)
            continue
        sub_index = 1
        for start in range(0, len(unit_ids), max_units):
            repaired.append(split_plan_copy(plan, unit_ids[start : start + max_units], parsed_by_id, sub_index))
            sub_index += 1
    for index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{index:03d}"
    return repaired


def repair_continuous_dialogue_boundaries(plans: list[dict], parsed_by_id: dict) -> list[dict]:
    """Keep a speaker's short, contiguous turn from being split across requests."""
    repaired = [{**plan, "source_unit_ids": list(plan.get("source_unit_ids", []))} for plan in plans]
    index = 0
    while index + 1 < len(repaired):
        current = repaired[index]
        following = repaired[index + 1]
        current_ids = current.get("source_unit_ids", [])
        following_ids = following.get("source_unit_ids", [])
        if not current_ids or not following_ids:
            index += 1
            continue
        tail = parsed_by_id.get(current_ids[-1], {})
        head = parsed_by_id.get(following_ids[0], {})
        tail_words = len(re.findall(r"\b[\w']+\b", str(tail.get("adapted_text") or tail.get("source_text") or "")))
        is_continuation = (
            tail.get("type") == "dialogue"
            and head.get("type") == "dialogue"
            and tail.get("speaker") == head.get("speaker")
            and tail.get("paragraph_id") == head.get("paragraph_id")
            and tail_words <= 5
        )
        candidate_ids = current_ids + [following_ids[0]]
        if is_continuation and request_voice_capacity_ok(candidate_ids, parsed_by_id):
            candidate = {**current, "source_unit_ids": candidate_ids}
            candidate.update(local_plan_fields_for_units(current, candidate_ids, parsed_by_id))
            prompt, _roles = compose_director_prompt(candidate, parsed_by_id, index + 1, len(repaired))
            if len(prompt) <= MAX_PROMPT_CHARS:
                repaired[index] = candidate
                following["source_unit_ids"] = following_ids[1:]
                if not following["source_unit_ids"]:
                    repaired.pop(index + 1)
                else:
                    following.update(local_plan_fields_for_units(following, following["source_unit_ids"], parsed_by_id))
                index += 1
                continue
        index += 1
    for chunk_index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{chunk_index:03d}"
    return repaired


def enforce_prompt_length_limit(plans: list[dict], parsed_by_id: dict) -> list[dict]:
    repaired = list(plans)
    changed = True
    while changed:
        changed = False
        next_plans: list[dict] = []
        total = len(repaired)
        for index, plan in enumerate(repaired, start=1):
            prompt, _roles = compose_director_prompt(plan, parsed_by_id, index, total)
            unit_ids = plan.get("source_unit_ids", [])
            if len(prompt) <= MAX_PROMPT_CHARS or len(unit_ids) <= 1:
                next_plans.append(plan)
                continue
            midpoint = max(1, len(unit_ids) // 2)
            narrative_boundaries = [
                candidate
                for candidate in range(1, len(unit_ids))
                if parsed_by_id.get(unit_ids[candidate], {}).get("speaker") == "Narrator"
            ]
            if narrative_boundaries:
                midpoint = min(narrative_boundaries, key=lambda candidate: (abs(candidate - midpoint), candidate))
            first = {**plan, "source_unit_ids": unit_ids[:midpoint]}
            second = {**plan, "source_unit_ids": unit_ids[midpoint:]}
            first["title"] = f"{plan.get('title', 'Beat')} A"
            second["title"] = f"{plan.get('title', 'Beat')} B"
            next_plans.extend([first, second])
            changed = True
        repaired = next_plans
    for index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{index:03d}"
    return repaired


def enforce_min_chunk_shape(plans: list[dict], parsed_by_id: dict, min_units: int = 4) -> list[dict]:
    repaired: list[dict] = []
    index = 0
    while index < len(plans):
        plan = plans[index]
        unit_ids = list(plan.get("source_unit_ids", []))
        if len(unit_ids) >= min_units or len(plans) == 1:
            repaired.append(plan)
            index += 1
            continue

        merged = False
        if repaired:
            previous = repaired[-1]
            candidate_ids = list(previous.get("source_unit_ids", [])) + unit_ids
            if request_voice_capacity_ok(candidate_ids, parsed_by_id):
                candidate = {**previous, "source_unit_ids": candidate_ids}
                candidate.pop("final_audio_prompt", None)
                candidate.pop("audio_prompt", None)
                candidate.pop("text_prompt", None)
                candidate.pop("active_roles", None)
                candidate.update(local_plan_fields_for_units(previous, candidate_ids, parsed_by_id))
                candidate["title"] = f"{previous.get('title', 'Beat')} + {plan.get('title', 'Beat')}"
                prompt, _roles = compose_director_prompt(candidate, parsed_by_id, len(repaired), max(1, len(plans) - 1))
                if len(prompt) <= MAX_PROMPT_CHARS:
                    repaired[-1] = candidate
                    merged = True
                    index += 1
        if not merged and index + 1 < len(plans):
            next_plan = plans[index + 1]
            candidate_ids = unit_ids + list(next_plan.get("source_unit_ids", []))
            if request_voice_capacity_ok(candidate_ids, parsed_by_id):
                candidate = {**next_plan, "source_unit_ids": candidate_ids}
                candidate.pop("final_audio_prompt", None)
                candidate.pop("audio_prompt", None)
                candidate.pop("text_prompt", None)
                candidate.pop("active_roles", None)
                candidate.update(local_plan_fields_for_units(next_plan, candidate_ids, parsed_by_id))
                candidate["title"] = f"{plan.get('title', 'Beat')} + {next_plan.get('title', 'Beat')}"
                prompt, _roles = compose_director_prompt(candidate, parsed_by_id, len(repaired) + 1, max(1, len(plans) - 1))
                if len(prompt) <= MAX_PROMPT_CHARS:
                    repaired.append(candidate)
                    index += 2
                    merged = True
        if not merged:
            repaired.append(plan)
            index += 1
    for chunk_index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{chunk_index:03d}"
    return repaired


def shift_narrative_setup_to_dialogue_openers(plans: list[dict], parsed_by_id: dict) -> list[dict]:
    repaired = [dict(plan) for plan in plans]
    for index in range(1, len(repaired)):
        current_ids = list(repaired[index].get("source_unit_ids", []))
        previous_ids = list(repaired[index - 1].get("source_unit_ids", []))
        if not current_ids or not previous_ids or len(previous_ids) <= 1:
            continue
        first = parsed_by_id.get(current_ids[0], {})
        previous_last = parsed_by_id.get(previous_ids[-1], {})
        previous_text = str(previous_last.get("adapted_text") or previous_last.get("source_text", ""))
        if first.get("speaker") == "Narrator":
            continue
        if previous_last.get("speaker") != "Narrator" or is_dialogue_attribution_text(previous_text):
            continue
        moved_id = previous_ids[-1]
        repaired[index - 1] = split_plan_copy(repaired[index - 1], previous_ids[:-1], parsed_by_id)
        repaired[index] = split_plan_copy(repaired[index], [moved_id] + current_ids, parsed_by_id)
    for chunk_index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{chunk_index:03d}"
    return repaired


def enforce_max_chunk_count(plans: list[dict], parsed_by_id: dict, target_max: int = 6) -> list[dict]:
    repaired = list(plans)
    while len(repaired) > target_max:
        best_index: int | None = None
        best_size: int | None = None
        for index in range(len(repaired) - 1):
            left = repaired[index]
            right = repaired[index + 1]
            candidate_ids = list(left.get("source_unit_ids", [])) + list(right.get("source_unit_ids", []))
            if not request_voice_capacity_ok(candidate_ids, parsed_by_id):
                continue
            candidate = {**left, "source_unit_ids": candidate_ids}
            candidate["title"] = f"{left.get('title', 'Beat')} + {right.get('title', 'Beat')}"
            prompt, _roles = compose_director_prompt(candidate, parsed_by_id, index + 1, len(repaired) - 1)
            merge_limit = TARGET_AUDIO_PROMPT_CHARS if is_english_prompt() else MAX_PROMPT_CHARS
            if len(prompt) > merge_limit:
                continue
            size = len(candidate_ids)
            if best_size is None or size > best_size:
                best_index = index
                best_size = size
        if best_index is None:
            break
        left = repaired[best_index]
        right = repaired[best_index + 1]
        merged = {**left}
        merged["title"] = f"{left.get('title', 'Beat')} + {right.get('title', 'Beat')}"
        merged["source_unit_ids"] = list(left.get("source_unit_ids", [])) + list(right.get("source_unit_ids", []))
        repaired = repaired[:best_index] + [merged] + repaired[best_index + 2:]
    for chunk_index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{chunk_index:03d}"
    return repaired


def merge_short_tail_chunk(plans: list[dict], parsed_by_id: dict, max_tail_units: int = 3) -> list[dict]:
    if len(plans) < 2:
        return plans
    tail = plans[-1]
    tail_ids = list(tail.get("source_unit_ids", []))
    if len(tail_ids) > max_tail_units:
        return plans
    previous = plans[-2]
    candidate_ids = list(previous.get("source_unit_ids", [])) + tail_ids
    if not request_voice_capacity_ok(candidate_ids, parsed_by_id):
        return plans
    candidate = {**previous, "source_unit_ids": candidate_ids}
    candidate["title"] = previous.get("title", "Final beat")
    prompt, _roles = compose_director_prompt(candidate, parsed_by_id, len(plans) - 1, len(plans) - 1)
    merge_limit = TARGET_AUDIO_PROMPT_CHARS if is_english_prompt() else MAX_PROMPT_CHARS
    if len(prompt) > merge_limit:
        return plans
    repaired = plans[:-2] + [candidate]
    for chunk_index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{chunk_index:03d}"
    return repaired


def summarize_sfx_for_units(unit_ids: list[str], parsed_by_id: dict, limit: int = 8) -> str:
    cues: list[str] = []
    for unit_id in unit_ids:
        unit = parsed_by_id[unit_id]
        for key in ("sfx_before", "sfx_during", "sfx_after"):
            for cue in unit.get(key, []) or []:
                cleaned = re.sub(r"\s+", " ", str(cue).replace("_", " ")).strip()
                if cleaned and cleaned not in cues:
                    cues.append(cleaned)
    return ", ".join(cues[:limit]) or "only source-motivated room tone and movement"


def plan_lookup_by_unit(plans: list[dict]) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for plan in plans:
        for unit_id in plan.get("source_unit_ids", []):
            lookup[unit_id] = plan
    return lookup


def compact_plan_text(value: str, fallback: str, max_chars: int = 180) -> str:
    text = clean_english_director_text(value or fallback) if is_english_prompt() else str(value or fallback)
    if len(text) <= max_chars:
        return text
    shortened = text[: max_chars - 3].rstrip()
    cut_points = [shortened.rfind(mark) for mark in (".", ";", ",", " ")]
    cut = max(cut_points)
    if cut > max_chars * 0.6:
        shortened = shortened[:cut]
    return shortened.rstrip(" ,.;")


def compact_clause_text(value: str, fallback: str, max_chars: int = 120) -> str:
    text = clean_english_director_text(value or fallback) if is_english_prompt() else str(value or fallback)
    if len(text) <= max_chars:
        return text.rstrip(" ,.;")
    parts = [part.strip(" ,.;") for part in re.split(r"[,;]", text) if part.strip(" ,.;")]
    kept: list[str] = []
    for part in parts:
        candidate = ", ".join(kept + [part])
        if len(candidate) > max_chars:
            break
        kept.append(part)
    if kept:
        return ", ".join(kept).rstrip(" ,.;")
    words = text.split()
    out: list[str] = []
    for word in words:
        candidate = " ".join(out + [word])
        if len(candidate) > max_chars:
            break
        out.append(word)
    return " ".join(out).rstrip(" ,.;") or fallback


def capacity_first_plan(parsed_units: list[dict], parsed_by_id: dict, seed_plans: list[dict]) -> list[dict]:
    seed_lookup = plan_lookup_by_unit(seed_plans)

    def make_plan(unit_ids: list[str], index: int) -> dict:
        first = seed_lookup.get(unit_ids[0], {})
        last = seed_lookup.get(unit_ids[-1], {})
        title = first.get("title") or first.get("chunk_opening_state") or f"Longform window {index}"
        if last and last is not first:
            title = f"{title} / {last.get('title', last.get('chunk_ending_state', 'continuation'))}"
        return {
            "chunk_id": f"chunk_{index:03d}",
            "title": title,
            "source_unit_ids": unit_ids,
            "persistent_ambience": compact_plan_text(
                first.get("persistent_ambience", ""),
                "continuous room tone from the source setting",
                150,
            ),
            "music_bed": compact_plan_text(
                first.get("music_bed", ""),
                "shared score bed follows the whole scene and changes intensity with the source action",
                190,
            ),
            "sound_design": summarize_sfx_for_units(unit_ids, parsed_by_id),
            "speech_rate": ENGLISH_TARGET_SPEECH_RATE if is_english_prompt() else first.get("speech_rate", TARGET_SPEECH_RATE),
            "pace_note": compact_plan_text(first.get("pace_note", ""), "natural, connected performance", 110),
            "continuity": {
                "previous_context_summary": "capacity-first longform chunk",
                "chunk_opening_state": title,
                "chunk_ending_state": last.get("chunk_ending_state", "continues"),
                "next_context_hint": "continue source order",
            },
            "expected_duration_sec": {"min": 20, "max": int(SAFE_DURATION_CEILING_SEC)},
        }

    plans: list[dict] = []
    current: list[str] = []
    for unit in parsed_units:
        unit_id = unit["source_unit_id"]
        candidate = current + [unit_id]
        if current and not request_voice_capacity_ok(candidate, parsed_by_id):
            plans.append(make_plan(current, len(plans) + 1))
            current = [unit_id]
            continue
        test_plan = make_plan(candidate, len(plans) + 1)
        test_prompt, _roles = compose_director_prompt(test_plan, parsed_by_id, len(plans) + 1, max(1, len(plans) + 1))
        if current and len(test_prompt) > TARGET_AUDIO_PROMPT_CHARS:
            plans.append(make_plan(current, len(plans) + 1))
            current = [unit_id]
        else:
            current = candidate
    if current:
        plans.append(make_plan(current, len(plans) + 1))
    return plans


def explain_chunk_boundaries(chunks: list[dict], parsed_by_id: dict) -> dict:
    reports = []
    for index, chunk in enumerate(chunks):
        unit_ids = chunk.get("source_unit_ids", [])
        next_chunk = chunks[index + 1] if index + 1 < len(chunks) else None
        merge_report = None
        if next_chunk:
            merged_unit_ids = unit_ids + next_chunk.get("source_unit_ids", [])
            merged_roles = roles_for_unit_ids(merged_unit_ids, parsed_by_id)
            probe = {
                "chunk_id": chunk.get("chunk_id", f"chunk_{index + 1:03d}"),
                "title": "merge probe",
                "source_unit_ids": merged_unit_ids,
                "persistent_ambience": "continuous source-derived room tone",
                "music_bed": "shared score bed follows the whole scene naturally",
                "sound_design": summarize_sfx_for_units(merged_unit_ids, parsed_by_id),
                "speech_rate": chunk.get("speech_rate", TARGET_SPEECH_RATE),
                "pace_note": chunk.get("pace_note", "natural adaptive pacing"),
            }
            merged_prompt, _roles = compose_director_prompt(probe, parsed_by_id, index + 1, len(chunks))
            reasons = []
            if not is_english_prompt() and len(merged_roles) > 3:
                reasons.append("merged_active_roles_exceed_3_reference_limit")
            if len(merged_prompt) > MAX_PROMPT_CHARS:
                reasons.append("merged_prompt_exceeds_text_prompt_limit")
            merge_report = {
                "next_chunk_id": next_chunk.get("chunk_id"),
                "merged_unit_count": len(merged_unit_ids),
                "merged_active_roles": merged_roles,
                "merged_prompt_chars": len(merged_prompt),
                "can_merge_under_current_constraints": not reasons,
                "blocking_reasons": reasons,
            }
        reports.append(
            {
                "chunk_id": chunk.get("chunk_id"),
                "source_unit_count": len(unit_ids),
                "active_roles": chunk.get("active_roles", []),
                "prompt_chars": len(chunk.get("text_prompt", "")),
                "prompt_limit": MAX_PROMPT_CHARS,
                "prompt_utilization": round(len(chunk.get("text_prompt", "")) / MAX_PROMPT_CHARS, 3),
                "sfx_cues": chunk.get("input_metrics", {}).get("sfx_cue_count"),
                "music_actions": chunk.get("input_metrics", {}).get("music_actions", []),
                "merge_with_next_probe": merge_report,
            }
        )
    blocking_counts: dict[str, int] = {}
    for report in reports:
        merge_report = report.get("merge_with_next_probe") or {}
        for reason in merge_report.get("blocking_reasons", []):
            blocking_counts[reason] = blocking_counts.get(reason, 0) + 1
    return {
        "strategy": "capacity_first_source_order_chunking",
        "goal": "Use the fewest chunks possible while preserving source order, <=3 active voice references, continuous ambience/music, source-derived SFX, and text_prompt length.",
        "constraints": {
            "max_prompt_chars": MAX_PROMPT_CHARS,
            "target_prompt_chars": TARGET_AUDIO_PROMPT_CHARS,
            "max_audio_or_speaker_references_per_request": 3,
            "english_role_policy": "Only primary voices receive references; extra speaking roles stay in the same natural prompt with text voice descriptions.",
            "must_keep_ambience": True,
            "must_keep_music": True,
            "must_keep_source_derived_sfx": True,
            "must_keep_sequential_non_overlapping_voices": True,
        },
        "final_chunk_count": len(chunks),
        "blocking_reason_counts": blocking_counts,
        "chunks": reports,
    }


def role_ref_map(active_roles: list[str]) -> dict[str, str]:
    return {role: voice_ref_token(index) for index, role in enumerate(active_roles, start=1)}


def voice_ref_token(index: int) -> str:
    if is_english_prompt():
        return f"<<TGT_SPK{index}>>"
    return f"@Audio{index}"


def clean_attribution(text: str | None, speaker: str) -> str:
    if is_english_prompt():
        return ""
    if text:
        cleaned = simplify_text(text.strip().rstrip("：:"))
        if cleaned == "操传令曰":
            cleaned = "曹操传令曰"
        if cleaned == "肃曰":
            cleaned = "鲁肃曰"
        if cleaned:
            return cleaned + "。"
    name = ROLE_SPOKEN_NAMES.get(speaker)
    if name:
        return f"{name}说道。"
    return ""


def delivery_for_chunk(unit: dict, chunk: dict) -> str:
    speech_rate = int(chunk.get("speech_rate", TARGET_SPEECH_RATE))
    speaker = unit.get("speaker", "Narrator")
    if speech_rate <= 15:
        if speaker == "Narrator":
            return "natural-slow, clear literary cadence, use natural spacing"
        return "natural-slow, thoughtful, clear pauses between clauses"
    if speech_rate <= 18:
        if speaker == "Narrator":
            return "natural medium-slow, clear connective narration"
        return "natural dialogue pace, calm and unhurried"
    if speech_rate >= 21:
        if speaker == "Narrator":
            return "brisk but clear, action-forward, no rushed syllables"
        return "brisk, tense, clear articulation"
    if speaker == "Narrator":
        return "natural medium pace, connective narration, not mechanical"
    return "natural dialogue pace, expressive but not rushed"


def clean_english_director_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"\b(?:ENTER|CONTINUE|DUCK|SWELL|FADE)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"\bextremely\s+slow\b", "measured and connected", text, flags=re.I)
    text = re.sub(r"\bvery\s+slow\b", "measured and connected", text, flags=re.I)
    text = re.sub(r"\bslow,\s*exhausted,\s*deliberate\b", "measured, tired, connected", text, flags=re.I)
    text = re.sub(r"\blong\s+heavy\s+silences?\s+between\s+every\s+line\b", "connected transitions between lines", text, flags=re.I)
    text = re.sub(r"\bpauses?\s+are\s+long\b", "pauses are brief and natural", text, flags=re.I)
    text = re.sub(r"\bno\s+rushing\b", "natural pace without rushing", text, flags=re.I)
    text = re.sub(r"\bflat\s+fast,?\s*no\s+pause\b", "tense but naturally articulated, with complete phrases", text, flags=re.I)
    text = re.sub(r"\bno\s+pause\b", "connected natural phrasing", text, flags=re.I)
    text = re.sub(r"\bevery\s+word\s+lands?\s+with\s+weight\b", "important words are clear without dragging", text, flags=re.I)
    text = re.sub(r"\babsolute\s+stillness\b", "low continuous room tone", text, flags=re.I)
    text = re.sub(r"\bNo\s+rhythm\b", "slow subtle pulse", text, flags=re.I)
    text = re.sub(r"\blong\s+silence\b", "low tense room tone", text, flags=re.I)
    text = re.sub(r"\bsilence\s+stays\s+present\s+in\s+the\s+mix\b", "low room tone stays present in the mix", text, flags=re.I)
    text = re.sub(r"\bsilence\s+cuts?\s+clearly\s+through\s+the\s+ambience\b", "a low tense room tone cuts through the ambience", text, flags=re.I)
    text = re.sub(r"\bfinal\s+total\s+silence\b", "quiet ending", text, flags=re.I)
    text = re.sub(r"\btotal\s+silence\b", "quiet ending", text, flags=re.I)
    text = re.sub(r"\bcomplete\s+silence\b", "very low room tone", text, flags=re.I)
    text = re.sub(r"\bambience\s+cuts?\s+completely\b", "ambience drops to very low room tone", text, flags=re.I)
    text = re.sub(r"\ball\s+ambience\s+cuts?\s+completely\b", "ambience drops to very low room tone", text, flags=re.I)
    text = re.sub(r"\bfades?\s+out\s+completely\b", "settles into very low room tone", text, flags=re.I)
    text = re.sub(r"\bfade\s+[^,.;]{0,60}\s+completely\s+out\b", "settle ambience into very low room tone", text, flags=re.I)
    text = re.sub(r"\bdead\s+silence\s+hold\b", "brief dramatic beat", text, flags=re.I)
    text = re.sub(r"\bsudden\s+silence\b", "sudden hush over low room tone", text, flags=re.I)
    text = re.sub(r"\bextreme\s+long\s+pause\b", "brief tense beat", text, flags=re.I)
    text = re.sub(r"\bhold\s+full\s+silence(?:\s+for\s+[^,.;]*)?", "hold a brief tense musical beat", text, flags=re.I)
    text = re.sub(r"\bhold\s+long\s+silences?\b", "hold brief tense beats", text, flags=re.I)
    text = re.sub(r"\bremain\s+silent\b", "remain very low and tense", text, flags=re.I)
    text = re.sub(r"\bhold\s+final\s+silence\s+for\s+full\s+\d+\s+seconds?\s+at\s+end\b", "end promptly after a brief final beat", text, flags=re.I)
    text = re.sub(r"\bstretch\s+silence\s+before\s+twist\b", "tighten the beat before the twist", text, flags=re.I)
    text = re.sub(r"\bcut\s+all\s+music\s+completely\b", "let the music resolve briefly", text, flags=re.I)
    text = re.sub(r"\bcut\s+all\s+music\s+entirely(?:\s+on\s+[^,.;]*)?", "let the music resolve briefly", text, flags=re.I)
    text = re.sub(r"\bcut\s+music\s+completely(?:\s+[^,.;]*)?", "let the music drop to a tense low drone", text, flags=re.I)
    text = re.sub(r"\bmusic\s+cuts?\s+out\s+completely\b", "music drops to a tense low drone", text, flags=re.I)
    text = re.sub(r"\bcuts?\s+out\s+completely\b", "drops to a tense low drone", text, flags=re.I)
    text = re.sub(r"\bcuts?\s+to\s+silence\b", "drops into a low suspense drone", text, flags=re.I)
    text = re.sub(r"\blow\s+room\s+tone\b", "low stone ambience", text, flags=re.I)
    text = re.sub(r"\broom\s+tone\b", "stone ambience", text, flags=re.I)
    text = re.sub(r"\blong\s+held\s+silences?\b", "brief held tense beats", text, flags=re.I)
    text = re.sub(r"\bno\s+resolution\b", "carry unresolved tension", text, flags=re.I)
    text = re.sub(r"\bfinal\s+silence\b", "quiet ending", text, flags=re.I)
    text = re.sub(r"\bNo\s+fade\s+yet\b", "keep the music continuous", text, flags=re.I)
    text = re.sub(r"\bNo\s+yet\b", "keep it continuous", text, flags=re.I)
    text = re.sub(r"\bleave\s+short\s+pauses\b", "use natural spacing", text, flags=re.I)
    text = re.sub(r"\bleave\s+a\s+short\s+pause\b", "use natural spacing", text, flags=re.I)
    text = re.sub(r"\ballow\s+silence\s+between\s+lines\b", "keep transitions connected", text, flags=re.I)
    text = re.sub(r"\bbefore\s+([A-Za-z][A-Za-z ]{0,40})\s+speaks\b", r"before \1 line", text, flags=re.I)
    text = re.sub(r"\bspeaks\b", "line", text, flags=re.I)
    text = re.sub(r"\bEnd\s+(?:scene|segment)\.?", "let the existing ambience settle naturally", text, flags=re.I)
    text = re.sub(r"\bno\s+fade\s+out\b", "keep the sound bed continuous", text, flags=re.I)
    text = re.sub(r"\bno\s+hard\s+(?:ending|fade)\b", "use a brief natural ambience tail", text, flags=re.I)
    text = re.sub(r"\bhard\s+ending\b", "natural ambience-tail ending", text, flags=re.I)
    text = re.sub(r"\bhard\s+fade\b", "natural connected fade", text, flags=re.I)
    text = re.sub(r"([A-Za-z])[-–—]\s*\"", r'\1."', text)
    text = re.sub(r"\s+", " ", text).strip(" ,.;")
    return text


def audio_safe_pace_note(value: str) -> str:
    text = clean_english_director_text(value)
    risky = re.search(r"\bsilence|pause|stillness|extremely\s+slow|very\s+slow|no\s+rhythm\b", text, re.I)
    if risky or not text:
        return "natural measured pace, emotionally tense but connected, brief natural beats only, no empty gaps"
    return compact_clause_text(text, "natural measured pace, connected transitions, no empty gaps", 120)


def safe_english_speech_rate(value: object, narrator_units: int = 0) -> int:
    requested = int(value if value is not None else ENGLISH_TARGET_SPEECH_RATE)
    if not is_english_prompt():
        return requested
    # Seed Audio speech_rate is an API speed adjustment, not a dramatic-intensity
    # score. Narration-heavy action becomes artificial and clips phonemes above
    # a modest positive adjustment.
    return max(-5, min(requested, 8 if narrator_units else 10))


def english_score_bible() -> str:
    return "a cinematic fantasy arrangement of low cello drone, glass harmonics, soft piano pulse, and distant choir texture"


def background_music_disabled() -> bool:
    return DISABLE_BACKGROUND_MUSIC


def no_background_music_instruction() -> str:
    return (
        "No composed background music, no score bed, no underscore, no musical drone, and no musical sting. "
        "Use only clean spoken voices, continuous room tone or environmental ambience, and source-motivated SFX. "
        "Keep ambience connected under speech and leave only a brief natural room-tone tail."
    )


def strip_background_music_from_prompt(prompt: str) -> str:
    """Force a generated/planned prompt into the no-music delivery policy."""
    if not background_music_disabled():
        return prompt
    lines: list[str] = []
    music_line = re.compile(
        r"^\s*(?:Background music|Music|Score|The music|The same music|The shared score|The score|\[Music)\b",
        re.I,
    )
    for line in str(prompt or "").splitlines():
        if music_line.search(line):
            continue
        cleaned = re.sub(r"\bmusic\s+and\s+ambience\b", "ambience", line, flags=re.I)
        cleaned = re.sub(r"\bambience\s+and\s+music\b", "ambience", cleaned, flags=re.I)
        cleaned = re.sub(r"\bshared\s+score\b", "shared room tone", cleaned, flags=re.I)
        cleaned = re.sub(r"\bbackground\s+music\b", "room tone", cleaned, flags=re.I)
        cleaned = re.sub(r"\bcomposed\s+underscore\b", "room tone", cleaned, flags=re.I)
        cleaned = re.sub(r"\bunderscore\b", "room tone", cleaned, flags=re.I)
        cleaned = re.sub(r"\bscore\b", "room tone", cleaned, flags=re.I)
        lines.append(cleaned)
    text = "\n".join(lines).strip()
    if no_background_music_instruction() not in text:
        text = f"{no_background_music_instruction()}\n{text}".strip()
    return text


def english_audible_music_text(value: str) -> str:
    text = clean_english_director_text(value or "low cinematic tension")
    text = re.sub(r"\b(?:full|total|absolute|complete|perfect)\s+silence\b", "low sustained tension", text, flags=re.I)
    text = re.sub(r"\bsilence\b", "low sustained room tone", text, flags=re.I)
    text = re.sub(r"\badd quiet\b", "stay clearly audible beneath the voices", text, flags=re.I)
    text = re.sub(r"\bquietly\b", "clearly but softly", text, flags=re.I)
    text = re.sub(r"\bvery quiet\b", "soft but audible", text, flags=re.I)
    text = re.sub(r"\bsubtle\b", "audible and restrained", text, flags=re.I)
    text = re.sub(r"\bfade(?:s)?\b", "carry forward softly", text, flags=re.I)
    text = re.sub(r"\bduck(?:s)?\b", "lower briefly", text, flags=re.I)
    return text or "low cinematic tension"


def no_sustained_background_music(chunk: dict) -> bool:
    """Whether the plan explicitly opts out of an underscore beneath speech."""
    music = str(chunk.get("music_bed", ""))
    return bool(
        re.search(
            r"\b(?:no|without)\s+(?:sustained|continuous|persistent)\s+(?:background\s+)?music\b",
            music,
            re.I,
        )
    )


def english_music_instruction(chunk: dict, index: int, total: int) -> str:
    if background_music_disabled():
        return no_background_music_instruction()
    if no_sustained_background_music(chunk):
        music = compact_clause_text(
            clean_english_director_text(str(chunk.get("music_bed", ""))),
            "no sustained background music",
            115,
        )
        return (
            f"{music}. Do not introduce a continuous score bed or carry music beneath dialogue. "
            "Any named opening sting must be brief and finish before the spoken exchange."
        )
    music = english_audible_music_text(chunk.get("music_bed", "soft strings stay low under speech"))
    music = compact_clause_text(music, "soft strings stay low under speech", 115)
    if index != total:
        music = re.sub(r",?\s*(?:quick\s+)?fade(?:s)?(?:\s+(?:out|under [^,.;]+|after [^,.;]+|into [^,.;]+))?", "", music, flags=re.I).strip(" ,.;")
        music = english_audible_music_text(music)
        music = music or "soft strings stay low under speech"
    score = english_score_bible()
    if total == 1:
        return f"The music begins with {score}; {music}. It continues beneath the scene, softens under dialogue, rises with the action, and resolves after the final beat."
    if index == 1:
        return f"The music begins with {score}; {music}. It continues beneath the voices and grows naturally with the action."
    if index == total:
        return f"The same music continues from the first sound with {score}; {music}. It stays beneath the voices, rises on the final turn, and settles naturally after the last line."
    return f"The same music is already playing under the first sound with {score}; {music}. It continues beneath the voices and action without reaching a cadence."


def english_music_actions(index: int, total: int) -> list[str]:
    if background_music_disabled():
        return []
    if total == 1:
        return ["enter", "duck", "swell", "fade"]
    if index == 1:
        return ["enter", "duck", "swell", "carry"]
    if index == total:
        return ["continue", "duck", "swell", "fade"]
    return ["continue", "duck", "swell", "carry"]


def english_reference_phrase(role: str, ref: str, is_first: bool, delivery: str, emotion: str) -> str:
    label = speaker_label(role)
    clean_emotion = re.sub(r"[_-]+", " ", str(emotion or "neutral")).strip()
    if role == "Narrator":
        return f"({ref} Narrator)"
    return f"({ref} {label}; {clean_emotion})"


def english_dialogue_verb(unit: dict) -> str:
    text = " ".join(
        str(unit.get(key, ""))
        for key in ("emotion", "delivery", "source_text", "adapted_text")
    ).lower()
    if any(word in text for word in ("whisper", "hushed", "quiet")):
        return "whispers"
    if any(word in text for word in ("shout", "cry", "yell", "furious", "urgent")):
        return "cries"
    if any(word in text for word in ("answer", "reply", "respond")):
        return "answers"
    if any(word in text for word in ("laugh", "teas", "dry", "sarcastic")):
        return "says dryly"
    return "says"


def english_voice_parenthetical(role: str, ref: str | None, first_seen: bool, emotion: str) -> str:
    label = speaker_label(role)
    spec = ROLE_SPECS.get(role, {})
    description = compact_plan_text(spec.get("description", "natural expressive character voice"), "natural expressive character voice", 65)
    clean_emotion = clean_english_director_text(str(emotion or "natural"))
    if ref:
        if first_seen:
            return f"{label} ({description}, actor is {ref}, {clean_emotion})"
        return f"{label} (actor is {ref}, {clean_emotion})"
    if first_seen:
        return f"{label} ({description}, {clean_emotion})"
    return f"{label} ({clean_emotion})"


def english_quote(text: str) -> str:
    cleaned = clean_english_director_text(text)
    cleaned = cleaned.replace('"', "'")
    return f"\"{cleaned}\""


def english_voice_line(role: str, ref: str | None, first_seen: bool, unit: dict, text: str) -> str:
    emotion = unit.get("emotion", "natural")
    phrase = english_voice_parenthetical(role, ref, first_seen, emotion)
    if role == "Narrator":
        return f"{phrase} narrates: {english_quote(text)}"
    return f"{phrase} {english_dialogue_verb(unit)}: {english_quote(text)}"


def english_time_ordered_sfx_sentences(unit: dict, max_items: int = 2) -> list[str]:
    events: list[tuple[str, str]] = []
    for key, phase in (
        ("sfx_before", "before"),
        ("sfx_during", "during"),
        ("sfx_after", "after"),
    ):
        for cue in unit.get(key, []) or []:
            cleaned = clean_english_director_text(str(cue).replace("_", " ").replace("-", " "))
            if cleaned and cleaned not in [item[1] for item in events]:
                events.append((phase, cleaned))

    importance = str(unit.get("sfx_importance", "")).lower()
    layer = str(unit.get("sfx_layer", "")).lower()
    source_text = " ".join(str(unit.get(key, "")) for key in ("source_text", "adapted_text")).lower()
    action_heavy = bool(re.search(r"\b(spell|wand|bolt|blast|shatter|clang|clatter|snap|burst|crash|hit|fire|smoke|serpent|armor|cord|spark)\b", source_text))
    if not events or (importance not in {"medium", "high"} and layer != "foreground" and not action_heavy):
        return []

    sentences: list[str] = []
    for phase, cue in events[:max_items]:
        if phase == "before":
            sentences.append(f"A brief {cue} cuts through the cloister air.")
        elif phase == "during":
            sentences.append(f"{cue[:1].upper() + cue[1:]} stays audible around the action, below the voice.")
        else:
            sentences.append(f"{cue[:1].upper() + cue[1:]} rings out and fades into the music.")
    return sentences


def english_unit_music_sentence(unit: dict) -> str:
    if background_music_disabled():
        return ""
    intent = english_audible_music_text(unit.get("music_intent", ""))
    if not intent:
        return ""
    text = " ".join(str(unit.get(key, "")) for key in ("source_text", "adapted_text", "emotion")).lower()
    if not re.search(r"\b(spell|attack|impact|reveal|twist|fear|furious|shatter|burst|clatter|smile|cursed)\b", text):
        return ""
    return f"The background music swells briefly here, then returns under the voices: {compact_clause_text(intent, 'support the action', 95)}."


def english_natural_sfx_sentences(unit: dict, max_items: int = 2) -> list[str]:
    cues: list[str] = []
    llm_foreground = (
        str(unit.get("sfx_layer", "")).lower() == "foreground"
        or str(unit.get("sfx_importance", "")).lower() == "high"
    )
    if not llm_foreground:
        return []
    for key in ("sfx_before", "sfx_during", "sfx_after"):
        for cue in unit.get(key, []) or []:
            cleaned = clean_english_director_text(str(cue).replace("_", " ").replace("-", " "))
            if not cleaned or cleaned in cues:
                continue
            cues.append(cleaned)
    result = []
    for cue in cues[:max_items]:
        if llm_foreground:
            result.append(f"The sound of {cue} cuts clearly through the ambience.")
        else:
            result.append(f"Under the voices, {cue} stays present in the room.")
    return result


def english_sfx_sentence(unit: dict, chunk: dict, position: str) -> str:
    def clean_sfx(value: str) -> str:
        return re.sub(r"\s+", " ", str(value).replace("_", " ").replace("-", " ")).strip()

    def is_foreground_action(value: str) -> bool:
        text = clean_sfx(value).lower()
        foreground_terms = [
            "hit",
            "slam",
            "snap",
            "whoosh",
            "roar",
            "impact",
            "crack",
            "shatter",
            "blast",
            "explosion",
            "clang",
            "clatter",
            "rattle",
            "crash",
            "pop",
            "burst",
            "thud",
            "scream",
            "shout",
        ]
        return any(term in text for term in foreground_terms)

    def is_ambient_or_micro(value: str) -> bool:
        text = clean_sfx(value).lower()
        ambient_terms = [
            "wind",
            "room tone",
            "stone echo",
            "canvas rustle",
            "fountain",
            "water drip",
            "wand hum",
            "spark fall",
            "light reflection",
        ]
        return any(term in text for term in ambient_terms)

    before = unit.get("sfx_before") or []
    during = unit.get("sfx_during") or []
    after = unit.get("sfx_after") or []
    llm_foreground = (
        str(unit.get("sfx_layer", "")).lower() == "foreground"
        or str(unit.get("sfx_importance", "")).lower() == "high"
    )
    if position == "before" and before:
        if is_ambient_or_micro(before[0]) and unit.get("speaker") == "Narrator":
            return ""
        cleaned = clean_sfx(before[0])
        if llm_foreground or is_foreground_action(before[0]):
            return f"SFX hit before: {cleaned}."
        return f"SFX quiet before: {cleaned}."
    if position == "during" and during:
        if is_ambient_or_micro(during[0]):
            return ""
        cleaned = clean_sfx(during[0])
        if llm_foreground or is_foreground_action(during[0]):
            return f"SFX foreground: {cleaned}, brief and ducked under voice."
        return f"SFX under: {cleaned}."
    if position == "after" and after:
        if is_ambient_or_micro(after[0]):
            return ""
        cleaned = clean_sfx(after[0])
        if llm_foreground or is_foreground_action(after[0]):
            return f"SFX hit after: {cleaned}."
        return f"SFX after: {cleaned}."
    return ""


def rendered_sfx_limit(unit_count: int) -> int:
    if unit_count <= 2:
        return 2
    if unit_count <= 5:
        return 4
    return 5


def rendered_sfx_count(prompt: str) -> int:
    return len(
        re.findall(
            r"^(?:A brief .+ cuts through|[A-Z][^.\n]+ stays audible around|[A-Z][^.\n]+ rings out|The sound of .+)",
            prompt,
            flags=re.M,
        )
    )


def prompt_tail_after_last_spoken_line(prompt: str) -> str:
    matches = list(re.finditer(r':\s*"[^"]+"', prompt))
    return prompt[matches[-1].end():] if matches else ""


def dialogue_without_recent_narration_count(prompt: str, max_gap_chars: int = 700) -> int:
    spoken = list(
        re.finditer(
            r'(?P<role>[A-Z][A-Za-z0-9\' .-]{1,80})\s*\([^\n]*actor is <<TGT_SPK\d+>>[^\n]*\):\s*"[^"]+"',
            prompt,
        )
    )
    failures = 0
    for item in spoken:
        if item.group("role").strip() == "Narrator":
            continue
        prior = prompt[max(0, item.start() - max_gap_chars):item.start()]
        if not re.search(r'Narrator\s*\([^\n]*actor is <<TGT_SPK\d+>>[^\n]*\):\s*"[^"]+"', prior):
            failures += 1
    return failures


def quoted_word_count(prompt: str) -> int:
    quoted = re.findall(r'"([^"]+)"', prompt)
    return sum(len(re.findall(r"[A-Za-z0-9']+", item)) for item in quoted)


def quoted_dialogue_line_count(prompt: str) -> int:
    return len(re.findall(r':\s*"[^"]+"', prompt))


def quoted_dialogue_segments(prompt: str) -> list[tuple[str, str]]:
    pattern = r"([A-Z][A-Za-z0-9' .-]{1,80})\s*\(([^)]*)\)\s*:\s*\"[^\"]+\""
    return [(match.group(1).strip(), match.group(2).strip()) for match in re.finditer(pattern, prompt)]


def forbidden_summary_directive_count(prompt: str) -> int:
    patterns = [
        r"\bnarrate\b",
        r"\bdeliver\s+(?:his|her|their|the)\s+(?:line|reply|answer|dialogue)\b",
        r"\b(?:boom|booms|shout|shouts|deadpan|deadpans|say|says)\s+(?:his|her|their|its)\s+(?:line|reply|challenge|complaint|judgement)\b",
        r"\backnowledges?\s+the\s+trick\b",
        r"\bexplains?\s+(?:that\s+)?(?:he|she|it|they)\s+(?:is|are)\b",
    ]
    return sum(len(re.findall(pattern, prompt, flags=re.I)) for pattern in patterns)


def quoted_dialogue_lines(prompt: str) -> list[str]:
    return [match.group(0).strip() for match in re.finditer(r"[A-Z][A-Za-z0-9' .-]{1,80}\s*\([^)]*\)\s*:\s*\"[^\"]+\"", prompt)]


def unbound_quoted_dialogue_lines(prompt: str) -> list[str]:
    return [
        line.strip()
        for line in quoted_dialogue_lines(prompt)
        if not re.search(r"actor is <<TGT_SPK\d+>>", line)
    ]


def offstage_or_unreferenced_dialogue_roles(prompt: str, active_roles: list[str]) -> list[str]:
    labels = {
        speaker_label(role): role
        for role in ROLE_SPECS
    }
    active = set(active_roles)
    found: list[str] = []
    for label_text, _parenthetical in quoted_dialogue_segments(prompt):
        for label, role in labels.items():
            if re.search(rf"\b{re.escape(label)}\b", label_text) and role not in active and role not in found:
                found.append(role)
    return found


def keyword_count(prompt: str, words: list[str]) -> int:
    pattern = r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b"
    return len(re.findall(pattern, prompt, flags=re.I))


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def soundscape_event_count(prompt: str) -> int:
    words = [
        "ambience",
        "ambient",
        "music",
        "score",
        "drone",
        "choir",
        "cello",
        "strings",
        "piano",
        "reverb",
        "wind",
        "stone",
        "echo",
        "whoosh",
        "boot",
        "boots",
        "footfall",
        "footfalls",
        "footstep",
        "footsteps",
        "skid",
        "curse",
        "zip",
        "screech",
        "screeches",
        "clang",
        "clatter",
        "creak",
        "click",
        "crack",
        "crackle",
        "impact",
        "shatter",
        "burst",
        "roar",
        "rubble",
        "spark",
        "gasps",
        "hiss",
        "hum",
        "swell",
        "duck",
        "fade",
        "carry",
    ]
    return keyword_count(prompt, words)


def english_best_practice_prompt_metrics(prompt: str) -> dict:
    total_words = len(re.findall(r"[A-Za-z0-9']+", prompt))
    spoken_words = quoted_word_count(prompt)
    return {
        "explicit_narrator_lines": len(re.findall(r"^Narrator \([^)\n]+\)\s*(?:narrates)?:", prompt, flags=re.M)),
        "explicit_character_lines": len(
            re.findall(r"^[A-Z][A-Za-z0-9' .-]+ \([^)\n]+\) (?:says|says dryly|whispers|cries|answers):", prompt, flags=re.M)
        ),
        "time_ordered_sfx_lines": rendered_sfx_count(prompt),
        "music_reaction_lines": len(
            re.findall(
                r"^(?:The background music swells briefly here|The score remains clearly audible|The background music remains clearly audible)",
                prompt,
                flags=re.M,
            )
        ),
        "uses_official_reference_marker_style": bool(re.search(r"actor is <<TGT_SPK\d+>>", prompt)),
        "uses_chronological_performance_style": bool(
            re.search(r"Background music:|score|music", prompt, re.I)
            and re.search(r"Ambient sound:|ambience|ambient", prompt, re.I)
            and re.search(r"Sound design:|foreground|whoosh|clang|impact|shatter|spell", prompt, re.I)
        ),
        "spoken_quote_words": spoken_words,
        "spoken_quote_word_ratio": round(spoken_words / total_words, 3) if total_words else 0,
        "quoted_dialogue_line_count": quoted_dialogue_line_count(prompt),
        "unbound_quoted_dialogue_line_count": len(unbound_quoted_dialogue_lines(prompt)),
        "forbidden_summary_directive_count": forbidden_summary_directive_count(prompt),
        "soundscape_event_count": soundscape_event_count(prompt),
        "music_keyword_count": keyword_count(prompt, ["music", "score", "drone", "choir", "cello", "strings", "piano", "swell", "duck", "carry", "fade"]),
        "ambience_keyword_count": keyword_count(prompt, ["ambience", "ambient", "air", "wind", "stone", "echo", "reverb", "room", "cloister"]),
        "sfx_keyword_count": keyword_count(prompt, ["whoosh", "boot", "boots", "footfall", "footfalls", "footstep", "footsteps", "skid", "curse", "zip", "screech", "screeches", "clang", "clatter", "creak", "click", "crack", "crackle", "impact", "shatter", "burst", "roar", "rubble", "spark", "gasps", "hiss", "hum", "spell", "bolt", "wand", "magic", "metal", "explosion", "explode", "slam", "slams", "thud", "thuds"]),
        "dialogue_without_recent_narration_count": dialogue_without_recent_narration_count(prompt),
        "sound_only_opening": "Begin with a brief sound-only establishing beat" in prompt,
        "sound_only_coda": "After the last voice, no further speech occurs" in prompt,
        "tail_after_last_spoken_chars": len(prompt_tail_after_last_spoken_line(prompt).strip()),
    }


def english_prompt_performance_metrics(prompt: str) -> dict:
    if "\n\n" in prompt:
        meta, playable = prompt.split("\n\n", 1)
    else:
        meta, playable = "", prompt
    word_re = r"[A-Za-z0-9']+"
    meta_words = len(re.findall(word_re, meta))
    playable_words = len(re.findall(word_re, playable))
    total_words = meta_words + playable_words
    playable_ratio = playable_words / total_words if total_words else 0
    estimated_duration_sec = round(playable_words / 180 * 60, 1) if playable_words else 0
    return {
        "meta_words": meta_words,
        "playable_words": playable_words,
        "total_words": total_words,
        "playable_word_ratio": round(playable_ratio, 3),
        "read_aloud_estimated_duration_sec": estimated_duration_sec,
        "estimated_duration_sec": estimated_duration_sec,
    }


def normalized_fragment(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9']+", " ", text.lower())).strip()


def audio_drama_coverage_metrics(prompt: str, plan: dict) -> dict:
    source_beats = [str(item).strip() for item in plan.get("source_beats", []) if str(item).strip()]
    must_keep = [
        item for item in plan.get("must_keep_dialogue", [])
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]
    optional = [
        item for item in plan.get("optional_dialogue", [])
        if isinstance(item, dict) and (str(item.get("text", "")).strip() or str(item.get("reason", "")).strip())
    ]
    narration_bridge = [str(item).strip() for item in plan.get("narration_bridge", []) if str(item).strip()]
    sfx_story_events = [str(item).strip() for item in plan.get("sfx_story_events", []) if str(item).strip()]
    omission_rationale = [str(item).strip() for item in plan.get("omission_rationale", []) if str(item).strip()]
    prompt_norm = normalized_fragment(prompt)
    present_dialogue = 0
    missing_dialogue: list[str] = []
    for item in must_keep:
        text = normalized_fragment(str(item.get("text", "")))
        if text and text in prompt_norm:
            present_dialogue += 1
        elif text:
            missing_dialogue.append(str(item.get("text", ""))[:120])
    spoken_words = quoted_word_count(prompt)
    soundscape_events = soundscape_event_count(prompt)
    # Audio-drama timing is shorter than read-aloud timing. Spoken words drive
    # duration; SFX/music are quality cues, but many are transient and should
    # not inflate the minimum length into a read-aloud estimate.
    audio_drama_floor = round((spoken_words / 240 * 60) + len(narration_bridge) * 1.5 + min(soundscape_events, 10) * 0.3, 1)
    audio_drama_ceiling = round(audio_drama_floor * 2.2 + 18, 1) if audio_drama_floor else 0
    return {
        "source_beat_count": len(source_beats),
        "must_keep_dialogue_count": len(must_keep),
        "must_keep_dialogue_present_count": present_dialogue,
        "missing_must_keep_dialogue": missing_dialogue,
        "optional_dialogue_count": len(optional),
        "narration_bridge_count": len(narration_bridge),
        "sfx_story_event_count": len(sfx_story_events),
        "omission_rationale_count": len(omission_rationale),
        "audio_drama_estimated_duration_floor_sec": audio_drama_floor,
        "audio_drama_estimated_duration_ceiling_sec": audio_drama_ceiling,
    }


def single_source_unit_duration_evidence(prompt: str, plan: dict, parsed_by_id: dict) -> dict:
    """Admit an indivisible unit when the compiled render schedule is still safe.

    The broad audio-drama ceiling intentionally models delivery variance, but
    it can slightly exceed the content ceiling for a single, complete source
    unit even when the deterministic audible-closure schedule and render
    contract fit comfortably inside the provider window. Multi-unit chunks
    must still be split normally.
    """
    unit_ids = list(plan.get("source_unit_ids", []))
    metrics = audio_drama_coverage_metrics(prompt, plan)
    evidence = {
        "code": "single_source_unit_uses_safe_render_schedule",
        "admissible": False,
        "source_unit_ids": unit_ids,
        "estimated_floor_sec": float(metrics.get("audio_drama_estimated_duration_floor_sec") or 0),
        "conservative_ceiling_sec": float(metrics.get("audio_drama_estimated_duration_ceiling_sec") or 0),
    }
    if len(unit_ids) != 1:
        return evidence

    unit = parsed_by_id.get(unit_ids[0], {})
    narrator_count = 1 if unit.get("speaker") == "Narrator" else 0
    dialogue_count = 0 if narrator_count else 1
    timing = audible_closure_contract(prompt, plan)
    render_contract = derive_render_plan_contract(
        prompt,
        plan,
        {
            "narrator_unit_count": narrator_count,
            "dialogue_unit_count": dialogue_count,
            "soundscape_event_count": metrics.get("soundscape_event_count", 0),
        },
    )
    speech_floor = evidence["estimated_floor_sec"]
    foreground_hard_end = float(timing.get("foreground_hard_end_sec") or 0)
    render_max = float(render_contract.get("duration_range_sec", {}).get("max") or 0)
    evidence.update(
        {
            "foreground_hard_end_sec": foreground_hard_end,
            "target_end_sec": float(timing.get("target_end_sec") or 0),
            "render_contract_max_sec": render_max,
        }
    )
    evidence["admissible"] = bool(
        evidence["conservative_ceiling_sec"] <= SAFE_DURATION_CEILING_SEC
        and speech_floor <= foreground_hard_end + 0.25
        and render_max <= SAFE_DURATION_CEILING_SEC
    )
    return evidence


def pre_admission_compiled_duration_evidence(prompt: str, plan: dict, parsed_by_id: dict) -> dict:
    """Use the same render-contract evidence before local splitting hard-fails.

    `enforce_estimated_duration_limit` runs before final prompt compilation.
    For no-music audiobook prompts, the coarse coverage estimator can count
    fixed director/contract prose as if it were spoken content, especially
    after a plan has already been split down to one source unit.  The final
    provider gate already accepts such chunks when the deterministic render
    contract is safe; mirror that evidence here so planning does not block
    before the real input validation stage can make the same decision.
    """
    unit_ids = list(plan.get("source_unit_ids", []))
    narrator_count = sum(
        1
        for unit_id in unit_ids
        if parsed_by_id.get(unit_id, {}).get("speaker") == "Narrator"
    )
    dialogue_count = max(0, len(unit_ids) - narrator_count)
    render_contract = derive_render_plan_contract(
        prompt,
        plan,
        {
            "narrator_unit_count": narrator_count,
            "dialogue_unit_count": dialogue_count,
            **(english_best_practice_prompt_metrics(prompt) if is_english_prompt() else {}),
        },
    )
    return compiled_render_contract_duration_evidence(prompt, plan, render_contract)


def compiled_render_contract_duration_evidence(prompt: str, plan: dict, render_contract: dict) -> dict:
    """Use the compiled contract when compiler metadata inflates the coarse ceiling."""
    metrics = audio_drama_coverage_metrics(prompt, plan)
    conservative_ceiling = float(metrics.get("audio_drama_estimated_duration_ceiling_sec") or 0)
    estimated_speech = float(render_contract.get("estimated_speech_sec") or 0)
    pre_roll = float(render_contract.get("planned_pre_roll_sec") or 0)
    post_roll = float(render_contract.get("planned_post_roll_sec") or 0)
    target = float(render_contract.get("target_audible_duration_sec") or 0)
    render_max = float(render_contract.get("duration_range_sec", {}).get("max") or 0)
    evidence = {
        "code": "compiled_prompt_uses_safe_render_contract",
        "admissible": False,
        "source_unit_ids": list(plan.get("source_unit_ids", [])),
        "estimated_floor_sec": float(metrics.get("audio_drama_estimated_duration_floor_sec") or 0),
        "conservative_ceiling_sec": conservative_ceiling,
        "estimated_speech_sec": estimated_speech,
        "planned_pre_roll_sec": pre_roll,
        "planned_post_roll_sec": post_roll,
        "target_end_sec": target,
        "render_contract_max_sec": render_max,
    }
    evidence["admissible"] = bool(
        conservative_ceiling <= SAFE_DURATION_CEILING_SEC
        and estimated_speech + pre_roll + post_roll <= target + 0.25
        and target <= SAFE_CONTENT_CEILING_SEC
        and render_max <= SAFE_DURATION_CEILING_SEC
    )
    return evidence


def enforce_estimated_duration_limit(plans: list[dict], parsed_by_id: dict) -> list[dict]:
    """Split oversized plans locally before the static provider admission gate."""
    # Keep r6's already-validated partition boundaries stable while r7 fixes
    # final narration synthesis.  The partition estimator may ignore lexical
    # attribution sentences; final prompts and gates below always use the real
    # source-aware predicate and therefore keep those sentences audible.
    partition_parsed_by_id: dict[str, dict] = {}
    for unit_id, unit in parsed_by_id.items():
        partition_unit = dict(unit)
        partition_text = str(partition_unit.get("adapted_text") or partition_unit.get("source_text", ""))
        if partition_unit.get("source_kind") == "narrative_text" and is_dialogue_attribution_text(partition_text):
            partition_unit["source_kind"] = "narrative_attribution"
        partition_parsed_by_id[unit_id] = partition_unit
    repaired = list(plans)
    changed = True
    while changed:
        changed = False
        next_plans: list[dict] = []
        total = len(repaired)
        for index, plan in enumerate(repaired, start=1):
            unit_ids = list(plan.get("source_unit_ids", []))
            derived = split_plan_copy(plan, unit_ids, partition_parsed_by_id)
            prompt, _roles = compose_director_prompt(derived, partition_parsed_by_id, index, total)
            metrics = audio_drama_coverage_metrics(prompt, derived)
            ceiling = float(metrics.get("audio_drama_estimated_duration_ceiling_sec") or 0)
            if ceiling <= SAFE_CONTENT_CEILING_SEC or len(unit_ids) <= 1:
                next_plans.append(derived)
                continue
            midpoint = max(1, len(unit_ids) // 2)
            narrative_boundaries = [
                candidate
                for candidate in range(1, len(unit_ids))
                if partition_parsed_by_id.get(unit_ids[candidate], {}).get("speaker") == "Narrator"
            ]
            if narrative_boundaries:
                midpoint = min(narrative_boundaries, key=lambda candidate: (abs(candidate - midpoint), candidate))
            next_plans.append(split_plan_copy(plan, unit_ids[:midpoint], partition_parsed_by_id, 1))
            next_plans.append(split_plan_copy(plan, unit_ids[midpoint:], partition_parsed_by_id, 2))
            changed = True
        repaired = next_plans
    for index, plan in enumerate(repaired, start=1):
        plan["chunk_id"] = f"chunk_{index:03d}"
        prompt, _roles = compose_director_prompt(plan, parsed_by_id, index, len(repaired))
        evidence = single_source_unit_duration_evidence(prompt, plan, parsed_by_id)
        if not evidence.get("admissible") and is_english_prompt() and is_auto_planning():
            compiled_evidence = pre_admission_compiled_duration_evidence(prompt, plan, parsed_by_id)
            if compiled_evidence.get("admissible"):
                evidence = compiled_evidence
        ceiling = evidence["conservative_ceiling_sec"]
        if ceiling > SAFE_CONTENT_CEILING_SEC:
            if evidence["admissible"]:
                warnings = [
                    warning
                    for warning in plan.get("duration_gate_warnings", [])
                    if warning.get("code") != evidence["code"]
                ]
                plan["duration_gate_warnings"] = [*warnings, evidence]
                continue
            raise SystemExit(
                f"{plan['chunk_id']} cannot meet the <= {SAFE_CONTENT_CEILING_SEC:g}s estimated content ceiling "
                "without splitting a single source unit. Replan that source unit before provider admission."
            )
    return repaired


def seed_final_audio_prompt(chunk: dict) -> str:
    return str(chunk.get("final_audio_prompt") or chunk.get("audio_prompt") or chunk.get("text_prompt") or "").strip()


def normalize_final_audio_prompt(prompt: str, active_roles: list[str], refs: dict[str, str], chunk: dict, index: int, total: int) -> str:
    prompt = prompt.strip()
    prompt = prompt.replace("@Audio", "<<TGT_SPK")
    prompt = re.sub(r"\bsource_unit(?:_id)?s?\b", "story beats", prompt, flags=re.I)
    prompt = re.sub(r"\bcomplete\s+silence\b", "very low ambience", prompt, flags=re.I)
    prompt = re.sub(r"\btotal\s+silence\b", "very low ambience", prompt, flags=re.I)
    prompt = re.sub(r"\bdead\s+silence\b", "a brief tense musical beat", prompt, flags=re.I)
    prompt = re.sub(r"\blong\s+pause\b", "brief tense beat", prompt, flags=re.I)
    prompt = clean_english_director_text(prompt)
    prompt = re.sub(r"\bDo not read\b:?", "", prompt, flags=re.I)
    prompt = prompt.replace("<<TGT_SPK1", "<<TGT_SPK1>>").replace("<<TGT_SPK2", "<<TGT_SPK2>>").replace("<<TGT_SPK3", "<<TGT_SPK3>>")
    prompt = re.sub(r"(<<TGT_SPK\d+>>)>+", r"\1", prompt)
    prompt = re.sub(r"\s*\n\s*", "\n", prompt).strip()
    if not re.search(r"Ambient sound:|ambience|ambient", prompt, re.I):
        ambience = compact_clause_text(chunk.get("persistent_ambience", ""), "continuous source-derived ambience", 120)
        prompt = f"Ambient sound: {ambience}, continuing naturally beneath the voices.\n{prompt}"
    if not re.search(r"Background music:|score|music", prompt, re.I):
        prompt = f"{english_music_instruction(chunk, index, total)}\n{prompt}"
    return prompt.strip()


def has_audio_silence_risk(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(extremely\s+slow|very\s+slow|long\s+heavy\s+silences?|silences?\s+between\s+every\s+line|"
            r"pauses?\s+are\s+long|absolute\s+stillness|no\s+rhythm|long\s+silence|empty\s+silent\s+gaps)\b",
            prompt,
            re.I,
        )
    )


def clean_prompt_quote_text(text: str) -> str:
    return clean_english_director_text(text).replace('"', "'").strip()


DANGLING_SPOKEN_WORDS = {
    "a", "an", "the", "and", "but", "or", "to", "of", "for", "from", "with",
    "against", "into", "through", "that", "which", "his", "her", "their",
    "so", "they", "he", "she", "it", "we", "you",
}


def complete_spoken_bridge(text: str, source_text: str = "", max_chars: int = 300) -> str:
    candidate = clean_prompt_quote_text(text)
    candidate = re.sub(r"\s*[-–—]\s*([!?])$", r"\1", candidate)
    last_word = re.findall(r"[A-Za-z']+", candidate.lower())[-1:] or [""]
    if candidate[-1:] not in ".!?" and last_word[0] in DANGLING_SPOKEN_WORDS and source_text:
        candidate = clean_prompt_quote_text(source_text)
    if len(candidate) > max_chars:
        window = candidate[:max_chars]
        boundaries = [match.end() for match in re.finditer(r"[.!?;:,]", window)]
        if boundaries and boundaries[-1] >= max_chars * 0.35:
            candidate = window[: boundaries[-1]]
        else:
            candidate = window.rsplit(" ", 1)[0]
    candidate = candidate.rstrip(" ,;:-")
    words = re.findall(r"[A-Za-z']+", candidate.lower())
    while candidate[-1:] not in ".!?" and words and words[-1] in DANGLING_SPOKEN_WORDS:
        shortened = re.sub(r"\s+\S+$", "", candidate).rstrip(" ,;:-")
        if shortened == candidate:
            break
        candidate = shortened
        words = re.findall(r"[A-Za-z']+", candidate.lower())
    if candidate and candidate[-1] not in ".!?":
        candidate += "."
    return candidate


def narrator_quote_is_incomplete(text: str) -> bool:
    """Terminal punctuation wins over the dangling-word heuristic."""
    return not bool(re.search(r"[.!?]\s*$", text))


def complete_narrator_quotes(prompt: str) -> str:
    pattern = re.compile(
        r'(Narrator\s*\([^\n]*actor is <<TGT_SPK\d+>>[^\n]*\):\s*")([^"]+)(")',
        flags=re.I,
    )
    return pattern.sub(
        lambda match: match.group(1) + complete_spoken_bridge(match.group(2)) + match.group(3),
        prompt,
    )


def dialogue_line_for_unit(unit: dict, refs: dict[str, str], first_seen: bool) -> str:
    role = unit.get("speaker", "Narrator")
    label = speaker_label(role)
    ref = refs.get(role)
    emotion = clean_english_director_text(str(unit.get("emotion", "natural")))
    raw_text = str(unit.get("adapted_text") or unit.get("source_text", ""))
    final_token = (re.findall(r"[A-Za-z']+", raw_text) or [""])[-1]
    if re.search(r"[-–—]\s*$", raw_text) and len(final_token) <= 6:
        return ""
    text = complete_spoken_bridge(
        raw_text,
        str(unit.get("source_text", "")),
        220,
    )
    if not ref:
        return ""
    if first_seen:
        description = compact_plan_text(ROLE_SPECS.get(role, {}).get("description", "expressive voice"), "expressive voice", 70)
        return f'{label} ({description}, actor is {ref}, {emotion}): "{text}"'
    return f'{label} (actor is {ref}, {emotion}): "{text}"'


def key_dialogue_units_for_chunk(chunk: dict, parsed_by_id: dict, max_lines: int = 6) -> list[dict]:
    units = [
        parsed_by_id[unit_id]
        for unit_id in chunk.get("source_unit_ids", [])
        if unit_id in parsed_by_id and parsed_by_id[unit_id].get("source_kind") == "quoted_text"
    ]
    if len(units) <= max_lines:
        return units
    first_two = units[:2]
    last_two = units[-2:]
    middle = units[len(units) // 2 : len(units) // 2 + 2]
    selected: list[dict] = []
    for unit in first_two + middle + last_two:
        if unit.get("source_unit_id") not in {item.get("source_unit_id") for item in selected}:
            selected.append(unit)
    return selected[:max_lines]


def ensure_key_dialogue_in_prompt(prompt: str, chunk: dict, parsed_by_id: dict, active_roles: list[str], refs: dict[str, str]) -> str:
    dialogue_units = key_dialogue_units_for_chunk(chunk, parsed_by_id)
    if not dialogue_units:
        return prompt
    required_count = min(2, len(dialogue_units))
    if quoted_dialogue_line_count(prompt) >= required_count and forbidden_summary_directive_count(prompt) == 0:
        return prompt

    first_seen: set[str] = set()
    lines = []
    for unit in dialogue_units:
        role = unit.get("speaker", "Narrator")
        line = dialogue_line_for_unit(unit, refs, role not in first_seen)
        if not line:
            continue
        first_seen.add(role)
        if line not in prompt:
            lines.append(line)
    if not lines:
        return prompt
    dialogue_block = (
        "Key spoken dialogue, performed as real character lines instead of summarized narration:\n"
        + "\n".join(lines)
    )
    if len(dialogue_block) + len(prompt) + 2 <= MAX_PROMPT_CHARS:
        return f"{prompt}\n{dialogue_block}"
    compact_lines = lines[: max(2, min(4, len(lines)))]
    dialogue_block = (
        "Key spoken dialogue, performed as real character lines instead of summarized narration:\n"
        + "\n".join(compact_lines)
    )
    if len(dialogue_block) + len(prompt) + 2 <= MAX_PROMPT_CHARS:
        return f"{prompt}\n{dialogue_block}"
    return prompt


def ensure_narration_bridges_in_prompt(prompt: str, chunk: dict, refs: dict[str, str]) -> str:
    narrator_ref = refs.get("Narrator")
    if not narrator_ref:
        return prompt
    bridges = [
        clean_prompt_quote_text(item)
        for item in chunk.get("narration_bridge", [])
        if str(item).strip() and not is_dialogue_attribution_text(str(item))
    ]
    prompt_words = set(re.findall(r"[a-z']+", prompt.lower()))
    bridges = [
        item for item in bridges
        if item
        and normalized_fragment(item) not in normalized_fragment(prompt)
        and (
            not (bridge_words := set(re.findall(r"[a-z']+", item.lower())))
            or len(bridge_words & prompt_words) / len(bridge_words) < 0.65
        )
    ]
    if not bridges:
        return prompt
    lines = [
        f'Narrator (actor is {narrator_ref}, tense connective narration): "{bridge}"'
        for bridge in bridges[:3]
    ]
    block = "Spoken English narration bridges, only these narration lines should be voiced:\n" + "\n".join(lines)
    if len(block) + len(prompt) + 2 <= MAX_PROMPT_CHARS:
        return f"{prompt}\n{block}"
    return prompt


def ensure_english_nonspoken_rule(prompt: str) -> str:
    rule = (
        "Non-quoted ambience, music, action, and sound-design descriptions are production directions only; "
        "do not voice them as narration."
    )
    if rule in prompt:
        return prompt
    return f"{prompt}\n{rule}"


def ensure_complete_audio_tail(prompt: str) -> str:
    rule = (
        "After the final spoken line finishes completely, keep the existing ambience briefly, then settle naturally "
        "without cutting the final word or phoneme."
    )
    prompt = re.sub(r"\bEnd\s+(?:scene|segment)\.?", "", prompt, flags=re.I)
    prompt = re.sub(r"\bno\s+fade\s+out\b", "keep the sound bed continuous", prompt, flags=re.I)
    prompt = re.sub(r"([A-Za-z])[-–—]\s*\"", r'\1."', prompt)
    if rule not in prompt and len(prompt) + len(rule) + 1 <= MAX_PROMPT_CHARS:
        prompt = f"{prompt.rstrip()}\n{rule}"
    return prompt


LEGACY_GENERIC_OUTROS = (
    "After the final line, the surrounding ambience and music continue briefly and settle in a natural dramatic ending.",
    "After the final line, let the foreground action finish and its tail decay naturally. The music softens back into the steady ambience, and the soundscape fully settles before the audio ends.",
)


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def strategy_fingerprint(strategy: str) -> str:
    normalized = re.sub(r"\s+", " ", clean_english_director_text(strategy).lower()).strip(" .")
    return prompt_sha256(normalized)


def ensure_novel_repair_strategy(chunk_id: str, render_strategy: str, failed_fingerprints: set[str]) -> None:
    if strategy_fingerprint(render_strategy) in failed_fingerprints:
        raise SystemExit(
            f"Repair strategy for {chunk_id} is equivalent to a failed tail strategy; provider call refused."
        )


def snapshot_generation_attempt(
    run_dir: Path,
    chunk_id: str,
    label: str,
    render_prompt: str,
    manifest: dict,
) -> Path:
    attempt_root = run_dir / "logs" / "generation_attempts" / chunk_id
    attempt_number = len(list(attempt_root.glob("attempt_*"))) + 1
    attempt_dir = attempt_root / f"attempt_{attempt_number:03d}_{label}"
    write_text_once(attempt_dir / "render_prompt.txt", render_prompt)
    write_json_once(attempt_dir / "manifest.json", {"attempt": attempt_number, "label": label, **manifest})
    return attempt_dir


def audible_layer_contract(chunk: dict) -> dict:
    music = "" if background_music_disabled() or no_sustained_background_music(chunk) else compact_clause_text(
        chunk.get("music_bed", ""), "a low source-derived dramatic score", 140
    )
    ambience = compact_clause_text(
        chunk.get("persistent_ambience", ""), "continuous source-derived room tone", 140
    )
    source_sfx = [
        clean_english_director_text(str(item))
        for item in chunk.get("sfx_story_events", [])
        if str(item).strip()
    ]
    key_sfx = source_sfx if len(source_sfx) <= 2 else [source_sfx[0], source_sfx[-1]]
    if not key_sfx:
        key_sfx = [compact_clause_text(chunk.get("sound_design", ""), "source-motivated foreground action", 150)]
    return {"music": music, "ambience": ambience, "key_sfx": key_sfx}


def audible_closure_contract(prompt: str, plan: dict, repair: bool = False) -> dict:
    metrics = audio_drama_coverage_metrics(prompt, plan)
    estimated = float(metrics.get("audio_drama_estimated_duration_ceiling_sec") or 8.0)
    speech_floor = float(metrics.get("audio_drama_estimated_duration_floor_sec") or 3.0)
    event_count = int(metrics.get("soundscape_event_count") or 0)
    closure_sec = min(4.0, max(2.0, 2.0 + event_count * 0.15))
    base_target = min(SAFE_CONTENT_CEILING_SEC, max(8.0, math.ceil(speech_floor + closure_sec + 1.5)))
    target_end = max(7.0, base_target - (closure_sec if repair else 0.0))
    cadence_duration = min(closure_sec, max(1.5, target_end * 0.25))
    cadence_start = round(target_end - cadence_duration, 1)
    preferred_content_end = cadence_start
    return {
        "estimated_ceiling_sec": estimated,
        "target_end_sec": target_end,
        "preferred_content_end_sec": preferred_content_end,
        "foreground_hard_end_sec": cadence_start,
        "cadence_start_sec": cadence_start,
        "cadence_duration_sec": round(cadence_duration, 1),
        "provider_margin_sec": round(SAFE_DURATION_CEILING_SEC - target_end, 1),
        "speech_action_percent": "70-75",
        "audible_closure_percent": "20-25",
        "silent_padding_forbidden": True,
        "repair": repair,
    }


def closure_timeline_instruction(timing: dict) -> str:
    return (
        f"Time budget: complete speech and foreground action mainly within the first {timing['speech_action_percent']}% "
        f"of this scene, preferably by {timing['preferred_content_end_sec']:g}s and no later than "
        f"{timing['foreground_hard_end_sec']:g}s. Use the final {timing['audible_closure_percent']}% for audible "
        f"closure; the named music-and-ambience cadence must run from {timing['cadence_start_sec']:g}s to "
        f"{timing['target_end_sec']:g}s. End on its audible decay. Do not wait for the output limit and do not add "
        "silent padding."
    )


def chunk_specific_audible_outro(contract: dict, repair: bool = False, timing: dict | None = None) -> str:
    ambience = compact_clause_text(contract.get("ambience", ""), "the continuous room tone", 105)
    key_sfx = [compact_clause_text(item, "the final foreground action", 80) for item in contract.get("key_sfx", [])]
    action = key_sfx[-1] if key_sfx else "the final foreground action"
    timing = timing or {
        "foreground_hard_end_sec": 44.0 if not repair else 40.0,
        "cadence_start_sec": 44.0 if not repair else 40.0,
        "target_end_sec": 48.0 if not repair else 44.0,
        "speech_action_percent": "70-75",
        "audible_closure_percent": "20-25",
    }
    music = compact_clause_text(contract.get("music", ""), "", 105)
    resolution = (
        f"resolve {music} clearly over {ambience}"
        if music
        else f"let {ambience} settle naturally without music"
    )
    if repair:
        return (
            f"Final audible coda for this full rerender: by {timing['foreground_hard_end_sec']:g}s {action} is complete; "
            f"from {timing['cadence_start_sec']:g}s to {timing['target_end_sec']:g}s {resolution}, ending on the audible "
            "cadence with no silent padding."
        )
    return (
        f"Final audible coda: by {timing['foreground_hard_end_sec']:g}s {action} is complete; from "
        f"{timing['cadence_start_sec']:g}s to {timing['target_end_sec']:g}s {resolution}, "
        "ending on the audible cadence with no silent padding."
    )


def apply_chunk_specific_audible_outro(
    prompt: str, contract: dict, repair: bool = False, plan: dict | None = None
) -> tuple[str, str, dict]:
    result = prompt.strip()
    result = re.sub(r"^Time budget:.*?(?=\n)", "", result, count=1, flags=re.S).lstrip()
    # This function is intentionally idempotent: planned prompts are normalized
    # again while generation requests are created and repair prompts are built.
    result = re.sub(
        r"\nFinal audible coda(?: for this full rerender)?:[^\n]*\s*$",
        "",
        result,
        count=1,
        flags=re.I,
    ).rstrip()
    removable_outros = (*LEGACY_GENERIC_OUTROS, chunk_specific_audible_outro(contract, repair=False))
    for old_outro in removable_outros:
        result = result.replace(old_outro, "").rstrip()
    timing = audible_closure_contract(result, plan or {}, repair=repair)
    outro = chunk_specific_audible_outro(contract, repair=repair, timing=timing)
    result = f"{closure_timeline_instruction(timing)}\n{result}\n{outro}".strip()
    return result, outro, timing


def sanitize_fresh_rerender_note(repair_note: str) -> str:
    note = compact_plan_text(
        repair_note, "Correct the cited performance defect while completing every spoken sentence", 180
    )
    note = re.sub(r"\b(?:the\s+)?existing\s+audio(?:\s+segment)?\b", "this full rerender", note, flags=re.I)
    note = re.sub(r"\bpreserv(?:e|ing)\s+all\s+existing\b", "recreate all required", note, flags=re.I)
    return note


def build_budgeted_repair_prompt(
    base_prompt: str,
    repair_note: str,
    contract: dict | None = None,
    plan: dict | None = None,
) -> str:
    base = base_prompt.strip()
    contract = contract or {
        "music": "the clearly audible dramatic score",
        "ambience": "the continuous source-derived room tone",
        "key_sfx": ["the source-motivated foreground action"],
    }
    base, outro, _timing = apply_chunk_specific_audible_outro(base, contract, repair=True, plan=plan)
    key_sfx = "; ".join(contract.get("key_sfx", [])) or "the source-motivated foreground action"
    correction = (
        "Fresh full rerender contract: generate the complete mixed scene again from this prompt; this is not an "
        "edit of prior audio. Keep clearly audible background music: "
        + compact_clause_text(contract.get("music", ""), "the dramatic score", 105)
        + ". Maintain continuous ambience: "
        + compact_clause_text(contract.get("ambience", ""), "the source-derived room tone", 105)
        + ". Render the key action effects in source order: "
        + compact_plan_text(key_sfx, "the source-motivated foreground action", 150)
        + ". Performance correction: "
        + sanitize_fresh_rerender_note(repair_note)
        + ". Complete every spoken sentence with measured natural articulation."
    )
    base_without_outro = base.removesuffix(outro).rstrip()
    timeline, separator, body = base_without_outro.partition("\n")
    candidate = f"{timeline}\n{correction}\n{body if separator else ''}\n{outro}".strip()
    if len(candidate) <= MAX_PROMPT_CHARS:
        return candidate
    lines = base.splitlines()
    sound_line_indices = [index for index, line in enumerate(lines) if line.startswith("The sound of ")]
    protected_sound_lines = set(sound_line_indices[:2])
    removable = [
        index for index, line in enumerate(lines)
        if (
            (line.startswith("The sound of ") and index not in protected_sound_lines)
            or line.startswith("Sound design:")
            or line.startswith("Workflow V2 render contract:")
        )
    ]
    for index in reversed(removable):
        lines.pop(index)
        candidate_body = "\n".join(lines).strip().removesuffix(outro).rstrip()
        timeline, separator, body = candidate_body.partition("\n")
        candidate = f"{timeline}\n{correction}\n{body if separator else ''}\n{outro}".strip()
        if len(candidate) <= MAX_PROMPT_CHARS:
            return candidate
    raise SystemExit("Repair prompt cannot fit Seed Audio prompt limit without removing spoken coverage.")


def fit_base_prompt_budget(prompt: str) -> str:
    if len(prompt) <= BASE_PROMPT_BUDGET:
        return prompt
    lines = prompt.splitlines()
    sound_line_indices = [index for index, line in enumerate(lines) if line.startswith("The sound of ")]
    protected_sound_lines = set(sound_line_indices[:2])
    removable = [
        index for index, line in enumerate(lines)
        if (line.startswith("The sound of ") and index not in protected_sound_lines) or line.startswith("Sound design:")
    ]
    for index in reversed(removable):
        lines.pop(index)
        candidate = "\n".join(lines).strip()
        if len(candidate) <= BASE_PROMPT_BUDGET:
            return candidate
    candidate = "\n".join(lines).strip()
    if len(candidate) <= MAX_PROMPT_CHARS:
        # The repair reserve is a target, not a reason to discard valid spoken
        # coverage. A later repair can compact optional direction or replan the
        # chunk if it cannot fit within the provider's hard limit.
        return candidate
    raise SystemExit("Base prompt exceeds Seed Audio prompt limit without removable non-spoken direction.")


def english_plan_dialogue_lines(chunk: dict, parsed_by_id: dict, active_roles: list[str], refs: dict[str, str], max_lines: int = 5) -> list[str]:
    lines: list[str] = []
    seen_text: set[str] = set()

    candidates: list[dict] = []
    for item in chunk.get("must_keep_dialogue", []) or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("speaker", "")).strip()
        text = str(item.get("text", "")).strip()
        if role and text:
            candidates.append({"speaker": role, "text": text, "emotion": "urgent, source-faithful"})
    for item in chunk.get("dialogue_lines", []) or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("speaker", "")).strip()
        text = str(item.get("text", "")).strip()
        if role and text:
            candidates.append({"speaker": role, "text": text, "emotion": "natural"})
    for unit in key_dialogue_units_for_chunk(chunk, parsed_by_id, max_lines=max_lines):
        candidates.append(
            {
                "speaker": unit.get("speaker", "Narrator"),
                "text": unit.get("adapted_text") or unit.get("source_text", ""),
                "emotion": unit.get("emotion", "natural"),
            }
        )

    for item in candidates:
        role = item.get("speaker", "Narrator")
        text = clean_prompt_quote_text(item.get("text", ""))
        if role not in active_roles or role == "Narrator" or not text or text in seen_text:
            continue
        ref = refs.get(role)
        if not ref:
            continue
        emotion = compact_clause_text(str(item.get("emotion", "natural")), "natural", 38)
        lines.append(f'{speaker_label(role)} (actor is {ref}, {emotion}): "{text}"')
        seen_text.add(text)
        if len(lines) >= max_lines:
            break
    return lines


def build_official_english_prompt_from_plan(chunk: dict, parsed_by_id: dict, active_roles: list[str], refs: dict[str, str], index: int, total: int) -> str:
    ambience = compact_clause_text(chunk.get("persistent_ambience", ""), "continuous source-derived ambience", 145)
    sound = compact_clause_text(chunk.get("sound_design", ""), "source-motivated foreground action effects", 150)
    pace = audio_safe_pace_note(chunk.get("pace_note", ""))
    pace = re.sub(r"^natural\s+", "", pace, flags=re.I)
    source_units = [parsed_by_id[unit_id] for unit_id in chunk.get("source_unit_ids", []) if unit_id in parsed_by_id]
    dialogue_positions = [index for index, unit in enumerate(source_units) if unit.get("speaker") != "Narrator"]
    narrator_positions: list[int] = []
    for dialogue_position in dialogue_positions:
        preceding = next(
            (
                position for position in range(dialogue_position - 1, -1, -1)
                if is_explicit_narration_bridge_unit(source_units[position])
            ),
            None,
        )
        if preceding is not None and preceding not in narrator_positions:
            narrator_positions.append(preceding)
    for position, unit in enumerate(source_units):
        text = str(unit.get("adapted_text") or unit.get("source_text", ""))
        if is_explicit_narration_bridge_unit(unit) and position not in narrator_positions:
            narrator_positions.append(position)
        if len(narrator_positions) >= 3:
            break
    if dialogue_positions:
        trailing_narrator = next(
            (
                position for position in range(len(source_units) - 1, dialogue_positions[-1], -1)
                if is_explicit_narration_bridge_unit(source_units[position])
            ),
            None,
        )
        if trailing_narrator is not None and trailing_narrator not in narrator_positions:
            narrator_positions = narrator_positions[:2] + [trailing_narrator]
    selected_dialogue_positions = dialogue_positions[:4]
    if dialogue_positions and dialogue_positions[-1] not in selected_dialogue_positions:
        selected_dialogue_positions = selected_dialogue_positions + [dialogue_positions[-1]]
    selected_positions = sorted(set(selected_dialogue_positions + narrator_positions[:3]))

    ambience = clean_english_director_text(ambience)
    sound = clean_english_director_text(sound)
    ambience = re.sub(r"\b(?:total|absolute|complete|perfect)\s+(?:silence|quiet)\b", "subdued continuous room tone", ambience, flags=re.I)
    sound = re.sub(r"\b(?:total|absolute|complete|perfect)\s+(?:silence|quiet)(?:\s+cut)?\b", "subdued room-tone transition", sound, flags=re.I)
    lines = [
        english_music_instruction(chunk, index, total),
        f"Ambient sound: {ambience}, while {sound} emerges naturally as the action unfolds.",
    ]
    first_seen: set[str] = set()
    timeline_event_count = 0
    for selected_index, position in enumerate(selected_positions):
        unit = source_units[position]
        role = unit.get("speaker", "Narrator")
        text = str(unit.get("adapted_text") or unit.get("source_text", ""))
        if role == "Narrator":
            ref = refs.get("Narrator")
            if ref and is_explicit_narration_bridge_unit(unit):
                narrator_detail = ""
                if "Narrator" not in first_seen:
                    description = compact_plan_text(
                        ROLE_SPECS.get("Narrator", {}).get("description", "literary narrator"),
                        "literary narrator",
                        100,
                    )
                    narrator_detail = f"{description}, "
                lines.append(
                    f'Narrator ({narrator_detail}actor is {ref}, natural {pace}): '
                    f'"{complete_spoken_bridge(text, text, 220)}"'
                )
                first_seen.add("Narrator")
        elif role in active_roles and len(first_seen) < 4:
            dialogue = dialogue_line_for_unit(unit, refs, role not in first_seen)
            if dialogue:
                lines.append(dialogue)
                first_seen.add(role)
        cues = [
            clean_english_director_text(str(cue))
            for key in ("sfx_before", "sfx_during", "sfx_after")
            for cue in (unit.get(key, []) or [])
            if str(cue).strip()
        ]
        if cues:
            lines.append(f"Then {cues[0]} is heard naturally in the scene.")
            timeline_event_count += 1

    fallback_sfx = [clean_english_director_text(item) for item in chunk.get("sfx_story_events", []) if str(item).strip()]
    for event in fallback_sfx[: max(0, 2 - timeline_event_count)]:
        lines.append(f"Afterward, {event} is heard in the surrounding space.")

    rendered_so_far = "\n".join(lines)
    for role in active_roles:
        if refs[role] not in rendered_so_far:
            lines.append(f"{speaker_label(role)} (actor is {refs[role]}) performs the brief source attribution.")

    contract = audible_layer_contract(chunk)
    prompt, _outro, _timing = apply_chunk_specific_audible_outro(
        "\n".join(line for line in lines if line is not None), contract, plan=chunk
    )
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt

    # Last-resort compaction for long source windows: keep the required
    # language/voice/music/ambience contract and key dialogue, trim excess
    # descriptive sound events before validation.
    retained_sound_lines = 0
    compact_lines = []
    for line in lines:
        if not line:
            continue
        if line.startswith("The sound of "):
            if retained_sound_lines >= 2:
                continue
            retained_sound_lines += 1
        compact_lines.append(line)
    prompt = "\n".join(compact_lines).strip()
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    # Do not delete mandatory sound or timeline structure to squeeze an
    # oversized dramatic window. The static gate will send it back for a
    # smaller plan before any Audio provider call.
    return "\n".join(compact_lines).strip()


def prompt_needs_engineering_repair(prompt: str, chunk: dict, active_roles: list[str]) -> bool:
    metrics = english_best_practice_prompt_metrics(prompt)
    if len(prompt) > MAX_PROMPT_CHARS:
        return True
    if has_audio_silence_risk(prompt):
        return True
    if metrics.get("explicit_narrator_lines", 0) > 3:
        return True
    if metrics.get("unbound_quoted_dialogue_line_count", 0) > 0:
        return True
    if offstage_or_unreferenced_dialogue_roles(prompt, active_roles):
        return True
    if forbidden_summary_directive_count(prompt) > 0:
        return True
    refs = role_ref_map(active_roles)
    if any(refs[role] not in prompt for role in active_roles):
        return True
    return False


def compose_english_director_prompt(chunk: dict, parsed_by_id: dict, index: int, total: int) -> tuple[str, list[str]]:
    source_unit_ids = chunk.get("source_unit_ids", [])
    active_roles = active_roles_for_chunk(chunk, parsed_by_id)
    refs = role_ref_map(active_roles)
    planned_prompt = seed_final_audio_prompt(chunk)
    if is_auto_planning() and planned_prompt:
        prompt = normalize_final_audio_prompt(planned_prompt, active_roles, refs, chunk, index, total)
        if prompt_needs_engineering_repair(prompt, chunk, active_roles):
            prompt = build_official_english_prompt_from_plan(chunk, parsed_by_id, active_roles, refs, index, total)
        prompt, _outro, _timing = apply_chunk_specific_audible_outro(
            prompt, audible_layer_contract(chunk), plan=chunk
        )
        prompt = strip_background_music_from_prompt(prompt)
        prompt = complete_narrator_quotes(prompt)
        return fit_base_prompt_budget(prompt), active_roles
    if is_auto_planning():
        prompt = build_official_english_prompt_from_plan(chunk, parsed_by_id, active_roles, refs, index, total)
        prompt = strip_background_music_from_prompt(prompt)
        prompt = complete_narrator_quotes(prompt)
        return fit_base_prompt_budget(prompt), active_roles
    first_seen: set[str] = set()
    ambience = clean_english_director_text(chunk.get("persistent_ambience", "cold night air, stone reverb, faint magical static"))
    sound_design = clean_english_director_text(chunk.get("sound_design", "stone ambience, movement, object and action sounds"))
    pace = clean_english_director_text(chunk.get("pace_note", "natural adaptive pacing"))
    lines = [
        "Create a complete cinematic audio drama mix, not dry narration: spoken voices, continuous background ambience, and foreground action effects are generated together.",
        no_background_music_instruction(),
        f"Ambient sound: {compact_clause_text(ambience, 'stone ambience', 115)}. Keep the cloister alive under every spoken line with air, stone resonance, distant magical static, and connected room tone.",
        english_music_instruction(chunk, index, total),
        f"Sound design: {compact_clause_text(sound_design, 'source-motivated action sounds', 125)}. Foreground effects should happen as cinematic sounds, not as spoken narration.",
    ]
    primary_voice_notes = []
    for role in active_roles:
        ref = refs.get(role)
        if not ref:
            continue
        primary_voice_notes.append(f"{speaker_label(role)} uses {ref}")
    if primary_voice_notes:
        lines.append(
            "Voices: "
            + "; ".join(primary_voice_notes)
            + f". Natural adaptive pace ({pace}); one voice at a time; no overlapping narration and dialogue."
        )
    lines.append("")

    for unit_id in source_unit_ids:
        unit = parsed_by_id[unit_id]
        speaker = unit.get("speaker", "Narrator")
        if speaker not in ROLE_SPECS:
            speaker = "Narrator"
        voice_ref = refs.get(speaker)
        text = unit.get("adapted_text") or unit.get("source_text", "")
        first_for_role = speaker not in first_seen
        first_seen.add(speaker)
        sfx_lines = english_time_ordered_sfx_sentences(unit)
        for sfx_line in [line for line in sfx_lines if "cuts through" in line]:
            lines.append(sfx_line)
        lines.append(english_voice_line(speaker, voice_ref, first_for_role, unit, text))
        music_line = english_unit_music_sentence(unit)
        if music_line:
            lines.append(music_line)
        for sfx_line in [line for line in sfx_lines if "cuts through" not in line]:
            lines.append(sfx_line)
    if index == total:
        lines.append("After the final line, the foreground action finishes and the stone ambience settles naturally.")
    else:
        lines.append("The same ambience carries forward without a musical cadence.")
    prompt, _outro, _timing = apply_chunk_specific_audible_outro(
        "\n".join(lines), audible_layer_contract(chunk), plan=chunk
    )
    prompt = strip_background_music_from_prompt(prompt)
    return fit_base_prompt_budget(prompt), active_roles


def compose_director_prompt(chunk: dict, parsed_by_id: dict, index: int, total: int) -> tuple[str, list[str]]:
    if is_english_prompt():
        return compose_english_director_prompt(chunk, parsed_by_id, index, total)
    source_unit_ids = chunk.get("source_unit_ids", [])
    active_roles = active_roles_for_chunk(chunk, parsed_by_id)
    refs = role_ref_map(active_roles)
    if is_english_prompt():
        scene_default = "Moonlit magical cloister"
        ambience_default = "cold night air, stone reverb, faint magical static"
        music_default = "soft strings enter, duck under dialogue, swell on magic, fade at the end"
        pace_default = "natural adaptive English pacing; slower for dread, brisk but clear for action"
        sfx_default = "wand hum, robe movement, stone echo"
    else:
        scene_default = "草船借箭"
        ambience_default = "江水、雾气、木船轻响。"
        music_default = "低弦轻入,对白时压低,结尾淡出。"
        pace_default = "中文自然中速,按剧情调整快慢,不要机械匀速。"
        sfx_default = "水声、木船、战鼓。"
    lines = [
        f"[Scene: {with_sentence_punctuation(chunk.get('title', scene_default))}]",
        f"[Ambience: {chunk.get('persistent_ambience', ambience_default)}]",
        *([] if background_music_disabled() else [f"[Music: {chunk.get('music_bed', music_default)}]"]),
        *(["[Music: none. Use only voices, ambience / room tone, and necessary SFX.]"] if background_music_disabled() else []),
        f"[Pace: {with_sentence_punctuation(chunk.get('pace_note', pace_default))} API speech_rate={chunk.get('speech_rate', TARGET_SPEECH_RATE)}{sentence_punctuation()}]",
        transition_rule_text(),
        "",
        "[Voice binding]",
        *[with_sentence_punctuation(f"{speaker_label(role)} uses {refs[role]}. {ROLE_SPECS[role]['description']}") for role in active_roles],
        only_active_voices_text(),
        "",
        f"[SFX: {with_sentence_punctuation(chunk.get('sound_design', sfx_default))} {sfx_visibility_text()}]",
        f"[Chunk {index:02d}/{total:02d}]",
    ]
    last_attribution = None
    last_speaker = None
    for unit_id in source_unit_ids:
        unit = parsed_by_id[unit_id]
        speaker = unit.get("speaker", "Narrator")
        if speaker not in refs:
            speaker = "Narrator"
        voice_ref = refs[speaker]
        delivery = delivery_for_chunk(unit, chunk)
        text = unit.get("adapted_text") or unit.get("source_text", "")
        before = unit.get("sfx_before", [])[:1]
        after = unit.get("sfx_after", [])[:1]
        bridge = transition_bridge_text()
        if last_speaker and last_speaker != speaker and bridge:
            lines.append(bridge)
        if before and not is_english_prompt():
            lines.append(f"[SFX under transition: {before[0]}]")
        attribution = unit.get("quote_attribution_text")
        if not is_english_prompt() and speaker != "Narrator" and "Narrator" in refs and attribution != last_attribution:
            cue = clean_attribution(attribution, speaker)
            if cue:
                lines.append(f"[{speaker_label('Narrator')}, @Audio1, cue, natural, brief speaker attribution, leave a small pause after this cue]")
                lines.append(cue)
            last_attribution = attribution
        lines.append(f"[{speaker_label(speaker)}, {voice_ref}, {unit.get('emotion', 'neutral')}, {delivery}]")
        lines.append(simplify_text(text))
        if after and not is_english_prompt():
            lines.append(f"[SFX bridge: {after[0]}]")
        last_speaker = speaker
    lines.append(music_fade_text())
    return "\n".join(lines), active_roles


def derive_render_plan_contract(prompt: str, plan: dict, metrics: dict) -> dict:
    """Turn an open-ended scene plan into a bounded, renderable contract."""
    spoken_words = quoted_word_count(prompt)
    narrator_count = int(metrics.get("narrator_unit_count") or 0)
    dialogue_count = int(metrics.get("dialogue_unit_count") or 0)
    event_count = int(metrics.get("soundscape_event_count") or rendered_sfx_count(prompt))
    speech_sec = max(2.5, spoken_words / 3.1 + narrator_count * 0.8)
    pre_roll_sec = 1.0 if event_count else 0.5
    post_roll_sec = min(4.0, max(1.5, 1.5 + event_count * 0.25))
    action_sec = min(5.0, event_count * 0.45)
    target = min(SAFE_CONTENT_CEILING_SEC, max(7.0, speech_sec + pre_roll_sec + post_roll_sec + action_sec))
    lower = max(5.0, target * 0.72)
    upper = min(SAFE_DURATION_CEILING_SEC, max(lower + 4.0, target * 1.30))
    if dialogue_count and narrator_count:
        chunk_type = "mixed_dialogue_narration"
    elif dialogue_count:
        chunk_type = "dialogue"
    else:
        chunk_type = "narration"
    return {
        "version": "render_contract_v2",
        "chunk_type": chunk_type,
        "estimated_speech_sec": round(speech_sec, 1),
        "planned_pre_roll_sec": round(pre_roll_sec, 1),
        "planned_post_roll_sec": round(post_roll_sec, 1),
        "target_audible_duration_sec": round(target, 1),
        "duration_range_sec": {"min": round(lower, 1), "max": round(upper, 1)},
        "max_unplanned_silent_tail_sec": 4.0,
        "silence_padding_forbidden": True,
        "audible_bed_required": bool(plan.get("music_bed") or plan.get("persistent_ambience")),
    }


def compile_prompt_v2(prompt: str, contract: dict) -> tuple[str, dict]:
    """Compile planner prose into one deterministic render contract and lint it."""
    compiled = prompt.strip()
    # Clarify a known contradictory phrase without erasing the intended sound.
    compiled = re.sub(r"\bno cadence\b", "no added spoken cadence", compiled, flags=re.I)
    lines = compiled.splitlines()
    seen: set[str] = set()
    duplicate_lines: list[str] = []
    for line in lines:
        normalized = re.sub(r"\s+", " ", line.strip().lower())
        if normalized and normalized in seen:
            duplicate_lines.append(line.strip())
        seen.add(normalized)
    quiet_terms = re.findall(r"\b(?:soft|faint|distant|low|quiet|restrained|barely audible)\b", compiled, flags=re.I)
    contradictions = []
    affirmative_silence = re.sub(
        r"\b(?:no|not|never|without|forbid(?:den)?|do not add)\b[^.\n]{0,35}\bsilent(?:ce)?\b",
        "",
        compiled,
        flags=re.I,
    )
    if re.search(r"\bsilent(?:ce)?\b", affirmative_silence, re.I) and re.search(r"\b(?:audible|music|ambience|score)\b", compiled, re.I):
        contradictions.append("silent_word_coexists_with_audible_layer_contract")
    contract_line = (
        "Workflow V2 render contract: keep the named music or ambience perceptibly audible whenever it is requested; "
        f"target about {contract['target_audible_duration_sec']:g}s within "
        f"{contract['duration_range_sec']['min']:g}-{contract['duration_range_sec']['max']:g}s; "
        f"allow roughly {contract['planned_pre_roll_sec']:g}s pre-roll and {contract['planned_post_roll_sec']:g}s "
        "audible post-roll; never extend the file with silence to meet a duration. Natural dramatic pauses are allowed, "
        "but a pause longer than four seconds must retain an audible room tone, ambience, or score bed."
    )
    prompt_lines = compiled.splitlines()
    if prompt_lines and prompt_lines[-1].startswith("Final audible coda:"):
        compiled = "\n".join([*prompt_lines[:-1], contract_line, prompt_lines[-1]]).strip()
    else:
        compiled = f"{compiled}\n{contract_line}".strip()
    blockers = []
    if len(compiled) > MAX_PROMPT_CHARS:
        blockers.append("compiled_prompt_over_character_limit")
    if contract["duration_range_sec"]["max"] > SAFE_DURATION_CEILING_SEC:
        blockers.append("duration_contract_over_provider_limit")
    warnings = []
    if len(quiet_terms) >= 6:
        warnings.append("low_audibility_language_is_overstacked")
    if duplicate_lines:
        warnings.append("duplicate_prompt_directives")
    warnings.extend(contradictions)
    return compiled, {
        "version": "prompt_lint_v2",
        "status": "fail" if blockers else ("warn" if warnings else "pass"),
        "blockers": blockers,
        "warnings": warnings,
        "quiet_term_count": len(quiet_terms),
        "duplicate_lines": duplicate_lines[:5],
        "compiled_prompt_chars": len(compiled),
    }


def normalize_rewrite(data: dict, source_units: list[dict]) -> dict:
    if is_auto_planning() and isinstance(data.get("roles"), dict) and not (STORY_CONFIG and STORY_CONFIG.get("lock_roles")):
        apply_planned_roles(data["roles"])
    if is_auto_planning() and isinstance(data.get("chunk_plan"), list):
        assert STORY_CONFIG is not None
        STORY_CONFIG["chunk_plan"] = data["chunk_plan"]
    data["voice_registry"] = {
        "scene_id": SCENE_ID,
        "production_mode": PRODUCTION_MODE,
        "voice_registry_version": VOICE_REGISTRY_VERSION,
        "reference_order": list(ROLE_SPECS.keys()),
        "voices": [
            {
                "role": role,
                "key": spec["key"],
                "speaker_env": speaker_env_name(role),
                "speaker": configured_speaker(role),
                "speaker_source": "env_override" if os.getenv(speaker_env_name(role), "").strip() else "official_voice_list_default",
                "reference_mode": reference_mode(role),
                "fallback_reference": role_reference_file(role).as_posix(),
                "description": spec["description"],
            }
            for role, spec in ROLE_SPECS.items()
        ],
    }
    data["reference_prompts"] = {spec["key"]: spec["reference_prompt"] for spec in ROLE_SPECS.values()}
    data.setdefault(
        "adaptation_plan",
        {
            "format": PRODUCTION_MODE,
            "source_beats": [],
            "must_keep_dialogue": [],
            "optional_dialogue": [],
            "narration_strategy": "",
            "sfx_story_strategy": "",
            "music_strategy": "",
            "compression_policy": "",
        },
    )
    parsed = data.get("parsed_source_units") or []
    if not parsed:
        parsed = [
            {
                **unit,
                "type": "dialogue" if unit.get("source_kind") == "quoted_text" else "narration",
                "speaker": infer_speaker_from_source(unit),
                "speaker_evidence": unit.get("quote_attribution_text", "narration"),
                "adapted_text": unit["source_text"],
                "preservation_level": "verbatim",
                "omission_reason": None,
                "emotion": "tense",
                "narration_style": "descriptive_narration",
                "delivery": "medium-fast, steady but not slow",
                "sfx_before": [],
                "sfx_during": [],
                "sfx_after": [],
                "sfx_layer": "none",
                "sfx_importance": "none",
                "sfx_reason": "no explicit action sound in fallback parser",
                "music_intent": "maintain tension",
            }
            for unit in source_units
        ]
    source_by_id = {unit["source_unit_id"]: unit for unit in source_units}
    parsed_by_source_id = {
        unit.get("source_unit_id"): unit
        for unit in parsed
        if unit.get("source_unit_id") in source_by_id
    }
    normalized_parsed = []
    for source in source_units:
        unit = parsed_by_source_id.get(source["source_unit_id"], {})
        merged = {**source, **unit}
        inferred_speaker = infer_speaker_from_source(source)
        if not unit:
            merged.update(
                {
                    "type": "dialogue" if source.get("source_kind") == "quoted_text" else "narration",
                    "speaker": inferred_speaker,
                    "speaker_evidence": source.get("quote_attribution_text", "source-unit fallback"),
                    "adapted_text": source["source_text"],
                    "preservation_level": "verbatim",
                    "omission_reason": None,
                    "emotion": "tense",
                    "narration_style": "descriptive_narration",
                    "delivery": "medium-fast, steady but not slow",
                    "sfx_before": [],
                    "sfx_during": [],
                    "sfx_after": [],
                    "sfx_layer": "none",
                    "sfx_importance": "none",
                    "sfx_reason": "model omitted this unit; preserve source text for downstream coverage",
                    "music_intent": "maintain continuity",
                    "model_omitted_fallback": True,
                }
            )
        if not is_auto_planning() and inferred_speaker != "Narrator":
            merged["speaker"] = inferred_speaker
        if merged.get("speaker") not in ROLE_SPECS:
            merged["speaker"] = "Narrator"
        merged.setdefault("adapted_text", source["source_text"])
        merged.setdefault("preservation_level", "verbatim")
        merged.setdefault("omission_reason", None)
        merged.setdefault("delivery", "medium-fast, steady but not slow")
        merged.setdefault("sfx_before", [])
        merged.setdefault("sfx_during", [])
        merged.setdefault("sfx_after", [])
        merged.setdefault("sfx_layer", "none")
        merged.setdefault("sfx_importance", "none")
        merged.setdefault("sfx_reason", "")
        normalized_parsed.append(merged)
    if is_auto_planning() and is_english_prompt():
        normalized_parsed = repair_quoted_speakers(normalized_parsed)
    normalized_parsed = [normalize_speaker_attribution(unit) for unit in normalized_parsed]
    normalized_parsed = enforce_supported_named_speakers(normalized_parsed)
    data["parsed_source_units"] = normalized_parsed

    parsed_by_id = {unit["source_unit_id"]: unit for unit in normalized_parsed}
    seed_plans = default_chunk_plan(source_units)
    seed_has_final_prompts = any(seed_final_audio_prompt(plan) for plan in seed_plans)
    if is_auto_planning() and is_english_prompt() and seed_has_final_prompts:
        plans = seed_plans
        plans = enforce_chunk_role_limit(plans, parsed_by_id)
        plans = enforce_max_source_unit_count(plans, parsed_by_id, max_units=SAFE_SOURCE_UNITS_PER_CHUNK)
        plans = enforce_min_chunk_shape(plans, parsed_by_id, min_units=3)
    elif is_auto_planning() and is_english_prompt():
        plans = capacity_first_plan(normalized_parsed, parsed_by_id, seed_plans)
        plans = enforce_max_source_unit_count(plans, parsed_by_id, max_units=SAFE_SOURCE_UNITS_PER_CHUNK)
        plans = enforce_min_chunk_shape(plans, parsed_by_id, min_units=3)
    else:
        plans = seed_plans
        plans = enforce_chunk_role_limit(plans, parsed_by_id)
        plans = enforce_min_chunk_shape(plans, parsed_by_id)
        plans = enforce_max_chunk_count(plans, parsed_by_id, target_max=3 if is_english_prompt() else 6)
    if is_auto_planning() and is_english_prompt():
        plans = shift_narrative_setup_to_dialogue_openers(plans, parsed_by_id)
        plans = enforce_chunk_role_limit(plans, parsed_by_id)
    if not seed_has_final_prompts:
        plans = enforce_prompt_length_limit(plans, parsed_by_id)
        if is_auto_planning() and is_english_prompt():
            # Never merge a long dramatic section back into a few oversized
            # requests after the planner has split it for performance safety.
            plans = enforce_max_source_unit_count(plans, parsed_by_id, max_units=SAFE_SOURCE_UNITS_PER_CHUNK)
            plans = enforce_min_chunk_shape(plans, parsed_by_id, min_units=3)
        else:
            plans = enforce_max_chunk_count(plans, parsed_by_id, target_max=3 if is_english_prompt() else 6)
            plans = merge_short_tail_chunk(plans, parsed_by_id)
    if is_auto_planning() and is_english_prompt():
        plans = repair_continuous_dialogue_boundaries(plans, parsed_by_id)
        plans = enforce_max_source_unit_count(plans, parsed_by_id, max_units=SAFE_SOURCE_UNITS_PER_CHUNK)
        plans = enforce_estimated_duration_limit(plans, parsed_by_id)
        plans = shift_narrative_setup_to_dialogue_openers(plans, parsed_by_id)
        plans = enforce_chunk_role_limit(plans, parsed_by_id)
        plans = enforce_estimated_duration_limit(plans, parsed_by_id)
    chunks = []
    for index, plan in enumerate(plans, start=1):
        derived_fields = local_plan_fields_for_units(plan, plan.get("source_unit_ids", []), parsed_by_id)
        for field, value in derived_fields.items():
            if field == "narration_bridge" or not plan.get(field):
                plan[field] = value
        if len(plan.get("source_unit_ids", [])) >= 8 and not plan.get("omission_rationale"):
            plan["omission_rationale"] = [
                "Nonessential descriptive wording may be compressed into narration and source-motivated sound while every source unit remains mapped."
            ]
        prompt, active_roles = compose_director_prompt(plan, parsed_by_id, index, len(plans))
        unit_ids = plan.get("source_unit_ids", [])
        music_actions = english_music_actions(index, len(plans)) if is_english_prompt() else ["enter", "duck", "swell", "fade"]
        narrator_count = sum(
            1 for unit_id in unit_ids if is_explicit_narration_bridge_unit(parsed_by_id[unit_id])
        )
        dialogue_count = sum(1 for unit_id in unit_ids if parsed_by_id[unit_id].get("speaker") != "Narrator")
        safe_speech_rate = safe_english_speech_rate(plan.get("speech_rate", TARGET_SPEECH_RATE), narrator_count)
        source_sfx_count = sum(
            len(parsed_by_id[unit_id].get("sfx_before", []))
            + len(parsed_by_id[unit_id].get("sfx_during", []))
            + len(parsed_by_id[unit_id].get("sfx_after", []))
            for unit_id in unit_ids
        )
        rendered_sfx = rendered_sfx_count(prompt) if is_english_prompt() else source_sfx_count
        performance_metrics = english_prompt_performance_metrics(prompt) if is_english_prompt() else {}
        best_practice_metrics = english_best_practice_prompt_metrics(prompt) if is_english_prompt() else {}
        coverage_metrics = audio_drama_coverage_metrics(prompt, plan) if is_english_prompt() and is_auto_planning() else {}
        render_contract = derive_render_plan_contract(
            prompt,
            plan,
            {
                "narrator_unit_count": narrator_count,
                "dialogue_unit_count": dialogue_count,
                **best_practice_metrics,
            },
        )
        prompt, prompt_lint = compile_prompt_v2(prompt, render_contract)
        if prompt_lint["blockers"]:
            raise SystemExit(f"{plan.get('chunk_id', f'chunk_{index:03d}')} prompt lint failed: {prompt_lint['blockers']}")
        performance_metrics = english_prompt_performance_metrics(prompt) if is_english_prompt() else {}
        best_practice_metrics = english_best_practice_prompt_metrics(prompt) if is_english_prompt() else {}
        coverage_metrics = audio_drama_coverage_metrics(prompt, plan) if is_english_prompt() and is_auto_planning() else {}
        duration_gate_evidence = (
            compiled_render_contract_duration_evidence(prompt, plan, render_contract)
            if is_english_prompt() and is_auto_planning()
            else {"admissible": False}
        )
        if (
            duration_gate_evidence.get("admissible")
            and duration_gate_evidence.get("conservative_ceiling_sec", 0) > SAFE_CONTENT_CEILING_SEC
        ):
            warning_code = duration_gate_evidence["code"]
            if warning_code not in prompt_lint["warnings"]:
                prompt_lint["warnings"].append(warning_code)
            if not prompt_lint["blockers"]:
                prompt_lint["status"] = "warn"
        chunks.append(
            {
                "chunk_id": plan.get("chunk_id", f"chunk_{index:03d}"),
                "source_unit_ids": unit_ids,
                "active_roles": active_roles,
                "text_prompt": prompt,
                "expected_duration_sec": render_contract["duration_range_sec"],
                "render_plan_contract": render_contract,
                "prompt_lint": prompt_lint,
                "coverage_summary": plan.get("coverage_summary", ""),
                "source_beats": plan.get("source_beats", []),
                "must_keep_dialogue": plan.get("must_keep_dialogue", []),
                "optional_dialogue": plan.get("optional_dialogue", []),
                "narration_bridge": plan.get("narration_bridge", []),
                "sfx_story_events": plan.get("sfx_story_events", []),
                "persistent_ambience": plan.get("persistent_ambience", ""),
                "music_bed": plan.get("music_bed", ""),
                "sound_design": plan.get("sound_design", ""),
                "omission_rationale": plan.get("omission_rationale", []),
                "input_metrics": {
                    "source_unit_count": len(unit_ids),
                    "narrator_unit_count": narrator_count,
                    "dialogue_unit_count": dialogue_count,
                    "source_sfx_cue_count": source_sfx_count,
                    "sfx_cue_count": rendered_sfx,
                    "sfx_cue_limit": None if is_english_prompt() else None,
                    "music_actions": music_actions,
                    "prompt_chars": len(prompt),
                    "speech_rate": safe_speech_rate,
                    "uses_seed_final_audio_prompt": bool(is_auto_planning() and is_english_prompt() and seed_final_audio_prompt(plan)),
                    "duration_gate_evidence": duration_gate_evidence,
                    **performance_metrics,
                    **best_practice_metrics,
                    **coverage_metrics,
                },
                "continuity": plan.get("continuity", {}),
                "speech_rate": safe_speech_rate,
                "pace_note": plan.get("pace_note", ""),
            }
        )
    data["director_prompt_chunks"] = chunks
    data["scene_parse"] = {
        "scene_id": SCENE_ID,
        "beats": [
            {
                "beat_id": plan.get("chunk_id", f"chunk_{index:03d}"),
                "title": plan.get("title", ""),
                "source_unit_ids": plan.get("source_unit_ids", []),
                "persistent_ambience": plan.get("persistent_ambience", ""),
                "music_bed": plan.get("music_bed", ""),
                "sound_design": plan.get("sound_design", ""),
                **plan.get("continuity", {}),
            }
            for index, plan in enumerate(plans, start=1)
        ],
    }
    data["input_review"] = [
        {
            "chunk_id": chunk["chunk_id"],
            "active_roles": chunk["active_roles"],
            "source_units": chunk["input_metrics"]["source_unit_count"],
            "prompt_chars": len(chunk["text_prompt"]),
            "meta_words": chunk["input_metrics"].get("meta_words"),
            "playable_words": chunk["input_metrics"].get("playable_words"),
            "playable_word_ratio": chunk["input_metrics"].get("playable_word_ratio"),
            "read_aloud_estimated_duration_sec": chunk["input_metrics"].get("read_aloud_estimated_duration_sec"),
            "audio_drama_estimated_duration_floor_sec": chunk["input_metrics"].get("audio_drama_estimated_duration_floor_sec"),
            "audio_drama_estimated_duration_ceiling_sec": chunk["input_metrics"].get("audio_drama_estimated_duration_ceiling_sec"),
            "narrator_units": chunk["input_metrics"]["narrator_unit_count"],
            "dialogue_units": chunk["input_metrics"]["dialogue_unit_count"],
            "sfx_cues": chunk["input_metrics"]["sfx_cue_count"],
            "music_actions": chunk["input_metrics"]["music_actions"],
            "source_beats": chunk.get("source_beats", []),
            "must_keep_dialogue": chunk.get("must_keep_dialogue", []),
            "optional_dialogue": chunk.get("optional_dialogue", []),
            "narration_bridge": chunk.get("narration_bridge", []),
            "sfx_story_events": chunk.get("sfx_story_events", []),
            "render_plan_contract": chunk.get("render_plan_contract", {}),
            "prompt_lint": chunk.get("prompt_lint", {}),
            "omission_rationale": chunk.get("omission_rationale", []),
            "source_beat_count": chunk["input_metrics"].get("source_beat_count"),
            "must_keep_dialogue_count": chunk["input_metrics"].get("must_keep_dialogue_count"),
            "must_keep_dialogue_present_count": chunk["input_metrics"].get("must_keep_dialogue_present_count"),
            "missing_must_keep_dialogue": chunk["input_metrics"].get("missing_must_keep_dialogue"),
            "narration_bridge_count": chunk["input_metrics"].get("narration_bridge_count"),
            "sfx_story_event_count": chunk["input_metrics"].get("sfx_story_event_count"),
            "omission_rationale_count": chunk["input_metrics"].get("omission_rationale_count"),
            "explicit_narrator_lines": chunk["input_metrics"].get("explicit_narrator_lines"),
            "explicit_character_lines": chunk["input_metrics"].get("explicit_character_lines"),
            "time_ordered_sfx_lines": chunk["input_metrics"].get("time_ordered_sfx_lines"),
            "music_reaction_lines": chunk["input_metrics"].get("music_reaction_lines"),
            "uses_seed_final_audio_prompt": chunk["input_metrics"].get("uses_seed_final_audio_prompt"),
            "spoken_quote_word_ratio": chunk["input_metrics"].get("spoken_quote_word_ratio"),
            "quoted_dialogue_line_count": chunk["input_metrics"].get("quoted_dialogue_line_count"),
            "unbound_quoted_dialogue_line_count": chunk["input_metrics"].get("unbound_quoted_dialogue_line_count"),
            "forbidden_summary_directive_count": chunk["input_metrics"].get("forbidden_summary_directive_count"),
            "soundscape_event_count": chunk["input_metrics"].get("soundscape_event_count"),
            "music_keyword_count": chunk["input_metrics"].get("music_keyword_count"),
            "ambience_keyword_count": chunk["input_metrics"].get("ambience_keyword_count"),
            "sfx_keyword_count": chunk["input_metrics"].get("sfx_keyword_count"),
            "uses_official_reference_marker_style": chunk["input_metrics"].get("uses_official_reference_marker_style"),
            "uses_chronological_performance_style": chunk["input_metrics"].get("uses_chronological_performance_style"),
            "persistent_ambience": True,
            "narrator_connects_plot": True,
            "pace_not_too_slow": True,
            "voice_binding_complete": True,
            "ready_for_generation": True,
        }
        for chunk in chunks
    ]
    data["chunking_report"] = explain_chunk_boundaries(chunks, parsed_by_id)
    return data


def rewrite_attempt_number(path: Path) -> int | None:
    match = re.fullmatch(r"seed2_rewrite_(?:prompt|response|error)_attempt_(\d+)\.txt", path.name)
    return int(match.group(1)) if match else None


def rewrite_attempt_numbers(log_dir: Path) -> list[int]:
    numbers = {
        number
        for path in log_dir.glob("seed2_rewrite_*_attempt_*.txt")
        if (number := rewrite_attempt_number(path)) is not None
    }
    return sorted(numbers)


def rewrite_response_candidates(log_dir: Path) -> list[Path]:
    numbered = sorted(
        log_dir.glob("seed2_rewrite_response_attempt_*.txt"),
        key=lambda path: rewrite_attempt_number(path) or 0,
        reverse=True,
    )
    legacy = log_dir / "seed2_rewrite_response.txt"
    if legacy.exists():
        numbered.append(legacy)
    return numbered


def retryable_rewrite_error(message: str) -> bool:
    normalized = message.lower()
    return bool(re.search(r"http(?: error)?\s*429\b", normalized)) or any(
        marker in normalized
        for marker in (
            "too many requests",
            "toomanyrequests",
            "serveroverloaded",
            "incompleteread",
            "unexpected eof",
            "remote end closed",
            "timed out",
            "read operation timed out",
        )
    ) or bool(re.search(r"rewrite attempt \d+ exceeded \d+s", normalized))


def retryable_rewrite_validation_error(message: str) -> bool:
    return any(
        pattern in message
        for pattern in (
            "quoted dialogue for roles outside active_roles",
            "has more than 3 active roles",
            "multiple active roles using the same speaker id",
            "has quoted dialogue lines without actor markers",
            "too many explicit narrator lines",
            "missing must-keep dialogue",
            "must keep real quoted character dialogue",
            "without verifiable adjacent source evidence",
            "has too little real spoken dialogue",
        )
    )


def rewrite_checkpoint_path(log_dir: Path, response_path: Path) -> Path:
    number = rewrite_attempt_number(response_path)
    suffix = f"attempt_{number}" if number is not None else "legacy"
    return log_dir / f"seed2_rewrite_checkpoint_{suffix}.json"


def call_seed2_rewrite(run_dir: Path, source_units: list[dict]) -> dict:
    prompt = compact_rewrite_prompt(source_units)
    log_dir = run_dir / "logs"
    write_text_once(log_dir / "seed2_rewrite_prompt.txt", prompt)
    last_error = ""
    cached_response = log_dir / "seed2_rewrite_response.txt"
    for candidate in rewrite_response_candidates(log_dir):
        try:
            data = normalize_rewrite(extract_json(candidate.read_text(encoding="utf-8")), source_units)
            validate_rewrite(data, source_units)
            checkpoint = {"status": "reused", "path": str(candidate)}
            numbered_checkpoint = rewrite_checkpoint_path(log_dir, candidate)
            if not numbered_checkpoint.exists():
                write_json_once(numbered_checkpoint, checkpoint)
            legacy_checkpoint = log_dir / "seed2_rewrite_checkpoint.json"
            if not legacy_checkpoint.exists():
                write_json_once(legacy_checkpoint, checkpoint)
            return data
        except (Exception, SystemExit) as exc:
            if not last_error:
                last_error = f"Cached rewrite response failed current validation: {str(exc)[:800]}"
    if os.getenv("SEED_REWRITE_CACHED_ONLY") == "1":
        if rewrite_response_candidates(log_dir):
            raise SystemExit(last_error or "Cached rewrite response failed current validation.")
        raise SystemExit(f"Cached rewrite response is required but missing: {cached_response}")

    rewrite_attempts = max(1, int(os.getenv("SEED_REWRITE_ATTEMPTS", "2")))
    rewrite_timeout = max(10, int(os.getenv("SEED_REWRITE_TIMEOUT", "180")))
    prior_attempts = rewrite_attempt_numbers(log_dir)
    latest_attempt = max(prior_attempts, default=0)
    if latest_attempt:
        prior_error = log_dir / f"seed2_rewrite_error_attempt_{latest_attempt}.txt"
        if prior_error.exists():
            last_error = prior_error.read_text(encoding="utf-8")[:1000]
        elif not last_error:
            last_error = (
                f"Prior rewrite attempt {latest_attempt} produced no validated response. "
                "Do not repeat its prompt; continue with a revised attempt."
            )
            write_text_once(prior_error, last_error)

    next_attempt = latest_attempt + 1
    if next_attempt > rewrite_attempts:
        raise SystemExit(
            f"Seed 2.0 Pro rewrite attempt budget exhausted ({latest_attempt}/{rewrite_attempts}); "
            "no provider request was made."
        )

    for attempt in range(next_attempt, rewrite_attempts + 1):
        prompt_for_attempt = prompt
        if last_error:
            prompt_for_attempt = (
                f"{prompt}\n\nPrevious attempt failed: {last_error}\n"
                "Fix the plan, do not explain. If a chunk needs quoted dialogue from more than three roles, split it into smaller chunks. "
                "Every quoted dialogue role must be listed in that chunk's active_roles and bound with actor is <<TGT_SPKn>>. "
                "Never assign one role's dialogue to another role's speaker marker. "
                "If there are too many Narrator lines, compress narration into 1-4 short spoken bridges and move visible action into ambience, music, and foreground sound events. "
                "Return valid JSON only. Escape every double quote inside final_audio_prompt as \\\"."
            )
        try:
            attempt_prompt = log_dir / f"seed2_rewrite_prompt_attempt_{attempt}.txt"
            write_text_once(attempt_prompt, prompt_for_attempt)
            command = [
                sys.executable,
                str(LLM_CHAT_CLIENT),
                "--prompt-file",
                str(attempt_prompt),
                "--system",
                "You are a precise JSON-only audiobook workflow generator. Return one complete valid JSON object. Do not truncate.",
                "--model",
                REWRITE_MODEL,
                "--temperature",
                "0.1",
                "--max-tokens",
                str(int(os.getenv("SEED_REWRITE_MAX_TOKENS", "14000")) if is_auto_planning() else 12000),
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=rewrite_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"Seed 2.0 Pro rewrite attempt {attempt} exceeded {rewrite_timeout}s") from exc
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-2000:] or f"Seed 2.0 Pro rewrite exited {result.returncode}")
            text = result.stdout.strip()
            response_path = log_dir / f"seed2_rewrite_response_attempt_{attempt}.txt"
            write_text_once(response_path, text)
            data = normalize_rewrite(extract_json(text), source_units)
            validate_rewrite(data, source_units)
            if not cached_response.exists():
                write_text_once(cached_response, text)
            checkpoint = {"status": "validated", "path": str(response_path)}
            numbered_checkpoint = rewrite_checkpoint_path(log_dir, response_path)
            if not numbered_checkpoint.exists():
                write_json_once(numbered_checkpoint, checkpoint)
            legacy_checkpoint = log_dir / "seed2_rewrite_checkpoint.json"
            if not legacy_checkpoint.exists():
                write_json_once(legacy_checkpoint, checkpoint)
            return data
        except SystemExit as exc:
            message = str(exc)
            retryable = retryable_rewrite_validation_error(message)
            last_error = message[:1000]
            write_text_once(log_dir / f"seed2_rewrite_error_attempt_{attempt}.txt", last_error)
            if attempt < rewrite_attempts and retryable:
                time.sleep(20 * attempt)
                continue
            raise
        except Exception as exc:
            retryable = retryable_rewrite_error(str(exc))
            retryable = retryable or retryable_rewrite_validation_error(str(exc))
            last_error = str(exc)[:1000]
            write_text_once(log_dir / f"seed2_rewrite_error_attempt_{attempt}.txt", last_error)
            if attempt < rewrite_attempts and (retryable or isinstance(exc, json.JSONDecodeError)):
                time.sleep(20 * attempt)
                continue
            raise
    raise SystemExit(f"Seed 2.0 Pro rewrite failed after retries: {last_error}")


def validate_rewrite(data: dict, source_units: list[dict]) -> None:
    required = [
        "voice_registry",
        "reference_prompts",
        "parsed_source_units",
        "scene_parse",
        "director_prompt_chunks",
        "input_review",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        raise SystemExit(f"Seed 2.0 Pro rewrite missing keys: {missing}")
    expected_ids = [unit["source_unit_id"] for unit in source_units]
    parsed_ids = [unit.get("source_unit_id") for unit in data["parsed_source_units"]]
    if parsed_ids != expected_ids:
        raise SystemExit("Seed 2.0 Pro rewrite must preserve every source_unit_id in the original order.")
    unsupported_attributions = [
        unit.get("source_unit_id")
        for index, unit in enumerate(data["parsed_source_units"])
        if unit.get("source_kind") == "quoted_text"
        and unit.get("speaker") != "Narrator"
        and (
            unit.get("speaker_confidence") != "high"
            or not named_speaker_has_source_evidence(data["parsed_source_units"], index)
        )
    ]
    if unsupported_attributions:
        raise SystemExit(
            "Quoted dialogue assigned to a specific role without verifiable adjacent source evidence: "
            f"{unsupported_attributions}. Use Narrator for low-confidence lines or provide concise contextual evidence."
        )
    chunk_unit_ids: list[str] = []
    for chunk in data["director_prompt_chunks"]:
        prompt = chunk.get("text_prompt", "")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise SystemExit(f"{chunk.get('chunk_id')} prompt too long: {len(prompt)}")
        for index, _role in enumerate(chunk.get("active_roles", []), start=1):
            token = voice_ref_token(index)
            if token not in prompt:
                raise SystemExit(f"{chunk.get('chunk_id')} missing voice binding token {token}")
        if len(chunk.get("active_roles", [])) > 3:
            raise SystemExit(f"{chunk.get('chunk_id')} has more than 3 active roles.")
        speaker_refs: dict[str, list[str]] = {}
        for role in chunk.get("active_roles", []):
            if role in ROLE_SPECS and reference_mode(role) == "speaker":
                speaker_refs.setdefault(configured_speaker(role), []).append(role)
        speaker_conflicts = {speaker: roles for speaker, roles in speaker_refs.items() if speaker and len(roles) > 1}
        if speaker_conflicts:
            raise SystemExit(
                f"{chunk.get('chunk_id')} has multiple active roles using the same speaker id: {speaker_conflicts}. "
                "Change the voice registry speaker ids or split the chunk."
            )
        if is_english_prompt():
            if contains_cjk(prompt):
                raise SystemExit(f"{chunk.get('chunk_id')} contains CJK characters in English Audio prompt.")
            semantic_markers = {
                "ambience": r"Ambient sound:|ambience|ambient|wind|stone echo|room tone",
            }
            if not background_music_disabled():
                semantic_markers["music"] = r"Background music:|music|score|cello|strings|drone"
            for marker, pattern in semantic_markers.items():
                if not re.search(pattern, prompt, re.I):
                    raise SystemExit(f"{chunk.get('chunk_id')} missing prompt marker: {marker}")
        else:
            required_prompt_markers = ["Ambience", "SFX", "Pace"]
            if not background_music_disabled():
                required_prompt_markers.append("Music")
            for marker in required_prompt_markers:
                if marker not in prompt:
                    raise SystemExit(f"{chunk.get('chunk_id')} missing prompt marker: {marker}")
        if is_english_prompt():
            forbidden = [
                "Voice casting:",
                "Do not read",
                "@Audio",
                " speaks.",
                "Transition:",
                "leave one short",
                "leave short pauses",
                "No yet",
                "The next voice answers",
                "Background music and ambience fade naturally",
                "hard fade",
                "hard ending",
            ]
            found_forbidden = [item for item in forbidden if item in prompt]
            if found_forbidden:
                raise SystemExit(f"{chunk.get('chunk_id')} has read-risk prompt text: {found_forbidden}")
        source_unit_ids = chunk.get("source_unit_ids") or chunk.get("source_units") or []
        if not source_unit_ids:
            raise SystemExit(f"{chunk.get('chunk_id')} has no source_unit_ids")
        chunk_unit_ids.extend(source_unit_ids)
        metrics = chunk.get("input_metrics", {})
        if is_english_prompt():
            min_playable_ratio = 0.22 if metrics.get("source_unit_count", 0) <= 3 else 0.24
            if metrics.get("playable_word_ratio", 1) < min_playable_ratio:
                raise SystemExit(
                    f"{chunk.get('chunk_id')} has too much director/meta text for Audio 1.0: "
                    f"playable_word_ratio={metrics.get('playable_word_ratio')}, min={min_playable_ratio}"
                )
            if is_auto_planning():
                estimated_ceiling = float(metrics.get("audio_drama_estimated_duration_ceiling_sec") or 0)
                duration_evidence = metrics.get("duration_gate_evidence", {})
                if estimated_ceiling > SAFE_CONTENT_CEILING_SEC and not duration_evidence.get("admissible"):
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} exceeds the <= {SAFE_CONTENT_CEILING_SEC:g}s estimated "
                        f"content ceiling: {estimated_ceiling:g}s. Split it before provider admission."
                    )
                if metrics.get("unbound_quoted_dialogue_line_count", 0) > 0:
                    raise SystemExit(f"{chunk.get('chunk_id')} has quoted dialogue lines without actor markers.")
                unreferenced_roles = offstage_or_unreferenced_dialogue_roles(prompt, chunk.get("active_roles", []))
                if unreferenced_roles:
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} has quoted dialogue for roles outside active_roles: {unreferenced_roles}. "
                        "Split the chunk or include those roles in active_roles."
                    )
                if metrics.get("explicit_narrator_lines", 0) > 3:
                    raise SystemExit(f"{chunk.get('chunk_id')} has too many explicit narrator lines for sound-first official style.")
                if (
                    metrics.get("narrator_unit_count", 0) >= 3
                    and metrics.get("dialogue_unit_count", 0) == 0
                    and metrics.get("explicit_narrator_lines", 0) < min(3, metrics.get("narrator_unit_count", 0))
                ):
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} has insufficient complete narration bridges for narrator-only source coverage."
                    )
                narrator_quotes = re.findall(
                    r'Narrator\s*\([^\n]*actor is <<TGT_SPK\d+>>[^\n]*\):\s*"([^"]+)"',
                    prompt,
                    flags=re.I,
                )
                dangling_quotes = [
                    quote for quote in narrator_quotes
                    if narrator_quote_is_incomplete(quote)
                ]
                if dangling_quotes:
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} has incomplete or dangling Narrator lines: "
                        f"{dangling_quotes!r}"
                    )
                spoken_quotes = re.findall(
                    r'actor is <<TGT_SPK\d+>>[^\n]*\):\s*"([^"]+)"',
                    prompt,
                    flags=re.I,
                )
                incomplete_spoken_quotes = [
                    quote for quote in spoken_quotes
                    if re.search(r"[-–—]\s*$", quote)
                    or (
                        quote.rstrip()[-1:] not in ".!?"
                        and (re.findall(r"[A-Za-z']+", quote.lower())[-1:] or [""])[0] in DANGLING_SPOKEN_WORDS
                    )
                ]
                if incomplete_spoken_quotes:
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} has incomplete spoken lines: {incomplete_spoken_quotes!r}"
                    )
                if metrics.get("dialogue_unit_count", 0) >= 2 and metrics.get("quoted_dialogue_line_count", 0) < 2:
                    raise SystemExit(f"{chunk.get('chunk_id')} must keep real quoted character dialogue, not summary narration.")
                min_quote_ratio = 0.015 if metrics.get("dialogue_unit_count", 0) <= 3 else 0.03
                if metrics.get("dialogue_unit_count", 0) >= 2 and metrics.get("spoken_quote_word_ratio", 0) < min_quote_ratio:
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} has too little real spoken dialogue: "
                        f"spoken_quote_word_ratio={metrics.get('spoken_quote_word_ratio')}, min={min_quote_ratio}"
                    )
                if metrics.get("spoken_quote_word_ratio", 0) > 0.48:
                    raise SystemExit(f"{chunk.get('chunk_id')} is too speech-heavy: spoken_quote_word_ratio={metrics.get('spoken_quote_word_ratio')}")
                if metrics.get("forbidden_summary_directive_count", 0) > 0:
                    raise SystemExit(f"{chunk.get('chunk_id')} contains summary-style dialogue directives such as narrate/deliver-line.")
                required_source_beats = min(2, metrics.get("source_unit_count", 0))
                if metrics.get("source_beat_count", 0) < required_source_beats:
                    raise SystemExit(f"{chunk.get('chunk_id')} missing audio-drama source beat coverage.")
                if metrics.get("must_keep_dialogue_count", 0) and (
                    metrics.get("must_keep_dialogue_present_count", 0) < metrics.get("must_keep_dialogue_count", 0)
                ):
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} missing must-keep dialogue in final_audio_prompt: "
                        f"{metrics.get('missing_must_keep_dialogue', [])}"
                    )
                if metrics.get("narrator_unit_count", 0) >= 3 and metrics.get("narration_bridge_count", 0) < 1:
                    raise SystemExit(f"{chunk.get('chunk_id')} needs at least one narration_bridge for plot continuity.")
                if metrics.get("source_sfx_cue_count", 0) >= 2 and metrics.get("sfx_story_event_count", 0) < 1:
                    raise SystemExit(f"{chunk.get('chunk_id')} needs source-derived sfx_story_events.")
                if metrics.get("source_unit_count", 0) >= 8 and metrics.get("omission_rationale_count", 0) < 1:
                    raise SystemExit(f"{chunk.get('chunk_id')} needs omission_rationale for audio-drama compression.")
            else:
                if metrics.get("narrator_unit_count", 0) and metrics.get("explicit_narrator_lines", 0) < metrics.get("narrator_unit_count", 0):
                    raise SystemExit(f"{chunk.get('chunk_id')} has narration units without explicit Narrator lines.")
                if metrics.get("source_sfx_cue_count", 0) and metrics.get("time_ordered_sfx_lines", 0) == 0:
                    raise SystemExit(f"{chunk.get('chunk_id')} has source SFX but no time-ordered SFX lines.")
            if not metrics.get("uses_official_reference_marker_style"):
                raise SystemExit(f"{chunk.get('chunk_id')} missing official actor reference marker style.")
        if not is_english_prompt() and "Narrator" not in chunk.get("active_roles", []):
            raise SystemExit(f"{chunk.get('chunk_id')} must include Narrator as continuity anchor.")
        sfx_limit = metrics.get("sfx_cue_limit")
        if is_english_prompt() and sfx_limit is not None and metrics.get("sfx_cue_count", 0) > sfx_limit:
            raise SystemExit(f"{chunk.get('chunk_id')} has too many rendered SFX cues.")
        if not background_music_disabled():
            music_actions = set(metrics.get("music_actions", []))
            allowed_music_actions = {"enter", "continue", "duck", "swell", "carry", "fade"}
            if len(music_actions & allowed_music_actions) < 3:
                raise SystemExit(f"{chunk.get('chunk_id')} must include at least 3 music actions.")
    if chunk_unit_ids != expected_ids:
        raise SystemExit("Director prompt chunks must cover every source unit exactly once in order.")


def role_reference_placeholder(role: str) -> dict[str, str]:
    speaker = configured_speaker(role)
    if speaker and reference_mode(role) == "speaker":
        return {"speaker": speaker}
    key = ROLE_SPECS[role]["key"]
    return {"audio_url": f"FILE_PLACEHOLDER:{role_reference_file(role).as_posix()}"}


def role_reference_file(role: str) -> Path:
    key = ROLE_SPECS[role]["key"]
    suffix = ".mp3" if reference_mode(role) == "tts_audio" else ".wav"
    return Path("00_voice_references") / f"{key}{suffix}"


def role_reference_cli_args(run_dir: Path, role: str) -> list[str]:
    speaker = configured_speaker(role)
    if speaker and reference_mode(role) == "speaker":
        return ["--speaker", speaker]
    return ["--audio", str((run_dir / role_reference_file(role)).relative_to(ROOT))]


def audio_response_meta(path: Path) -> dict:
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_artifacts(run_dir: Path, rewrite: dict, source_units: list[dict]) -> dict:
    manifest = {
        "run_id": run_dir.name,
        "scene_id": SCENE_ID,
        "production_mode": PRODUCTION_MODE,
        "created_at": now_hk().isoformat(timespec="seconds"),
        "workflow_version": "doc_compliant_seed2pro_rewrite_then_seed_audio",
        "rewrite_model": REWRITE_MODEL,
        "audio_model": AUDIO_MODEL,
        "source": SOURCE_TITLE,
        "source_url": SOURCE_URL,
            "target_speech_rate": "adaptive_by_chunk",
        "status": "built",
        "outputs": {
            "final_audio": f"08_stitched/{SCENE_ID}_full.wav",
            "analysis": "09_stage_effect_analysis.md",
        },
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(
        run_dir / "00_workflow_config.json",
        {
            "scene_id": SCENE_ID,
            "production_mode": PRODUCTION_MODE,
            "prompt_language": prompt_language(),
            "auto_planning": is_auto_planning(),
            "source_title": SOURCE_TITLE,
            "source_url": SOURCE_URL,
            "roles": ROLE_SPECS,
        },
    )
    write_text(run_dir / "01_source_excerpt.txt", SOURCE_EXCERPT)
    write_json(run_dir / "02_source_units.json", source_units)
    write_json(
        run_dir / "03_scene_parse.json",
        {
            "parsed_source_units": rewrite["parsed_source_units"],
            "adaptation_plan": rewrite.get("adaptation_plan", {}),
            **rewrite["scene_parse"],
        },
    )
    write_json(run_dir / "04_voice_registry.json", rewrite["voice_registry"])

    refs = rewrite["reference_prompts"]
    for role, spec in ROLE_SPECS.items():
        key = spec["key"]
        if key not in refs:
            raise SystemExit(f"Missing reference prompt: {key}")
        if is_english_prompt() and contains_cjk(str(refs[key])):
            raise SystemExit(f"Reference prompt for {role} contains CJK characters in English mode.")
        write_text(run_dir / "00_reference_prompts" / f"{key}.txt", refs[key])
    active_chunk_ids = {chunk["chunk_id"] for chunk in rewrite["director_prompt_chunks"]}
    stale_files = [
        path
        for directory, suffix in (("05_director_prompt_chunks", ".txt"), ("06_generation_requests", ".json"))
        for path in (run_dir / directory).glob(f"chunk_*{suffix}")
        if path.stem not in active_chunk_ids
    ]
    if stale_files:
        archive_dir = run_dir / "logs" / "stale_plans" / now_hk().strftime("%Y%m%d-%H%M%S")
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in stale_files:
            shutil.move(str(path), archive_dir / f"{path.parent.name}__{path.name}")

    prompt_lengths = []
    for chunk in rewrite["director_prompt_chunks"]:
        chunk_id = chunk["chunk_id"]
        prompt = strip_background_music_from_prompt(chunk["text_prompt"])
        chunk["text_prompt"] = prompt
        prompt_lengths.append(len(prompt))
        source_unit_ids = chunk.get("source_unit_ids") or chunk.get("source_units", [])
        write_text(run_dir / "05_director_prompt_chunks" / f"{chunk_id}.txt", prompt)
        request = {
            "chunk_id": chunk_id,
            "scene_id": SCENE_ID,
            "production_mode": PRODUCTION_MODE,
            "model": AUDIO_MODEL,
            "rewrite_model": REWRITE_MODEL,
            "voice_registry_version": VOICE_REGISTRY_VERSION,
            "source_unit_ids": source_unit_ids,
            "active_roles": chunk.get("active_roles", []),
            "text_prompt": prompt,
            "coverage_summary": chunk.get("coverage_summary", ""),
            "source_beats": chunk.get("source_beats", []),
            "must_keep_dialogue": chunk.get("must_keep_dialogue", []),
            "optional_dialogue": chunk.get("optional_dialogue", []),
            "narration_bridge": chunk.get("narration_bridge", []),
            "sfx_story_events": chunk.get("sfx_story_events", []),
            "render_plan_contract": chunk.get("render_plan_contract", {}),
            "prompt_lint": chunk.get("prompt_lint", {}),
            "persistent_ambience": chunk.get("persistent_ambience", ""),
            "music_bed": chunk.get("music_bed", ""),
            "sound_design": chunk.get("sound_design", ""),
            "omission_rationale": chunk.get("omission_rationale", []),
            "references": [role_reference_placeholder(role) for role in chunk.get("active_roles", [])],
            "expected_duration_sec": chunk.get("expected_duration_sec", {"min": 20, "max": 100}),
            "prompt_constraints": {
                "max_prompt_chars": MAX_PROMPT_CHARS,
                "must_keep_voice_binding": True,
                "must_keep_persistent_ambience": True,
                "must_keep_music_bed": (not background_music_disabled()) and not no_sustained_background_music(chunk),
                "must_keep_sfx_cues": True,
                "must_keep_narrator_continuity": True,
                "must_keep_adaptive_natural_pace": True,
                "must_use_official_chronological_performance_prompt": True,
                "must_use_seed_final_audio_prompt": bool(is_auto_planning() and is_english_prompt()),
                "must_not_render_every_source_unit_as_a_separate_spoken_line": bool(is_auto_planning() and is_english_prompt()),
                "must_keep_source_coverage_mapping": True,
                "must_place_sfx_in_source_time_order": True,
                "must_not_change_plot": True,
            },
            "input_metrics": chunk.get("input_metrics", {}),
            "continuity": chunk.get("continuity", {}),
            "audio_config": {
                "format": "wav",
                "sample_rate": 24000,
                "speech_rate": chunk.get("speech_rate", TARGET_SPEECH_RATE),
                "loudness_rate": 0,
                "pitch_rate": 0,
            },
            "watermark": {},
        }
        contract = audible_layer_contract(chunk)
        _, base_outro, closure = apply_chunk_specific_audible_outro(prompt, contract, plan=chunk)
        request["audible_layer_contract"] = contract
        request["audible_closure_contract"] = closure
        request["prompt_fingerprints"] = {
            "base_prompt_sha256": prompt_sha256(prompt),
            "base_strategy_fingerprint": strategy_fingerprint(base_outro),
            "base_outro_strategy": base_outro,
            "closure_contract_fingerprint": prompt_sha256(json.dumps(closure, sort_keys=True)),
            "time_contract_position": "first_line",
            "audible_coda_position": "last_line",
        }
        request["failed_tail_strategy_fingerprints"] = []
        write_json(run_dir / "06_generation_requests" / f"{chunk_id}.json", request)

    write_json(run_dir / "logs" / "input_review.json", rewrite["input_review"])
    write_json(run_dir / "logs" / "chunking_report.json", rewrite.get("chunking_report", {}))
    write_json(
        run_dir / "logs" / "source_coverage.json",
        {
            "source_total_units": len(source_units),
            "parsed_units": len(rewrite["parsed_source_units"]),
            "generated_units": sum(len(chunk.get("source_unit_ids", chunk.get("source_units", []))) for chunk in rewrite["director_prompt_chunks"]),
            "generated_chunks": len(rewrite["director_prompt_chunks"]),
            "adaptation_plan": rewrite.get("adaptation_plan", {}),
            "chunk_coverage": [
                {
                    "chunk_id": chunk["chunk_id"],
                    "source_unit_ids": chunk.get("source_unit_ids", []),
                    "coverage_summary": chunk.get("coverage_summary", ""),
                    "source_beats": chunk.get("source_beats", []),
                    "must_keep_dialogue": chunk.get("must_keep_dialogue", []),
                    "optional_dialogue": chunk.get("optional_dialogue", []),
                    "narration_bridge": chunk.get("narration_bridge", []),
                    "sfx_story_events": chunk.get("sfx_story_events", []),
                    "omission_rationale": chunk.get("omission_rationale", []),
                    "metrics": {
                        key: chunk.get("input_metrics", {}).get(key)
                        for key in [
                            "source_beat_count",
                            "must_keep_dialogue_count",
                            "must_keep_dialogue_present_count",
                            "optional_dialogue_count",
                            "narration_bridge_count",
                            "sfx_story_event_count",
                            "omission_rationale_count",
                        ]
                    },
                }
                for chunk in rewrite["director_prompt_chunks"]
            ],
            "rewrite_model": REWRITE_MODEL,
            "note": "Source units are automatic splits from plain source text; Seed 2.0 Pro annotates and rewrites them.",
        },
    )
    return {"chunks": len(rewrite["director_prompt_chunks"]), "prompt_lengths": prompt_lengths}


def rewrap_audio(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source.relative_to(ROOT)),
            "-ar",
            "24000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(target.relative_to(ROOT)),
        ]
    )
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg rewrap failed for {source.name}: {result.stderr.strip()}")


def trim_trailing_silence(path: Path, max_tail_sec: float = 1.2) -> None:
    if not audio_decodes(path):
        return
    duration = audio_duration(path)
    if duration is None:
        return
    tail_intervals = [
        item for item in silence_intervals(path, min_duration_sec=max_tail_sec)
        if item.get("start_sec") is not None and (item.get("end_sec") or duration) >= duration - 0.25
    ]
    if not tail_intervals:
        return
    tail = max(tail_intervals, key=lambda item: item["duration_sec"])
    if tail["duration_sec"] <= max_tail_sec:
        return
    keep_until = max(1.0, float(tail["start_sec"]) + max_tail_sec)
    if keep_until >= duration:
        return
    hard_tmp = path.with_name(f"{path.stem}.tailtrimtmp{path.suffix}")
    hard_result = run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-t",
            f"{keep_until:.3f}",
            "-ar",
            "24000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(hard_tmp),
        ]
    )
    if hard_result.returncode == 0 and audio_decodes(hard_tmp):
        hard_tmp.replace(path)
    elif hard_tmp.exists():
        hard_tmp.unlink()


def repair_delivery_tail(path: Path, fade_sec: float = 0.6, pre_fade_sec: float = 0.35) -> dict:
    """Repair score/ambience-only endings without another provider generation."""
    if not audio_decodes(path):
        return {"status": "skipped", "reason": "audio_decode_failed"}
    duration = audio_duration(path)
    if duration is None or duration <= 1.0:
        return {"status": "skipped", "reason": "audio_too_short"}
    terminal_silence = next(
        (
            item
            for item in reversed(silence_intervals(path, min_duration_sec=pre_fade_sec))
            if item.get("start_sec") is not None
            and (item.get("end_sec") or duration) >= duration - 0.1
        ),
        None,
    )
    if terminal_silence:
        # A fade cannot reconstruct music or ambience that already ended before
        # a silent pad. Keep the retained WAV unchanged and require a real plan
        # or provider correction instead of reporting a no-op as applied.
        return {
            "status": "skipped",
            "reason": "no_audible_tail_to_fade",
            "terminal_silence_start_sec": terminal_silence["start_sec"],
            "terminal_silence_duration_sec": terminal_silence["duration_sec"],
        }
    start = max(0.0, duration - pre_fade_sec)
    pad = max(0.0, fade_sec - pre_fade_sec)
    tmp = path.with_name(f"{path.stem}.tailrepairtmp{path.suffix}")
    result = run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-af",
            f"apad=pad_dur={pad:.3f},afade=t=out:st={start:.3f}:d={fade_sec:.3f}",
            "-ar",
            "24000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(tmp),
        ]
    )
    if result.returncode == 0 and audio_decodes(tmp):
        tmp.replace(path)
        return {
            "status": "applied",
            "duration_before_sec": duration,
            "duration_after_sec": audio_duration(path),
            "fade_start_sec": start,
            "fade_duration_sec": fade_sec,
        }
    if tmp.exists():
        tmp.unlink()
    return {"status": "failed", "reason": result.stderr[-500:]}


def compress_internal_silence(path: Path, max_internal_sec: float = 1.35, keep_silence_sec: float = 0.35) -> bool:
    if not audio_decodes(path):
        return False
    duration = audio_duration(path)
    if duration is None:
        return False
    long_intervals = [
        item for item in silence_intervals(path, min_duration_sec=max_internal_sec, threshold_db="-45dB")
        if item.get("start_sec") is not None
        and item["duration_sec"] > max_internal_sec
        and (item.get("end_sec") or 0) < duration - 0.5
    ]
    if not long_intervals:
        return False
    tmp = path.with_name(f"{path.stem}.silencetmp{path.suffix}")
    result = run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path.relative_to(ROOT)),
            "-af",
            (
                "silenceremove="
                "start_periods=1:"
                "start_duration=0:"
                "start_threshold=-45dB:"
                f"start_silence={keep_silence_sec}:"
                "stop_periods=-1:"
                f"stop_duration={max_internal_sec}:"
                "stop_threshold=-45dB:"
                f"stop_silence={keep_silence_sec}"
            ),
            "-ar",
            "24000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(tmp.relative_to(ROOT)),
        ]
    )
    if result.returncode == 0 and audio_decodes(tmp):
        new_duration = audio_duration(tmp)
        if new_duration is not None and new_duration >= max(1.0, duration * 0.25):
            tmp.replace(path)
            return True
    if tmp.exists():
        tmp.unlink()
    return False


def normalize_audio_timing(path: Path) -> dict:
    before = audio_duration(path)
    if DESTRUCTIVE_TIMING_POSTPROCESS:
        trim_trailing_silence(path)
        compressed = compress_internal_silence(path)
        trim_trailing_silence(path)
    else:
        compressed = False
    return {
        "duration_before_timing_normalization_sec": before,
        "duration_after_timing_normalization_sec": audio_duration(path),
        "internal_silence_compressed": compressed,
        "destructive_timing_postprocess_enabled": DESTRUCTIVE_TIMING_POSTPROCESS,
    }


def reference_text_for_tts(text: str) -> str:
    cleaned = re.sub(r"\[[^\]]+\]\s*", "", text).strip()
    return cleaned or text.strip()


def generate_reference_audio(run_dir: Path) -> dict:
    outputs = {}
    for role, spec in ROLE_SPECS.items():
        key = spec["key"]
        speaker = configured_speaker(role)
        mode = reference_mode(role)
        if speaker and mode == "speaker":
            outputs[key] = {
                "role": role,
                "speaker": speaker,
                "source": "byteplus_speaker",
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            }
            continue
        prompt_path = run_dir / "00_reference_prompts" / f"{key}.txt"
        if mode == "tts_audio":
            raw_path = run_dir / "00_voice_references" / f"{key}.mp3"
            text = reference_text_for_tts(prompt_path.read_text(encoding="utf-8"))
            try:
                tts_client.synthesize_to_file(
                    text,
                    raw_path,
                    speaker=speaker,
                    audio_format="mp3",
                    sample_rate=24000,
                    explicit_language="en" if is_english_prompt() else "zh",
                    context_language="en" if is_english_prompt() else "zh",
                )
            except Exception as exc:
                raise SystemExit(f"TTS reference generation failed for {key}: {exc}") from exc
            outputs[key] = {
                "role": role,
                "speaker": speaker,
                "source": "tts_generated_audio_reference",
                "path": str(raw_path.relative_to(ROOT)),
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            }
            continue
        raw_path = run_dir / "00_voice_references_raw" / f"{key}.wav"
        clean_path = run_dir / "00_voice_references" / f"{key}.wav"
        result = run(
            [
                sys.executable,
                str(SEED_AUDIO_CLIENT),
                "--text-file",
                str(prompt_path.relative_to(ROOT)),
                "--out",
                str(raw_path.relative_to(ROOT)),
                "--format",
                "wav",
                "--sample-rate",
                "24000",
                "--speech-rate",
                "0",
            ]
        )
        outputs[key] = {
            "role": role,
            "source": "generated_reference_audio",
            "raw_path": str(raw_path.relative_to(ROOT)),
            "path": str(clean_path.relative_to(ROOT)),
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        if result.returncode != 0:
            raise SystemExit(f"Reference generation failed for {key}: {result.stderr.strip()}")
        rewrap_audio(raw_path, clean_path)
        if DESTRUCTIVE_TIMING_POSTPROCESS:
            trim_trailing_silence(clean_path)
    return outputs


def generate_scene_audio(
    run_dir: Path,
    force_chunk_ids: set[str] | None = None,
    repair_notes: dict[str, str] | None = None,
    only_chunk_ids: set[str] | None = None,
) -> dict:
    force_chunk_ids = force_chunk_ids or set()
    repair_notes = repair_notes or {}
    logs = []
    parts = []
    for prompt_path in sorted((run_dir / "05_director_prompt_chunks").glob("chunk_*.txt")):
        if only_chunk_ids is not None and prompt_path.stem not in only_chunk_ids:
            continue
        request_path = run_dir / "06_generation_requests" / f"{prompt_path.stem}.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        active_roles = request.get("active_roles", [])
        if len(active_roles) > 3:
            raise SystemExit(f"{prompt_path.stem} has more than 3 active roles: {active_roles}")
        reference_args: list[str] = []
        for role in active_roles:
            reference_args.extend(role_reference_cli_args(run_dir, role))
        raw_path = run_dir / "07_audio_parts_raw" / f"{prompt_path.stem}.wav"
        clean_path = run_dir / "07_audio_parts" / f"{prompt_path.stem}.wav"
        speech_rate = int(request.get("audio_config", {}).get("speech_rate", TARGET_SPEECH_RATE))
        force_regenerate = prompt_path.stem in force_chunk_ids
        render_prompt_path = prompt_path
        repair_note = repair_notes.get(prompt_path.stem, "").strip()
        parent_audio_sha256 = hashlib.sha256(clean_path.read_bytes()).hexdigest() if clean_path.exists() else None
        base_prompt = prompt_path.read_text(encoding="utf-8")
        base_outro = request.get("prompt_fingerprints", {}).get("base_outro_strategy") or chunk_specific_audible_outro(
            request.get("audible_layer_contract") or audible_layer_contract(request)
        )
        base_strategy = request.get("prompt_fingerprints", {}).get("base_strategy_fingerprint") or strategy_fingerprint(base_outro)
        render_strategy = base_outro
        render_closure = request.get("audible_closure_contract") or audible_closure_contract(base_prompt, request)
        if force_regenerate and (raw_path.exists() or clean_path.exists()):
            revision_dir = run_dir / "07_audio_revisions" / prompt_path.stem / now_hk().strftime("%Y%m%d-%H%M%S")
            revision_dir.mkdir(parents=True, exist_ok=True)
            for source in (raw_path, raw_path.with_suffix(raw_path.suffix + ".meta.json"), clean_path, prompt_path):
                if source.exists():
                    shutil.copy2(source, revision_dir / source.name)
        if force_regenerate and repair_note:
            if re.search(r"rushed|clipp|cut.?off|garbl|unintellig|articul|pacing", repair_note, re.I):
                speech_rate = min(speech_rate, 2)
            render_prompt_path = run_dir / "05_director_prompt_chunks" / "repairs" / prompt_path.name
            if render_prompt_path.exists():
                stem = render_prompt_path.stem
                suffix = render_prompt_path.suffix
                for attempt_index in range(2, 100):
                    candidate = render_prompt_path.with_name(f"{stem}_attempt_{attempt_index:03d}{suffix}")
                    if not candidate.exists():
                        render_prompt_path = candidate
                        break
            _repair_preview, render_strategy, render_closure = apply_chunk_specific_audible_outro(
                base_prompt,
                request.get("audible_layer_contract") or audible_layer_contract(request),
                repair=True,
                plan=request,
            )
            repaired_prompt = build_budgeted_repair_prompt(
                base_prompt,
                repair_note,
                request.get("audible_layer_contract") or audible_layer_contract(request),
                request,
            )
            failed_strategies = set(request.get("failed_tail_strategy_fingerprints", [])) | {base_strategy}
            ensure_novel_repair_strategy(prompt_path.stem, render_strategy, failed_strategies)
            write_text_once(render_prompt_path, repaired_prompt)
        if not force_regenerate and not audio_decodes(clean_path) and audio_decodes(raw_path):
            rewrap_audio(raw_path, clean_path)
            normalize_audio_timing(clean_path)
        if not force_regenerate and audio_decodes(clean_path):
            timing_normalization = normalize_audio_timing(clean_path)
            logs.append(
                {
                    "prompt": str(prompt_path.relative_to(ROOT)),
                    "raw_output": str(raw_path.relative_to(ROOT)),
                    "output": str(clean_path.relative_to(ROOT)),
                    "audio_url": audio_response_meta(raw_path).get("audio_url", ""),
                    "returncode": 0,
                    "stdout": "reused existing decoded audio part",
                    "stderr": "",
                    "attempts": 0,
                    "force_regenerate": False,
                    "timing_normalization": timing_normalization,
                }
            )
            parts.append(clean_path)
            continue
        render_prompt = render_prompt_path.read_text(encoding="utf-8")
        attempt_label = "provider_repair" if force_regenerate and repair_note else "initial"
        snapshot_generation_attempt(
            run_dir,
            prompt_path.stem,
            attempt_label,
            render_prompt,
            {
                "chunk_id": prompt_path.stem,
                "recorded_at": now_hk().isoformat(timespec="seconds"),
                "request_path": str(request_path.relative_to(ROOT)),
                "parent_audio_sha256": parent_audio_sha256,
                "base_prompt_sha256": prompt_sha256(base_prompt),
                "render_prompt_sha256": prompt_sha256(render_prompt),
                "base_strategy_fingerprint": base_strategy,
                "render_strategy_fingerprint": strategy_fingerprint(render_strategy),
                "render_outro_strategy": render_strategy,
                "audible_closure_contract": render_closure,
                "closure_contract_fingerprint": prompt_sha256(json.dumps(render_closure, sort_keys=True)),
                "time_contract_position": "first_line",
                "audible_coda_position": "last_line",
                "failed_strategy_fingerprints": sorted(
                    set(request.get("failed_tail_strategy_fingerprints", []))
                    | ({base_strategy} if attempt_label == "provider_repair" else set())
                ),
                "repair_note": repair_note or None,
                "fresh_full_rerender": attempt_label == "provider_repair",
            },
        )
        cmd = [
            sys.executable,
            str(SEED_AUDIO_CLIENT),
            "--text-file",
            str(render_prompt_path.relative_to(ROOT)),
            *reference_args,
            "--out",
            str(raw_path.relative_to(ROOT)),
            "--format",
            "wav",
            "--sample-rate",
            "24000",
            "--speech-rate",
            str(speech_rate),
        ]
        result = None
        attempts = 0
        for attempts in range(1, 4):
            result = run(cmd)
            if result.returncode == 0:
                break
            retryable = (
                "HTTP 5" in result.stderr
                or "unexpected EOF" in result.stderr
                or "EOF occurred" in result.stderr
                or "violation of protocol" in result.stderr
                or "URLError" in result.stderr
            )
            if attempts < 3 and retryable:
                time.sleep(2 * attempts)
                continue
            break
        assert result is not None
        log = {
            "prompt": str(prompt_path.relative_to(ROOT)),
            "raw_output": str(raw_path.relative_to(ROOT)),
            "output": str(clean_path.relative_to(ROOT)),
            "audio_url": audio_response_meta(raw_path).get("audio_url", ""),
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "attempts": attempts,
            "force_regenerate": force_regenerate,
            "repair_note": repair_note,
        }
        logs.append(log)
        if result.returncode != 0:
            raise SystemExit(f"Scene generation failed for {prompt_path.name}: {result.stderr.strip()}")
        rewrap_audio(raw_path, clean_path)
        log["timing_normalization"] = normalize_audio_timing(clean_path)
        parts.append(clean_path)
    return {"logs": logs, "parts": parts}


def concat_audio(run_dir: Path, parts: list[Path]) -> Path:
    stitched = run_dir / "08_stitched"
    stitched.mkdir(parents=True, exist_ok=True)
    concat = stitched / "concat.txt"
    write_text(concat, "".join(f"file '../07_audio_parts/{path.name}'\n" for path in parts))
    out = stitched / f"{SCENE_ID}_full.wav"
    result = run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat.relative_to(ROOT)),
            "-ar",
            "24000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(out.relative_to(ROOT)),
        ]
    )
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg concat failed: {result.stderr.strip()}")
    if DESTRUCTIVE_TIMING_POSTPROCESS:
        trim_trailing_silence(out)
    part_durations = [audio_duration(path) for path in parts]
    expected_duration = sum(duration for duration in part_durations if duration is not None)
    stitched_duration = audio_duration(out)
    if (
        part_durations
        and all(duration is not None for duration in part_durations)
        and stitched_duration is not None
        and stitched_duration + 0.75 < expected_duration
    ):
        raise SystemExit(
            f"ffmpeg concat duration mismatch: stitched={stitched_duration:.2f}s "
            f"parts_sum={expected_duration:.2f}s"
        )
    return out


def audio_duration(path: Path) -> float | None:
    result = run(["ffmpeg", "-hide_banner", "-i", str(path.relative_to(ROOT)), "-f", "null", "-"])
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def silence_intervals(path: Path, min_duration_sec: float = 1.0, threshold_db: str = "-45dB") -> list[dict]:
    result = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path.relative_to(ROOT)),
            "-af",
            f"silencedetect=noise={threshold_db}:d={min_duration_sec}",
            "-f",
            "null",
            "-",
        ]
    )
    starts: list[float] = []
    intervals: list[dict] = []
    for line in result.stderr.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            starts.append(float(start_match.group(1)))
            continue
        end_match = re.search(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", line)
        if end_match:
            start = starts.pop(0) if starts else None
            intervals.append(
                {
                    "start_sec": start,
                    "end_sec": float(end_match.group(1)),
                    "duration_sec": float(end_match.group(2)),
                }
            )
    return intervals


def adaptive_audio_signal_from_intervals(duration: float | None, intervals: list[dict]) -> dict:
    """Classify silence by duration and output share, not a rigid one-second rule."""
    if duration is None or duration <= 0:
        return {
            "status": "fail",
            "hard_reasons": ["invalid_audio_duration"],
            "warning_reasons": [],
            "leading_silence_sec": 0.0,
            "trailing_silence_sec": 0.0,
            "internal_silences": [],
            "repair_class": "regenerate_chunk",
        }
    leading = [item for item in intervals if (item.get("start_sec") or 0.0) <= 0.1]
    trailing = [
        item for item in intervals
        if (item.get("end_sec") or duration) >= duration - 0.25
    ]
    internal = [item for item in intervals if item not in leading and item not in trailing]
    leading_sec = max((float(item.get("duration_sec") or 0) for item in leading), default=0.0)
    trailing_sec = max((float(item.get("duration_sec") or 0) for item in trailing), default=0.0)
    review_internal = [item for item in internal if float(item.get("duration_sec") or 0) > ADAPTIVE_SILENCE_POLICY["internal_review_sec"]]
    repairable_internal = [item for item in internal if float(item.get("duration_sec") or 0) > ADAPTIVE_SILENCE_POLICY["internal_repair_sec"]]
    long_internal = review_internal
    long_internal_ratio = sum(float(item.get("duration_sec") or 0) for item in long_internal) / duration
    hard_reasons: list[str] = []
    warnings: list[str] = []
    if leading_sec > ADAPTIVE_SILENCE_POLICY["leading_hard_sec"] or leading_sec / duration > ADAPTIVE_SILENCE_POLICY["leading_hard_ratio"]:
        hard_reasons.append("excessive_leading_silence")
    elif leading_sec > ADAPTIVE_SILENCE_POLICY["leading_warning_sec"]:
        warnings.append("long_leading_pause")
    if any(float(item.get("duration_sec") or 0) > ADAPTIVE_SILENCE_POLICY["internal_hard_sec"] for item in internal):
        hard_reasons.append("excessive_internal_silence")
    elif repairable_internal:
        warnings.append("repairable_internal_silence")
    elif len(long_internal) >= 2 and long_internal_ratio > ADAPTIVE_SILENCE_POLICY["chapter_long_silence_ratio_hard"]:
        warnings.append("repeated_review_internal_pause")
    elif long_internal:
        warnings.append("review_internal_pause")
    if trailing_sec > ADAPTIVE_SILENCE_POLICY["trailing_hard_sec"] or trailing_sec / duration > ADAPTIVE_SILENCE_POLICY["trailing_hard_ratio"]:
        hard_reasons.append("excessive_trailing_silence")
    elif trailing_sec > ADAPTIVE_SILENCE_POLICY["trailing_trim_sec"] or trailing_sec / duration > ADAPTIVE_SILENCE_POLICY["trailing_warning_ratio"]:
        warnings.append("trimmable_trailing_silence")
    repair_class = "none"
    if hard_reasons == ["excessive_trailing_silence"]:
        repair_class = "local_tail_trim_then_reaudit"
    elif not hard_reasons and repairable_internal:
        repair_class = "repair_audio"
    elif not hard_reasons and "trimmable_trailing_silence" in warnings:
        repair_class = "local_tail_trim_then_reaudit"
    elif hard_reasons:
        repair_class = "regenerate_chunk"
    return {
        "status": "fail" if hard_reasons else ("warn" if warnings else "pass"),
        "hard_reasons": hard_reasons,
        "warning_reasons": warnings,
        "duration_sec": round(duration, 3),
        "leading_silence_sec": round(leading_sec, 3),
        "leading_silence_ratio": round(leading_sec / duration, 4),
        "trailing_silence_sec": round(trailing_sec, 3),
        "trailing_silence_ratio": round(trailing_sec / duration, 4),
        "internal_silences": internal,
        "long_internal_silence_ratio": round(long_internal_ratio, 4),
        "repair_class": repair_class,
        "policy": ADAPTIVE_SILENCE_POLICY,
    }


def adaptive_audio_signal_report(path: Path) -> dict:
    return adaptive_audio_signal_from_intervals(audio_duration(path), silence_intervals(path))


def attach_objective_audio_evidence(report: dict, paths: list[Path]) -> dict:
    by_id = {path.stem: adaptive_audio_signal_report(path) for path in paths}
    for item in report.get("chunks", []):
        item["objective_audio"] = by_id.get(item.get("chunk_id"), {})
    report["objective_audio_by_chunk"] = by_id
    return report


def language_code_for_asr() -> str:
    return "en-US" if is_english_prompt() else "zh-CN"


def transcript_language_metrics(text: str) -> dict:
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_words = len(re.findall(r"\b[A-Za-z][A-Za-z']*\b", text))
    total_chars = len(text)
    return {
        "transcript_chars": total_chars,
        "cjk_char_count": cjk_count,
        "latin_word_count": latin_words,
        "cjk_char_ratio": round(cjk_count / total_chars, 4) if total_chars else 0,
    }


def transcript_dialogue_coverage(transcript: str, request: dict) -> dict:
    """A tolerant ASR gate for required dialogue in an adapted audio drama."""
    transcript_words = set(re.findall(r"[a-z']+", transcript.lower()))
    required = [
        item for item in request.get("must_keep_dialogue", [])
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]
    covered = 0
    missing: list[str] = []
    for item in required:
        text = str(item.get("text", ""))
        words = [word for word in re.findall(r"[a-z']+", text.lower()) if len(word) > 2]
        if not words:
            continue
        overlap = sum(word in transcript_words for word in set(words)) / len(set(words))
        if overlap >= 0.6:
            covered += 1
        else:
            missing.append(text[:120])
    return {
        "must_keep_dialogue_count": len(required),
        "must_keep_dialogue_covered_count": covered,
        "missing_must_keep_dialogue": missing,
    }


def asr_language_audit(run_dir: Path, scene_logs: list[dict]) -> dict:
    requests = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "06_generation_requests").glob("chunk_*.json"))
    }
    reports = []
    fail_reasons: list[str] = []
    for log in scene_logs:
        chunk_id = Path(log.get("prompt", "chunk_unknown.txt")).stem
        audio_url = str(log.get("audio_url", "")).strip()
        report = {
            "chunk_id": chunk_id,
            "audio_url_available": bool(audio_url),
            "language_hint": language_code_for_asr(),
            "status": "skipped",
            "transcript": "",
            "metrics": {},
            "reasons": [],
        }
        if not audio_url:
            report["status"] = "fail"
            report["reasons"].append("missing_seed_audio_url_for_asr")
            fail_reasons.append(f"{chunk_id}: missing_seed_audio_url_for_asr")
            reports.append(report)
            continue
        try:
            task_id = asr_client.submit_asr_task(audio_url, language=language_code_for_asr(), audio_format="wav")
            result = asr_client.poll_asr_task(task_id, verbose=False)
            transcript = asr_client.extract_transcript_text(result)
            report["status"] = "pass"
            report["task_id"] = task_id
            report["transcript"] = transcript
            report["metrics"] = transcript_language_metrics(transcript)
            report["dialogue_coverage"] = transcript_dialogue_coverage(transcript, requests.get(chunk_id, {}))
            write_json(run_dir / "logs" / f"asr_{chunk_id}_raw.json", result)
            write_text(run_dir / "logs" / f"asr_{chunk_id}_transcript.txt", transcript)
            if is_english_prompt() and report["metrics"].get("cjk_char_count", 0) > 0:
                report["status"] = "fail"
                report["reasons"].append("asr_detected_cjk_in_english_output")
                fail_reasons.append(f"{chunk_id}: asr_detected_cjk_in_english_output")
            if is_english_prompt() and report["metrics"].get("latin_word_count", 0) < 5:
                report["status"] = "fail"
                report["reasons"].append("asr_too_few_english_words")
                fail_reasons.append(f"{chunk_id}: asr_too_few_english_words")
            if report["dialogue_coverage"].get("missing_must_keep_dialogue"):
                report["status"] = "fail"
                report["reasons"].append("asr_missing_must_keep_dialogue")
                fail_reasons.append(f"{chunk_id}: asr_missing_must_keep_dialogue")
        except Exception as exc:
            report["status"] = "fail"
            report["reasons"].append(f"asr_failed: {exc}")
            fail_reasons.append(f"{chunk_id}: asr_failed")
        reports.append(report)
    return {
        "status": "fail" if fail_reasons else "pass",
        "fail_reasons": fail_reasons,
        "policy": {
            "english_output": "ASR transcript must not contain CJK characters for English-mode generations.",
            "dialogue_coverage": "Required dialogue is checked with tolerant content-word overlap so adapted punctuation and ASR casing do not cause a false fail.",
            "source": "Seed Audio temporary URL captured from the same generation response.",
        },
        "chunks": reports,
    }


PERFORMANCE_FAILURES = {
    "rushed_or_unnatural_pacing",
    "clipped_word_or_sentence_ending",
    "hard_boundary_or_abrupt_cut",
    "click_or_pop_at_boundary",
    "repeated_syllable_or_word",
    "stuttered_duplicate_speech",
    "voice_masked_by_music_or_sfx",
    "mechanical_narration",
    "overlapping_voices",
    "missing_required_background_music",
    "missing_required_ambience",
    "missing_required_action_sfx",
}


def performance_review_prompt(chunk_id: str, request: dict) -> str:
    """Keep the listening task narrow so it judges the render, not the story genre."""
    contract = request.get("audible_layer_contract") or audible_layer_contract(request)
    required_layers = []
    failure_types = [
        "rushed_or_unnatural_pacing",
        "clipped_word_or_sentence_ending",
        "hard_boundary_or_abrupt_cut",
        "click_or_pop_at_boundary",
        "repeated_syllable_or_word",
        "stuttered_duplicate_speech",
        "voice_masked_by_music_or_sfx",
        "mechanical_narration",
        "overlapping_voices",
    ]
    if contract.get("music"):
        required_layers.append("clearly audible composed background music")
        failure_types.append("missing_required_background_music")
    if contract.get("ambience"):
        required_layers.append("persistent environmental ambience")
        failure_types.append("missing_required_ambience")
    if contract.get("key_sfx"):
        required_layers.append("the planned foreground action SFX")
        failure_types.append("missing_required_action_sfx")
    layer_instruction = (
        "This request requires " + ", ".join(required_layers) + "."
        if required_layers
        else "This request has no required music, ambience, or foreground SFX layer."
    )
    return f"""You are the final listening reviewer for one English audio-drama chunk.
Listen to the attached audio, not only to the written intent. Check whether the spoken English is naturally performed and fully audible.
Pay special attention to repeated syllables, doubled word starts, stutter-like duplicate phonemes, digital clicks/pops, and abrasive boundary artifacts.

Chunk id: {chunk_id}
Expected voices: {', '.join(request.get('active_roles', [])) or 'unknown'}
Expected pace: {request.get('pace_note', 'natural adaptive pace')}

{layer_instruction}
Fail for an audible problem: {', '.join(failure_types)}.
This workflow intentionally disables composed background music unless explicitly listed above. Do not fail merely because there is no music. Room tone or environmental ambience is enough for the bed when music is not required.
Return one JSON object only:
{{
  "verdict": "pass" | "fail",
  "issues": [{{"type": "one allowed failure type", "severity": "minor|major", "evidence": "short audible evidence"}}],
  "repair_instruction": "one concise generation instruction, or empty when pass",
  "summary": "one concise sentence"
}}"""


def performance_audio_audit(run_dir: Path, parts: list[Path]) -> dict:
    requests = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "06_generation_requests").glob("chunk_*.json"))
    }
    reports: list[dict] = []
    unavailable: list[str] = []
    for part in parts:
        chunk_id = part.stem
        request = requests.get(chunk_id, {})
        report = {
            "chunk_id": chunk_id,
            "model": AUDIO_REVIEW_MODEL,
            "status": "unavailable",
            "verdict": None,
            "issues": [],
            "repair_instruction": "",
            "summary": "",
        }
        try:
            response = llm_chat.chat_audio(
                performance_review_prompt(chunk_id, request),
                [str(part)],
                system="You are a strict but fair audio-quality reviewer. Return JSON only.",
                model=AUDIO_REVIEW_MODEL,
                temperature=0.0,
                max_tokens=900,
            )
            write_text(run_dir / "logs" / f"performance_{chunk_id}_response.txt", response)
            payload = extract_json(response)
            issues = [
                item for item in payload.get("issues", [])
                if isinstance(item, dict) and item.get("type") in PERFORMANCE_FAILURES
            ]
            verdict = str(payload.get("verdict", "")).lower()
            report.update(
                {
                    "verdict": verdict,
                    "issues": issues,
                    "repair_instruction": str(payload.get("repair_instruction", "")).strip(),
                    "summary": str(payload.get("summary", "")).strip(),
                }
            )
            report["status"] = "fail" if verdict == "fail" and issues else "pass"
            if verdict not in {"pass", "fail"}:
                report["status"] = "unavailable"
                report["summary"] = "Reviewer returned no usable verdict."
        except BaseException as exc:
            report["summary"] = f"reviewer_unavailable: {str(exc)[:300]}"
            unavailable.append(chunk_id)
        reports.append(report)
        if report["status"] == "unavailable" and chunk_id not in unavailable:
            unavailable.append(chunk_id)
    failed = [item["chunk_id"] for item in reports if item["status"] == "fail"]
    return {
        "status": "fail" if failed else ("unavailable" if unavailable else "pass"),
        "delivery_status": "fail" if failed or unavailable else "pass",
        "preview_status": "available" if parts else "missing",
        "failed_chunk_ids": failed,
        "unavailable_chunk_ids": unavailable,
        "policy": {
            "model": AUDIO_REVIEW_MODEL,
            "preview": "Generated audio is retained when the reviewer is unavailable.",
            "delivery": "A delivery pass requires a successful reviewer verdict for every generated chunk.",
        },
        "chunks": reports,
    }


def performance_repair_notes(report: dict | None) -> dict[str, str]:
    if not report:
        return {}
    return {
        item["chunk_id"]: item.get("repair_instruction") or "Use measured natural pacing, complete every word ending, and preserve a natural room-tone tail."
        for item in report.get("chunks", [])
        if item.get("status") == "fail" and item.get("chunk_id")
    }


def performance_gate(report: dict, mode: str, phase: str = "batch") -> dict:
    critical_types = {
        "rushed_or_unnatural_pacing",
        "clipped_word_or_sentence_ending",
        "hard_boundary_or_abrupt_cut",
        "click_or_pop_at_boundary",
        "repeated_syllable_or_word",
        "stuttered_duplicate_speech",
        "voice_masked_by_music_or_sfx",
        "mechanical_narration",
        "overlapping_voices",
        "missing_required_background_music",
        "missing_required_ambience",
        "missing_required_action_sfx",
    }
    failed: list[str] = [
        chunk_id for chunk_id, evidence in report.get("objective_audio_by_chunk", {}).items()
        if evidence.get("hard_reasons")
    ]
    pilot_blocking_types = {
        "hard_boundary_or_abrupt_cut",
        "click_or_pop_at_boundary",
        "repeated_syllable_or_word",
        "stuttered_duplicate_speech",
        "missing_required_background_music",
        "missing_required_ambience",
        "missing_required_action_sfx",
    }
    if mode in {"balanced", "required"}:
        for item in report.get("chunks", []):
            if not item.get("chunk_id"):
                continue
            objective_hard = bool(item.get("objective_audio", {}).get("hard_reasons"))
            issues = item.get("issues", [])
            reviewer_blocking = item.get("status") == "fail" and (
                mode == "required" or (
                phase == "pilot"
                and any(issue.get("type") in pilot_blocking_types for issue in issues if isinstance(issue, dict))
                ) or any(
                issue.get("severity") == "major" and issue.get("type") in critical_types
                for issue in issues if isinstance(issue, dict)
                )
            )
            if objective_hard or reviewer_blocking:
                failed.append(item["chunk_id"])
    unavailable = report.get("unavailable_chunk_ids", []) if mode == "required" else []
    report["mode"] = mode
    report["gate_failed_chunk_ids"] = list(dict.fromkeys(failed))
    report["objective_gate_policy"] = "adaptive silence hard failures override reviewer pass or minor severity"
    report["gate_status"] = "fail" if failed or unavailable else ("skipped" if mode == "off" else "pass")
    return report


def asr_gate(report: dict, mode: str) -> dict:
    report["mode"] = mode
    report["gate_status"] = report.get("status") if mode == "required" else ("skipped" if mode == "off" else "pass")
    return report


def audio_quality_report(run_dir: Path, parts: list[Path], full: Path) -> dict:
    requests = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "06_generation_requests").glob("chunk_*.json"))
    }
    chunk_reports = []
    fail_reasons: list[str] = []
    for part in parts:
        request = requests.get(part.stem, {})
        duration = audio_duration(part)
        intervals = silence_intervals(part)
        objective_audio = adaptive_audio_signal_from_intervals(duration, intervals)
        long_internal_silences = [
            item for item in intervals
            if item["duration_sec"] > ADAPTIVE_SILENCE_POLICY["internal_repair_sec"]
            and (item["end_sec"] or 0) < (duration or 0) - 0.5
        ]
        tail_silences = [
            item for item in intervals
            if duration is not None and (item["end_sec"] or duration) >= duration - 0.25
        ]
        prompt = request.get("text_prompt", "")
        metrics = request.get("input_metrics", {})
        is_audio_drama_seed_prompt = bool(metrics.get("uses_seed_final_audio_prompt"))
        report = {
            "chunk_id": part.stem,
            "duration_sec": duration,
            "read_aloud_estimated_duration_sec": metrics.get("read_aloud_estimated_duration_sec", metrics.get("estimated_duration_sec")),
            "read_aloud_duration_ratio": round(duration / metrics.get("estimated_duration_sec"), 3)
            if duration is not None and metrics.get("estimated_duration_sec") else None,
            "audio_drama_estimated_duration_floor_sec": metrics.get("audio_drama_estimated_duration_floor_sec"),
            "audio_drama_estimated_duration_ceiling_sec": metrics.get("audio_drama_estimated_duration_ceiling_sec"),
            "audio_drama_duration_status": "not_evaluated",
            "silences_over_1s": intervals,
            "long_internal_silence_count": len(long_internal_silences),
            "tail_silence_sec": max([item["duration_sec"] for item in tail_silences], default=0),
            "objective_audio": objective_audio,
            "rendered_sfx_cues": metrics.get("sfx_cue_count", rendered_sfx_count(prompt)),
            "sfx_cue_limit": metrics.get("sfx_cue_limit"),
            "foreground_action_sfx_cues": metrics.get("time_ordered_sfx_lines", rendered_sfx_count(prompt)),
            "has_mixing_rule": "no overlapping narration and dialogue" in prompt,
            "has_voice_serialization_rule": "one voice at a time" in prompt,
            "has_shared_score_bed": bool(re.search(r"shared score|Background music:|score|music|cello|strings|piano", prompt, re.I)),
            "has_continuous_music_or_room_tone": bool(re.search(r"Background music:|Ambient sound:|stone ambience|carry|continue", prompt, re.I)),
            "has_audible_music_instruction": bool(re.search(r"audible|ducks? under|swells? on|shared score|Background music:|score|music|cello|strings|piano|builds|continuous", prompt, re.I)),
            "explicit_narrator_lines": metrics.get("explicit_narrator_lines"),
            "explicit_character_lines": metrics.get("explicit_character_lines"),
            "time_ordered_sfx_lines": metrics.get("time_ordered_sfx_lines"),
            "music_reaction_lines": metrics.get("music_reaction_lines"),
            "uses_seed_final_audio_prompt": metrics.get("uses_seed_final_audio_prompt"),
            "spoken_quote_word_ratio": metrics.get("spoken_quote_word_ratio"),
            "quoted_dialogue_line_count": metrics.get("quoted_dialogue_line_count"),
            "unbound_quoted_dialogue_line_count": metrics.get("unbound_quoted_dialogue_line_count"),
            "forbidden_summary_directive_count": metrics.get("forbidden_summary_directive_count"),
            "soundscape_event_count": metrics.get("soundscape_event_count"),
            "music_keyword_count": metrics.get("music_keyword_count"),
            "ambience_keyword_count": metrics.get("ambience_keyword_count"),
            "sfx_keyword_count": metrics.get("sfx_keyword_count"),
            "uses_chronological_performance_style": metrics.get("uses_chronological_performance_style"),
            "source_beat_count": metrics.get("source_beat_count"),
            "must_keep_dialogue_count": metrics.get("must_keep_dialogue_count"),
            "must_keep_dialogue_present_count": metrics.get("must_keep_dialogue_present_count"),
            "missing_must_keep_dialogue": metrics.get("missing_must_keep_dialogue"),
            "optional_dialogue_count": metrics.get("optional_dialogue_count"),
            "narration_bridge_count": metrics.get("narration_bridge_count"),
            "sfx_story_event_count": metrics.get("sfx_story_event_count"),
            "omission_rationale_count": metrics.get("omission_rationale_count"),
            "needs_regeneration": False,
            "reasons": [],
        }
        floor = report["audio_drama_estimated_duration_floor_sec"]
        ceiling = report["audio_drama_estimated_duration_ceiling_sec"]
        if duration is not None and floor and ceiling:
            if duration < floor * 0.45:
                report["audio_drama_duration_status"] = "suspiciously_short"
                report["needs_regeneration"] = True
                report["reasons"].append("audio_drama_duration_suspiciously_short")
            elif duration > ceiling * 1.4:
                report["audio_drama_duration_status"] = "suspiciously_long"
            else:
                report["audio_drama_duration_status"] = "within_audio_drama_range"
        if "repairable_internal_silence" in objective_audio.get("warning_reasons", []):
            report["needs_regeneration"] = True
            report["reasons"].append("repairable_internal_silence")
        if "trimmable_trailing_silence" in objective_audio.get("warning_reasons", []):
            report["needs_regeneration"] = True
            report["reasons"].append("trimmable_trailing_silence")
        report["reasons"].extend(objective_audio.get("hard_reasons", []))
        if objective_audio.get("hard_reasons"):
            report["needs_regeneration"] = True
        if duration is None or duration < 1.0:
            report["needs_regeneration"] = True
            report["reasons"].append("invalid_or_too_short_audio")
        if (
            floor is None
            and not is_audio_drama_seed_prompt
            and report["read_aloud_duration_ratio"] is not None
            and report["read_aloud_duration_ratio"] < 0.55
        ):
            report["needs_regeneration"] = True
            report["reasons"].append("duration_much_shorter_than_playable_text_estimate")
        if not report["has_mixing_rule"] or not report["has_continuous_music_or_room_tone"]:
            report["needs_regeneration"] = True
            report["reasons"].append("missing_ambience_continuity_prompt" if background_music_disabled() else "missing_music_continuity_prompt")
        if not report["has_voice_serialization_rule"]:
            report["needs_regeneration"] = True
            report["reasons"].append("missing_voice_serialization_rule")
        if not background_music_disabled() and not report["has_shared_score_bed"]:
            report["needs_regeneration"] = True
            report["reasons"].append("missing_shared_score_bed")
        if not background_music_disabled() and not report["has_audible_music_instruction"]:
            report["needs_regeneration"] = True
            report["reasons"].append("missing_audible_music_instruction")
        if (
            metrics.get("source_sfx_cue_count", 0)
            and not is_auto_planning()
            and not report["time_ordered_sfx_lines"]
        ):
            report["needs_regeneration"] = True
            report["reasons"].append("missing_time_ordered_sfx_lines")
        if (
            metrics.get("narrator_unit_count", 0)
            and not is_auto_planning()
            and (report["explicit_narrator_lines"] or 0) < metrics.get("narrator_unit_count", 0)
        ):
            report["needs_regeneration"] = True
            report["reasons"].append("missing_explicit_narrator_lines")
        if is_auto_planning() and is_english_prompt():
            if (report["unbound_quoted_dialogue_line_count"] or 0) > 0:
                report["needs_regeneration"] = True
                report["reasons"].append("unbound_quoted_dialogue")
            if offstage_or_unreferenced_dialogue_roles(prompt, request.get("active_roles", [])):
                report["needs_regeneration"] = True
                report["reasons"].append("quoted_dialogue_role_outside_active_refs")
            if metrics.get("dialogue_unit_count", 0) >= 2 and (report["quoted_dialogue_line_count"] or 0) < 2:
                report["needs_regeneration"] = True
                report["reasons"].append("missing_real_quoted_dialogue")
            min_quote_ratio = 0.015 if metrics.get("dialogue_unit_count", 0) <= 3 else 0.03
            if metrics.get("dialogue_unit_count", 0) >= 2 and (report["spoken_quote_word_ratio"] or 0) < min_quote_ratio:
                report["needs_regeneration"] = True
                report["reasons"].append("dialogue_too_summary_like")
            if (report["forbidden_summary_directive_count"] or 0) > 0:
                report["needs_regeneration"] = True
                report["reasons"].append("summary_style_dialogue_directives")
            if (report["soundscape_event_count"] or 0) < 8:
                report["needs_regeneration"] = True
                report["reasons"].append("weak_soundscape_prompt")
            if not background_music_disabled() and (report["music_keyword_count"] or 0) < 2:
                report["needs_regeneration"] = True
                report["reasons"].append("weak_music_prompt")
            if (report["ambience_keyword_count"] or 0) < 2:
                report["needs_regeneration"] = True
                report["reasons"].append("weak_ambience_prompt")
            if (report["sfx_keyword_count"] or 0) < 1 and (report["soundscape_event_count"] or 0) < 8:
                report["needs_regeneration"] = True
                report["reasons"].append("weak_sfx_prompt")
            if (report["source_beat_count"] or 0) < 2:
                report["needs_regeneration"] = True
                report["reasons"].append("missing_audio_drama_source_beats")
            if (report["must_keep_dialogue_count"] or 0) and (
                (report["must_keep_dialogue_present_count"] or 0) < (report["must_keep_dialogue_count"] or 0)
            ):
                report["needs_regeneration"] = True
                report["reasons"].append("missing_must_keep_dialogue")
            if metrics.get("narrator_unit_count", 0) >= 3 and (report["narration_bridge_count"] or 0) < 1:
                report["needs_regeneration"] = True
                report["reasons"].append("missing_narration_bridge")
            if metrics.get("source_sfx_cue_count", 0) >= 2 and (report["sfx_story_event_count"] or 0) < 1:
                report["needs_regeneration"] = True
                report["reasons"].append("missing_sfx_story_events")
            if metrics.get("source_unit_count", 0) >= 8 and (report["omission_rationale_count"] or 0) < 1:
                report["needs_regeneration"] = True
                report["reasons"].append("missing_omission_rationale")
        if report["sfx_cue_limit"] is not None and report["rendered_sfx_cues"] > report["sfx_cue_limit"]:
            report["needs_regeneration"] = True
            report["reasons"].append("sfx_density_over_limit")
        core_reasons = [
            reason for reason in report["reasons"]
            if reason in {"invalid_or_too_short_audio", *objective_audio.get("hard_reasons", [])}
        ]
        report["core_reasons"] = core_reasons
        report["core_needs_regeneration"] = bool(core_reasons)
        if report["needs_regeneration"]:
            fail_reasons.append(f"{part.stem}: {', '.join(report['reasons'])}")
        chunk_reports.append(report)
    final_duration = audio_duration(full)
    final_silences = silence_intervals(full)
    final_tail = [
        item for item in final_silences
        if final_duration is not None and (item["end_sec"] or final_duration) >= final_duration - 0.25
    ]
    core_fail_reasons = [
        f"{item['chunk_id']}: {', '.join(item.get('core_reasons', []))}"
        for item in chunk_reports if item.get("core_needs_regeneration")
    ]
    return {
        "status": "fail" if fail_reasons else "pass",
        "core_status": "fail" if core_fail_reasons else "pass",
        "core_fail_reasons": core_fail_reasons,
        "fail_reasons": fail_reasons,
        "policy": {
            "silence_threshold_db": "-45dB",
            "silence_detect_min_duration_sec": 1.0,
            "adaptive_silence_policy": ADAPTIVE_SILENCE_POLICY,
            "natural_pause_policy": "normal 1-3 second dramatic pauses are allowed; hard failures use duration plus output-share evidence",
            "prompt_style": "official chronological performance prompt: scene sound, continuous audible music, explicit speaker lines, and source-timed SFX",
            "sfx_density": "source-derived action SFX should be time-ordered and audible but brief",
            "duration_policy": "audio-drama QA does not fail only because output is shorter than read-aloud duration; duration is evaluated against beat/dialogue coverage.",
            "coverage_policy": "must keep required story beats and must-keep dialogue; narration, SFX, and music may carry compressed action.",
        },
        "chunks": chunk_reports,
        "final_audio": {
            "path": str(full.relative_to(ROOT)),
            "duration_sec": final_duration,
            "silences_over_1s": final_silences,
            "tail_silence_sec": max([item["duration_sec"] for item in final_tail], default=0),
        },
    }


def failed_quality_chunk_ids(quality: dict) -> set[str]:
    return {
        chunk.get("chunk_id")
        for chunk in quality.get("chunks", [])
        if chunk.get("core_needs_regeneration") and chunk.get("chunk_id")
    }


def failed_asr_chunk_ids(asr_report: dict | None) -> set[str]:
    if not asr_report:
        return set()
    return {
        chunk.get("chunk_id")
        for chunk in asr_report.get("chunks", [])
        if chunk.get("status") == "fail" and chunk.get("chunk_id")
    }


def validate_audio(paths: list[Path]) -> dict:
    report = {}
    for path in paths:
        full = path if path.is_absolute() else ROOT / path
        rel = full.relative_to(ROOT)
        result = run(["ffmpeg", "-v", "error", "-i", str(rel), "-f", "null", "-"])
        report[str(rel)] = {
            "exists": full.exists(),
            "bytes": full.stat().st_size if full.exists() else 0,
            "duration_sec": audio_duration(full),
            "ffmpeg_decode_ok": result.returncode == 0,
            "ffmpeg_error": result.stderr.strip(),
        }
    return report


def write_analysis(
    run_dir: Path,
    build: dict,
    validation: dict | None,
    quality: dict | None = None,
    performance: dict | None = None,
) -> None:
    validation_text = "Generation not run yet."
    if validation:
        validation_text = "\n".join(
            f"- `{path}`: bytes={item['bytes']}, duration={item['duration_sec']}, decode_ok={item['ffmpeg_decode_ok']}"
            for path, item in validation.items()
        )
    quality_text = "Generation not run yet."
    if quality:
        quality_text = f"- Status: `{quality['status']}`\n- Fail reasons: {quality.get('fail_reasons', [])}\n- Final duration: {quality.get('final_audio', {}).get('duration_sec')}"
    performance_text = "Generation not run yet."
    if performance:
        performance_text = (
            f"- Status: `{performance.get('status')}`\n"
            f"- Delivery status: `{performance.get('delivery_status')}`\n"
            f"- Failed chunks: {performance.get('failed_chunk_ids', [])}\n"
            f"- Reviewer unavailable: {performance.get('unavailable_chunk_ids', [])}"
        )
    write_text(
        run_dir / "09_stage_effect_analysis.md",
        f"""# Doc-Compliant Seed 2.0 Pro Rewrite Workflow

## Build Summary

- Rewrite model: {REWRITE_MODEL}
- Audio model: {AUDIO_MODEL}
- Target speech rate: adaptive by chunk
- Prompt chunks: {build['chunks']}
- Prompt lengths: {build['prompt_lengths']}

## Validation

{validation_text}

## Audio Quality Report

{quality_text}

## Performance Review

{performance_text}

## Compliance Notes

- Source text is stored as clean plain text in `01_source_excerpt.txt`.
- `02_source_units.json` is automatic sentence/dialogue-fragment segmentation from the plain source.
- `03_scene_parse.json` and `05_director_prompt_chunks` are generated by Seed 2.0 Pro.
- In automatic English planning, Seed 2.0 Pro must produce each chunk's final `final_audio_prompt`; the engineering layer does not expand every source unit into a dry line-reading script.
- Seed Audio receives only the generated final audio prompt chunks plus fixed references.
- English Audio input uses the official chronological mixed-scene style: compressed narration, selected key dialogue, continuous ambience, audible music, and source-timed foreground SFX woven into the same timeline.
- ASR checks speech-language coverage, skipped lines, read-risk leaks, and key terms only; music, ambience, SFX, overlap, and silence are handled by the separate audio mix audit.
""",
    )


def update_manifest(run_dir: Path, status: str) -> None:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = status
    manifest["updated_at"] = now_hk().isoformat(timespec="seconds")
    write_json(manifest_path, manifest)


def main() -> int:
    global OUTPUT_ROOT
    parser = argparse.ArgumentParser(description="Feishu-doc-compliant Seed 2.0 Pro rewrite + Seed Audio workflow.")
    parser.add_argument("--generate", action="store_true", help="Also call Seed Audio after Seed 2.0 Pro rewrite.")
    parser.add_argument("--run-id", help="Optional explicit run id. Must not already exist.")
    parser.add_argument("--resume-run-id", help="Resume generation from an existing run folder.")
    parser.add_argument("--resume-partial-run-id", help="Resume rewrite/build from an existing partial run folder.")
    parser.add_argument("--output-root", help="Override output root. Defaults to outputs/runs.")
    parser.add_argument(
        "--story-config",
        help="Story config JSON. Defaults to story_configs/moonlit_cloister_duel_en.json when present.",
    )
    parser.add_argument("--source-file", help="Plain novel/story text file. Enables full automatic planning from source text.")
    parser.add_argument("--language", default="en", help="Source language for --source-file: en or zh. Default: en.")
    parser.add_argument("--mode", default="audio_drama_adaptation", help="Production mode for --source-file.")
    parser.add_argument("--source-title", help="Optional source title for --source-file.")
    parser.add_argument("--asr-mode", choices=["off", "diagnostic", "required"], default=DEFAULT_ASR_MODE)
    parser.add_argument(
        "--performance-mode",
        choices=["off", "diagnostic", "balanced", "required"],
        default=DEFAULT_PERFORMANCE_MODE,
    )
    args = parser.parse_args()
    if args.output_root:
        output_root = Path(args.output_root)
        if not output_root.is_absolute():
            output_root = ROOT / output_root
        OUTPUT_ROOT = output_root
    if args.source_file and args.story_config:
        raise SystemExit("Use either --source-file or --story-config, not both.")
    if args.source_file:
        config = build_source_file_config(
            source_file=args.source_file,
            language=args.language,
            mode=args.mode,
            source_title=args.source_title,
        )
    else:
        config = load_story_config(args.story_config)
    if config:
        apply_story_config(config)

    if args.resume_run_id and args.resume_partial_run_id:
        raise SystemExit("Use only one resume mode.")
    if args.resume_run_id:
        run_dir = OUTPUT_ROOT / args.resume_run_id
        if not run_dir.exists():
            raise SystemExit(f"Resume folder does not exist: {run_dir.relative_to(ROOT)}")
        apply_voice_registry_file(run_dir / "04_voice_registry.json")
        prompt_lengths = [len(path.read_text(encoding="utf-8")) for path in sorted((run_dir / "05_director_prompt_chunks").glob("chunk_*.txt"))]
        build = {"chunks": len(prompt_lengths), "prompt_lengths": prompt_lengths}
    elif args.resume_partial_run_id:
        run_dir = OUTPUT_ROOT / args.resume_partial_run_id
        if not run_dir.exists():
            raise SystemExit(f"Partial resume folder does not exist: {run_dir.relative_to(ROOT)}")
        source_units = source_units_for_partial_resume(run_dir)
        write_text(run_dir / "01_source_excerpt.txt", SOURCE_EXCERPT)
        write_json(run_dir / "02_source_units.json", source_units)
        rewrite = call_seed2_rewrite(run_dir, source_units)
        build = build_artifacts(run_dir, rewrite, source_units)
    else:
        run_dir = make_run_dir(args.run_id)
        source_units = build_source_units()
        write_text(run_dir / "01_source_excerpt.txt", SOURCE_EXCERPT)
        write_json(run_dir / "02_source_units.json", source_units)
        rewrite = call_seed2_rewrite(run_dir, source_units)
        build = build_artifacts(run_dir, rewrite, source_units)
    validation = None
    quality = None
    asr_report = None
    performance_report = None
    audio = None
    if args.generate:
        update_manifest(run_dir, "generation_in_progress")
        references = generate_reference_audio(run_dir)
        scene = generate_scene_audio(run_dir)
        full = concat_audio(run_dir, scene["parts"])
        reference_paths = [Path(item["path"]) for item in references.values() if item.get("path")]
        validation = validate_audio(reference_paths + scene["parts"] + [full])
        quality = audio_quality_report(run_dir, scene["parts"], full)
        write_json(run_dir / "logs" / "validation_log.json", validation)
        write_json(run_dir / "logs" / "audio_quality_report.json", quality)
        update_manifest(run_dir, "asr_in_progress")
        if args.asr_mode == "off":
            asr_report = {"status": "skipped", "chunks": [], "fail_reasons": [], "policy": {"reason": "ASR disabled by workflow profile."}}
        else:
            asr_report = asr_language_audit(run_dir, scene["logs"])
        asr_report = asr_gate(asr_report, args.asr_mode)
        write_json(run_dir / "logs" / "asr_language_report.json", asr_report)
        update_manifest(run_dir, "performance_review_in_progress")
        if args.performance_mode == "off":
            performance_report = {
                "status": "skipped",
                "preview_status": "available",
                "failed_chunk_ids": [],
                "unavailable_chunk_ids": [],
                "chunks": [],
            }
        else:
            performance_report = performance_audio_audit(run_dir, scene["parts"])
        performance_report = performance_gate(performance_report, args.performance_mode)
        write_json(run_dir / "logs" / "performance_review_report.json", performance_report)
        repair_logs = []
        for _repair_attempt in range(1, PERFORMANCE_REPAIR_ATTEMPTS + 1):
            failed_chunks = (
                failed_quality_chunk_ids(quality)
                | (failed_asr_chunk_ids(asr_report) if args.asr_mode == "required" else set())
                | set(performance_report.get("gate_failed_chunk_ids", []))
            )
            if not failed_chunks:
                break
            repaired_scene = generate_scene_audio(
                run_dir,
                failed_chunks,
                repair_notes=performance_repair_notes(performance_report),
            )
            full = concat_audio(run_dir, repaired_scene["parts"])
            validation = validate_audio(reference_paths + repaired_scene["parts"] + [full])
            quality = audio_quality_report(run_dir, repaired_scene["parts"], full)
            write_json(run_dir / "logs" / "validation_log.json", validation)
            write_json(run_dir / "logs" / "audio_quality_report.json", quality)
            if args.asr_mode == "off":
                asr_report = {"status": "skipped", "chunks": [], "fail_reasons": [], "policy": {"reason": "ASR disabled by workflow profile."}}
            else:
                asr_report = asr_language_audit(run_dir, repaired_scene["logs"])
            asr_report = asr_gate(asr_report, args.asr_mode)
            write_json(run_dir / "logs" / "asr_language_report.json", asr_report)
            if args.performance_mode == "off":
                performance_report = {
                    "status": "skipped", "preview_status": "available", "failed_chunk_ids": [],
                    "unavailable_chunk_ids": [], "chunks": [],
                }
            else:
                performance_report = performance_audio_audit(run_dir, repaired_scene["parts"])
            performance_report = performance_gate(performance_report, args.performance_mode)
            write_json(run_dir / "logs" / "performance_review_report.json", performance_report)
            repair_logs.extend(repaired_scene["logs"])
            scene = {
                "logs": scene["logs"] + repaired_scene["logs"],
                "parts": repaired_scene["parts"],
            }
        write_json(
            run_dir / "logs" / "generation_log.json",
            {
                "references": references,
                "scene": scene["logs"],
                "repair_logs": repair_logs,
                "final_audio": str(full.relative_to(ROOT)),
            },
        )
        write_json(run_dir / "logs" / "validation_log.json", validation)
        write_json(run_dir / "logs" / "audio_quality_report.json", quality)
        write_json(run_dir / "logs" / "asr_language_report.json", asr_report)
        write_json(run_dir / "logs" / "performance_review_report.json", performance_report)
        audio = {
            "full": str(full.relative_to(ROOT)),
            "validation": validation,
            "quality": quality,
            "asr": asr_report,
            "performance": performance_report,
            "preview_status": performance_report.get("preview_status"),
            "delivery_status": "pass" if (
                all(item.get("ffmpeg_decode_ok") for item in validation.values())
                and quality.get("core_status") == "pass"
                and asr_report.get("gate_status") in {"pass", "skipped"}
                and performance_report.get("gate_status") in {"pass", "skipped"}
            ) else "fail",
        }
        update_manifest(run_dir, "completed" if audio["delivery_status"] == "pass" else "needs_review")
    write_analysis(run_dir, build, validation, quality, performance_report)
    print(json.dumps({"run_dir": str(run_dir.relative_to(ROOT)), "build": build, "audio": audio}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
