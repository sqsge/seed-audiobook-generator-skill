#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import llm_chat
import tts_client
import asr_client
import common


ROOT = Path(__file__).resolve().parents[1]
SEED_AUDIO_CLIENT = Path(__file__).resolve().parent / "seed_audio_client.py"
OUTPUT_ROOT = ROOT / "outputs" / "runs"
STORY_CONFIG: dict | None = None
SCENE_ID = "moonlit_cloister_duel_en"
PRODUCTION_MODE = "audio_drama_adaptation"
WORKFLOW_INPUT_MODE = "story_config"
REWRITE_MODEL = "seed-2-0-pro-260328"
AUDIO_MODEL = "seed-audio-1.0"
VOICE_REGISTRY_VERSION = "voice_registry_seed2pro_20260702_001"
MAX_PROMPT_CHARS = int(os.getenv("SEED_AUDIO_MAX_CHARS", "3000"))
TARGET_AUDIO_PROMPT_CHARS = int(os.getenv("SEED_AUDIO_TARGET_CHARS", "2200"))
TARGET_SPEECH_RATE = 24
ENGLISH_TARGET_SPEECH_RATE = int(os.getenv("SEED_AUDIO_EN_SPEECH_RATE", "0"))
PERFORMANCE_REVIEW_MODEL = os.getenv("SEED_AUDIO_PERFORMANCE_REVIEW_MODEL", "seed-2-0-lite-260428")
PERFORMANCE_REVIEW_TIMEOUT = int(os.getenv("SEED_AUDIO_PERFORMANCE_REVIEW_TIMEOUT", "75"))
REWRITE_MAX_TOKENS = int(os.getenv("SEED_AUDIO_REWRITE_MAX_TOKENS", "6000"))
POSTPROCESS_TIMING = common.get_env_bool("SEED_AUDIO_POSTPROCESS_TIMING", False)
HK_TZ = timezone(timedelta(hours=8))

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
    global ROLE_SPECS, ROLE_KEY_TO_ROLE, ROLE_SPOKEN_NAMES, NARRATOR_LABEL
    STORY_CONFIG = config
    SCENE_ID = config.get("scene_id", SCENE_ID)
    PRODUCTION_MODE = config.get("production_mode", PRODUCTION_MODE)
    SOURCE_TITLE = config.get("source_title", SOURCE_TITLE)
    SOURCE_URL = config.get("source_url", SOURCE_URL)
    SOURCE_EXCERPT = simplify_text(config.get("source_excerpt", SOURCE_EXCERPT))
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
QUOTE_RE = re.compile(r"「([^」]+)」|“([^”]+)”|\"([^\"]+)\"")
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
    units: list[dict] = []
    order = 1
    paragraphs = SOURCE_EXCERPT.strip().split("\n\n")
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        paragraph_id = f"p{paragraph_index:02d}"
        pos = 0
        for quote_match in QUOTE_RE.finditer(paragraph):
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


def now_hk() -> datetime:
    return datetime.now(HK_TZ)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


def compact_rewrite_prompt(source_units: list[dict]) -> str:
    allowed_speakers = "|".join(ROLE_SPECS.keys())
    speaker_candidates = ENGLISH_SPEAKER_CANDIDATES if is_english_prompt() else CHINESE_SPEAKER_CANDIDATES
    language_name = "English" if is_english_prompt() else "Chinese"
    role_lock_instruction = ""
    if is_auto_planning():
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
7. Create chunk_plan automatically. Each chunk should be a coherent cinematic sound scene, not a fixed character count.
8. Chunk constraints: target 1-2 chunks for a 2-minute excerpt; use 3 chunks only if a final_audio_prompt would exceed {MAX_PROMPT_CHARS} chars. Every character who has quoted dialogue in a chunk must be included in that chunk's active_roles and get a reference marker. If more than 3 characters need quoted dialogue, split the chunk instead of letting an unreferenced character speak.
9. For each chunk, infer:
   - title
   - source_unit_ids
   - active_roles: the exact 1-3 roles bound to <<TGT_SPK1>>, <<TGT_SPK2>>, <<TGT_SPK3>> in this chunk, in order
   - persistent_ambience from place/time/space
   - music_bed with concrete style, instruments, rhythm, and emotional function
   - sound_design with action-anchored SFX
   - speech_rate from -4 to 8, adaptive to scene pace. Keep action natural-medium or measured-brisk, never fast enough to blur syllables.
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
13a. Urgency must come from acting, sound, and music, never rushed diction. In action-heavy material, use fewer spoken turns per chunk, let one foreground action event land at a time, and preserve clear consonants, breath, and the end of every sentence. End each chunk only after a completed spoken thought and a short natural ambience tail; never end on a spell, shout, unfinished word, or music hit.
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
   - no more than 4 explicit Narrator narrates lines in a chunk
   - do not say "Do not read", "instruction", "source_unit", "SFX cue", "narrate", or "deliver the line" inside the final_audio_prompt
   - any narration that should be spoken must be an explicit Narrator line with actor marker and quoted English text
   - action, ambience, music, and SFX directions must be phrased as production sound events, not as prose that could be read aloud
   - action urgency must be expressed by performance and sound design, not rapid reading; preserve a natural breath and sentence ending before each chunk boundary
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


def longform_section_planning_prompt(source_units: list[dict]) -> str:
    """A bounded planning contract for chapter sections.

    The final Seed Audio prompt is built from this structured plan by the
    existing official-style composer. Asking the model to also emit that long
    prompt, metrics, reviews, and duplicate schemas made a 2.7k-character
    source section expand into a 24k-character rewrite request.
    """
    allowed_roles = list(ROLE_SPECS)
    compact_units = [
        {
            "source_unit_id": unit["source_unit_id"],
            "source_kind": unit["source_kind"],
            "source_text": unit["source_text"],
            **({"quote_attribution_text": unit["quote_attribution_text"]} if unit.get("quote_attribution_text") else {}),
        }
        for unit in source_units
    ]
    return f"""Plan this English fiction section for a Seed Audio 1.0 audio drama. Return one compact JSON object only.

Use only these fixed speaker ids: {json.dumps(allowed_roles)}. Preserve every source_unit_id exactly once and in original order. Keep story meaning and quoted dialogue intent. Urgency must come from acting, music, and action sound, never rushed speech. Keep speech clear, sequential, and end every chunk after a complete thought.

For every parsed source unit provide only: source_unit_id, type (narration|dialogue|monologue), speaker, speaker_evidence, emotion, narration_style, delivery, sfx_before, sfx_during, sfx_after, sfx_layer, sfx_importance, sfx_reason, music_intent. The workflow preserves each original source_text verbatim unless a later audio-adaptation step explicitly changes it.

Create 1-3 coherent chunk_plan entries. Each entry needs: title, source_unit_ids, active_roles (1-3 from the fixed list), persistent_ambience, music_bed, sound_design, speech_rate (-4 to 8), pace_note, continuity, expected_duration_sec, source_beats, must_keep_dialogue, optional_dialogue, narration_bridge, sfx_story_events, omission_rationale. Do not include a final_audio_prompt; the workflow composes the provider-safe timeline from your plan.

JSON shape:
{{"parsed_source_units":[{{"source_unit_id":"s0001","type":"narration","speaker":"Narrator","speaker_evidence":"context","emotion":"...","narration_style":"action_narration","delivery":"...","sfx_before":[],"sfx_during":[],"sfx_after":[],"sfx_layer":"foreground","sfx_importance":"high","sfx_reason":"...","music_intent":"..."}}],"chunk_plan":[{{"title":"...","source_unit_ids":["s0001"],"active_roles":["Narrator"],"persistent_ambience":"...","music_bed":"...","sound_design":"...","speech_rate":0,"pace_note":"...","continuity":{{"previous_context_summary":"...","chunk_opening_state":"...","chunk_ending_state":"...","next_context_hint":"..."}},"expected_duration_sec":{{"min":20,"max":90}},"source_beats":["..."],"must_keep_dialogue":[],"narration_bridge":["..."],"sfx_story_events":["..."],"omission_rationale":["..."]}}]}}

source_units:
{json.dumps(compact_units, ensure_ascii=False, separators=(",", ":"))}
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
    planned_roles = []
    for role in chunk.get("active_roles", []) or []:
        if role in ROLE_SPECS and role not in planned_roles:
            planned_roles.append(role)
    if planned_roles:
        return planned_roles[:3]
    if is_english_prompt():
        counts: dict[str, int] = {}
        for unit_id in chunk.get("source_unit_ids", []):
            speaker = parsed_by_id[unit_id].get("speaker", "Narrator")
            if speaker not in ROLE_SPECS:
                speaker = "Narrator"
            if speaker == "Narrator":
                continue
            counts[speaker] = counts.get(speaker, 0) + 1
        roles = ["Narrator"] if "Narrator" in ROLE_SPECS else []
        roles.extend(
            role
            for role, _count in sorted(counts.items(), key=lambda item: (-item[1], list(ROLE_SPECS).index(item[0])))
            if role not in roles
        )
        for role in ROLE_SPECS:
            if len(roles) >= 3:
                break
            if role not in roles:
                roles.append(role)
        return roles[:3] or list(ROLE_SPECS.keys())[:1] or ["Narrator"]
    roles = [] if is_english_prompt() else ["Narrator"]
    for unit_id in chunk.get("source_unit_ids", []):
        speaker = parsed_by_id[unit_id].get("speaker", "Narrator")
        if speaker not in ROLE_SPECS:
            speaker = "Narrator"
        if speaker not in roles:
            roles.append(speaker)
    if not roles:
        roles.append("Narrator")
    return roles[:3]


def roles_for_unit_ids(unit_ids: list[str], parsed_by_id: dict) -> list[str]:
    roles: list[str] = []
    for unit_id in unit_ids:
        speaker = parsed_by_id.get(unit_id, {}).get("speaker", "Narrator")
        if speaker not in ROLE_SPECS:
            speaker = "Narrator"
        if speaker not in roles:
            roles.append(speaker)
    return roles


def local_plan_fields_for_units(plan: dict, unit_ids: list[str], parsed_by_id: dict) -> dict:
    unit_set = set(unit_ids)
    units = [parsed_by_id[unit_id] for unit_id in unit_ids if unit_id in parsed_by_id]
    source_beats = []
    sfx_events = []
    narration_bridge = []
    for unit in units:
        text = clean_prompt_quote_text(unit.get("adapted_text") or unit.get("source_text", ""))
        if text and unit.get("speaker") == "Narrator" and len(narration_bridge) < 4:
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
    }
    for field in ("must_keep_dialogue", "optional_dialogue", "dialogue_lines"):
        values = plan.get(field)
        if isinstance(values, list):
            filtered[field] = [
                item for item in values
                if not isinstance(item, dict) or not item.get("source_unit_id") or item.get("source_unit_id") in unit_set
            ]
    return filtered


def repair_quoted_speakers(parsed_units: list[dict]) -> list[dict]:
    repaired = [dict(unit) for unit in parsed_units]
    for index, unit in enumerate(repaired):
        if unit.get("source_kind") != "quoted_text":
            continue
        source_text = str(unit.get("source_text", ""))
        if re.search(r"\bHarry\b", source_text, re.I) and re.search(r"\bYeh\b|\bter\b|\bmusta\b|\bye\b", source_text, re.I):
            unit["speaker"] = "Hagrid"
            unit["speaker_evidence"] = "engineering speaker repair from dialect and Harry vocative"
            continue
        if re.search(r"\bHagrid\b", source_text, re.I) and not re.search(r"\bYeh\b|\bter\b|\bmusta\b|\bye\b", source_text, re.I):
            unit["speaker"] = "Harry Potter"
            unit["speaker_evidence"] = "engineering speaker repair from Hagrid vocative in non-Hagrid line"
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
        current_score = scores.get(current, 0)
        if best_role != current and best_score > current_score:
            unit["speaker"] = best_role
            unit["speaker_evidence"] = f"engineering speaker repair from adjacent attribution/context: {context[:180]}"
    return repaired


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
        if len(roles_for_unit_ids(unit_ids, parsed_by_id)) <= max_roles:
            repaired.append(plan)
            continue
        sub_ids: list[str] = []
        sub_index = 1
        for unit_id in unit_ids:
            candidate = sub_ids + [unit_id]
            if sub_ids and len(roles_for_unit_ids(candidate, parsed_by_id)) > max_roles:
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
            if len(roles_for_unit_ids(candidate_ids, parsed_by_id)) <= 3:
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
            if len(roles_for_unit_ids(candidate_ids, parsed_by_id)) <= 3:
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


def enforce_max_chunk_count(plans: list[dict], parsed_by_id: dict, target_max: int = 6) -> list[dict]:
    repaired = list(plans)
    while len(repaired) > target_max:
        best_index: int | None = None
        best_size: int | None = None
        for index in range(len(repaired) - 1):
            left = repaired[index]
            right = repaired[index + 1]
            candidate_ids = list(left.get("source_unit_ids", [])) + list(right.get("source_unit_ids", []))
            if not is_english_prompt() and len(roles_for_unit_ids(candidate_ids, parsed_by_id)) > 3:
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
    if not is_english_prompt() and len(roles_for_unit_ids(candidate_ids, parsed_by_id)) > 3:
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
            "expected_duration_sec": {"min": 20, "max": 120},
        }

    plans: list[dict] = []
    current: list[str] = []
    for unit in parsed_units:
        unit_id = unit["source_unit_id"]
        candidate = current + [unit_id]
        if current and not is_english_prompt() and len(roles_for_unit_ids(candidate, parsed_by_id)) > 3:
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
    text = re.sub(r"\s+", " ", text).strip(" ,.;")
    return text


def audio_safe_pace_note(value: str) -> str:
    text = clean_english_director_text(value)
    risky = re.search(r"\bsilence|pause|stillness|extremely\s+slow|very\s+slow|no\s+rhythm\b", text, re.I)
    if risky or not text:
        return "natural measured pace, emotionally tense but connected, brief natural beats only, no empty gaps"
    return compact_clause_text(text, "natural measured pace, connected transitions, no empty gaps", 120)


def english_score_bible() -> str:
    return "a clearly audible cinematic fantasy score with low cello drone, glass harmonics, soft piano pulse, and distant choir texture"


def english_audible_music_text(value: str) -> str:
    text = clean_english_director_text(value or "low cinematic tension")
    text = re.sub(r"\badd quiet\b", "stay clearly audible beneath the voices", text, flags=re.I)
    text = re.sub(r"\bquietly\b", "clearly but softly", text, flags=re.I)
    text = re.sub(r"\bvery quiet\b", "soft but audible", text, flags=re.I)
    text = re.sub(r"\bsubtle\b", "audible and restrained", text, flags=re.I)
    text = re.sub(r"\bfade(?:s)?\b", "carry forward softly", text, flags=re.I)
    text = re.sub(r"\bduck(?:s)?\b", "lower briefly", text, flags=re.I)
    return text or "low cinematic tension"


def english_music_instruction(chunk: dict, index: int, total: int) -> str:
    music = english_audible_music_text(chunk.get("music_bed", "soft strings stay low under speech"))
    if index != total:
        music = re.sub(r",?\s*(?:quick\s+)?fade(?:s)?(?:\s+(?:out|under [^,.;]+|after [^,.;]+|into [^,.;]+))?", "", music, flags=re.I).strip(" ,.;")
        music = english_audible_music_text(music)
        music = music or "soft strings stay low under speech"
    score = english_score_bible()
    if total == 1:
        return f"Background music: {score}. Keep it present throughout the whole scene, clearly audible in pauses, lower under dialogue, swell on magic and impacts, and resolve after the last beat; contour: {music}."
    if index == 1:
        return f"Background music: Start {score}. Keep it present throughout the chunk, clearly audible in pauses, lower under dialogue, swell on magic and impacts, and carry into the next chunk; contour: {music}."
    if index == total:
        return f"Background music: Continue the same shared score. Keep it present under the voices, swell on the final turn, and resolve naturally after the last line; contour: {music}."
    return f"Background music: Continue the same shared score without a cadence. Keep it present throughout the chunk, clearly audible in pauses, lower under dialogue, swell on action, and carry forward; contour: {music}."


def english_music_actions(index: int, total: int) -> list[str]:
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
            r"^(?:A brief .+ cuts through|[A-Z][^.\n]+ stays audible around|[A-Z][^.\n]+ rings out)",
            prompt,
            flags=re.M,
        )
    )


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
        "music_reaction_lines": len(re.findall(r"^The background music swells briefly here", prompt, flags=re.M)),
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


def seed_final_audio_prompt(chunk: dict) -> str:
    return str(chunk.get("final_audio_prompt") or chunk.get("audio_prompt") or chunk.get("text_prompt") or "").strip()


def normalize_final_audio_prompt(prompt: str, active_roles: list[str], refs: dict[str, str], chunk: dict, index: int, total: int) -> str:
    prompt = prompt.strip()
    prompt = re.sub(r"^\s*VOICE\s+ROLES\s*:\s*", "Voice continuity: ", prompt, flags=re.I)
    prompt = re.sub(r"^\s*Voice\s+bindings\s+for\s+this\s+scene\s*:\s*", "Voice continuity: ", prompt, flags=re.I | re.M)
    prompt = re.sub(r"^\s*Voice\s+mapping(?:\s+for\s+this\s+scene)?\s*:\s*", "Voice continuity: ", prompt, flags=re.I | re.M)
    prompt = re.sub(r"^\s*Voices\s*:\s*", "Voice continuity: ", prompt, flags=re.I | re.M)
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

    production_header = (
        "Scene mix: "
        f"continuous ambience: {compact_plan_text(chunk.get('persistent_ambience', ''), 'source-derived ambience', 180)}. "
        f"Background music: {compact_plan_text(chunk.get('music_bed', ''), 'continuous score supports the whole scene', 240)}. "
        f"Foreground sound design: {compact_plan_text(chunk.get('sound_design', ''), 'source-motivated action effects', 220)}."
    )
    if "Scene mix:" not in prompt and len(production_header) + len(prompt) + 1 <= MAX_PROMPT_CHARS:
        prompt = f"{production_header}\n{prompt}"

    voice_parts = []
    for role in active_roles:
        ref = refs.get(role)
        if not ref:
            continue
        label = speaker_label(role)
        description = compact_plan_text(ROLE_SPECS.get(role, {}).get("description", "expressive voice"), "expressive voice", 70)
        voice_parts.append(f"{label} uses {ref} ({description}, actor is {ref})")
    voice_line = "Voice continuity: " + "; ".join(voice_parts) + ". Use adaptive pace, one voice at a time with no overlapping narration and dialogue."
    language_lock = "All audible speech must be English only. Do not translate or speak Chinese."

    if "Voice continuity:" not in prompt and len(voice_line) + len(prompt) + 1 <= MAX_PROMPT_CHARS:
        prompt = f"{voice_line}\n{prompt}"
    elif "one voice at a time" not in prompt or "no overlapping narration and dialogue" not in prompt:
        prompt = f"{voice_line}\n{prompt}"
    if language_lock not in prompt:
        prompt = f"{language_lock}\n{prompt}"
    if not re.search(r"Ambient sound:|ambience|ambient", prompt, re.I):
        ambience = compact_clause_text(chunk.get("persistent_ambience", ""), "continuous source-derived ambience", 120)
        prompt = f"Ambient sound: {ambience} stays present under the whole scene.\n{prompt}"
    if not re.search(r"Background music:|score|music", prompt, re.I):
        prompt = f"{english_music_instruction(chunk, index, total)}\n{prompt}"
    if not re.search(r"Sound design:|foreground|whoosh|clang|impact|shatter|spell", prompt, re.I):
        sound = compact_clause_text(chunk.get("sound_design", ""), "source-motivated foreground action sounds", 130)
        prompt = f"Sound design: {sound} happens as foreground cinematic sound, not spoken labels.\n{prompt}"
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


def dialogue_line_for_unit(unit: dict, refs: dict[str, str], first_seen: bool) -> str:
    role = unit.get("speaker", "Narrator")
    label = speaker_label(role)
    ref = refs.get(role)
    emotion = clean_english_director_text(str(unit.get("emotion", "natural")))
    text = clean_prompt_quote_text(unit.get("adapted_text") or unit.get("source_text", ""))
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
    bridges = [clean_prompt_quote_text(item) for item in chunk.get("narration_bridge", []) if str(item).strip()]
    bridges = [item for item in bridges if item and normalized_fragment(item) not in normalized_fragment(prompt)]
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
    voice_parts = []
    for role in active_roles:
        ref = refs.get(role)
        if not ref:
            continue
        voice_parts.append(f"{speaker_label(role)} uses {ref} (actor is {ref})")
    ambience = compact_clause_text(chunk.get("persistent_ambience", ""), "continuous source-derived ambience", 145)
    sound = compact_clause_text(chunk.get("sound_design", ""), "source-motivated foreground action effects", 150)
    pace = audio_safe_pace_note(chunk.get("pace_note", ""))
    source_beats = [clean_prompt_quote_text(item) for item in chunk.get("source_beats", []) if str(item).strip()]
    bridges = [clean_prompt_quote_text(item) for item in chunk.get("narration_bridge", []) if str(item).strip()]
    if not bridges and source_beats:
        bridges = source_beats[:2]
    elif len(bridges) < 3:
        existing = {normalized_fragment(item) for item in bridges}
        for beat in source_beats:
            if normalized_fragment(beat) not in existing:
                bridges.append(beat)
                existing.add(normalized_fragment(beat))
            if len(bridges) >= 3:
                break
    sfx_events = [clean_english_director_text(item) for item in chunk.get("sfx_story_events", []) if str(item).strip()]
    if not sfx_events:
        sfx_events = [item.strip() for item in re.split(r"[,.;]", str(chunk.get("sound_design", ""))) if item.strip()]

    ambience = clean_english_director_text(ambience)
    sound = clean_english_director_text(sound)
    lines = [
        "All audible speech must be English only. Do not translate or speak Chinese.",
        "Voice continuity: " + "; ".join(voice_parts) + f". Use {pace}; one voice at a time with no overlapping narration and dialogue.",
        f"Ambient sound: {ambience} stays continuous under the whole scene, keeping low room tone between lines.",
        english_music_instruction(chunk, index, total),
        f"Sound design: {sound} happens as foreground cinematic sound where the source action demands it.",
        "Scripted performance: perform every quoted Narrator and character line below exactly once, in order; do not skip, shorten, or replace them with summary.",
        "",
    ]

    narrator_ref = refs.get("Narrator")
    for bridge in bridges[:2]:
        if narrator_ref:
            lines.append(f'Narrator (actor is {narrator_ref}, tense connective narration): "{bridge}"')
        for event in sfx_events[:1]:
            lines.append(f"The sound of {clean_english_director_text(event)} cuts clearly through the ambience.")
        sfx_events = sfx_events[1:]

    dialogue_lines = english_plan_dialogue_lines(chunk, parsed_by_id, active_roles, refs, max_lines=4)
    if dialogue_lines:
        lines.extend(dialogue_lines)

    for event in sfx_events[:4]:
        event_text = clean_english_director_text(event)
        if event_text:
            lines.append(f"The sound of {event_text} stays present in the mix, below the voices.")

    if index == total:
        lines.append("The shared score swells on the final turn, then resolves naturally after the last voice.")
    else:
        lines.append("The same ambience and shared score carry forward into the next section without a cadence.")
    prompt = "\n".join(line for line in lines if line is not None).strip()
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt

    # Last-resort compaction for long source windows: keep the required
    # language/voice/music/ambience contract and key dialogue, trim excess
    # descriptive sound events before validation.
    compact_lines = [line for line in lines if line and not line.startswith("The sound of ")]
    prompt = "\n".join(compact_lines).strip()
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    compact_dialogue = english_plan_dialogue_lines(chunk, parsed_by_id, active_roles, refs, max_lines=3)
    compact_lines = compact_lines[:6] + compact_dialogue + compact_lines[-1:]
    return "\n".join(compact_lines).strip()


def prompt_needs_engineering_repair(prompt: str, chunk: dict, active_roles: list[str]) -> bool:
    metrics = english_best_practice_prompt_metrics(prompt)
    if len(prompt) > MAX_PROMPT_CHARS:
        return True
    if has_audio_silence_risk(prompt):
        return True
    if metrics.get("explicit_narrator_lines", 0) > 4:
        return True
    if metrics.get("unbound_quoted_dialogue_line_count", 0) > 0:
        return True
    if offstage_or_unreferenced_dialogue_roles(prompt, active_roles):
        return True
    if forbidden_summary_directive_count(prompt) > 0:
        return True
    if metrics.get("soundscape_event_count", 0) < 8:
        return True
    return False


def compose_english_director_prompt(chunk: dict, parsed_by_id: dict, index: int, total: int) -> tuple[str, list[str]]:
    source_unit_ids = chunk.get("source_unit_ids", [])
    active_roles = active_roles_for_chunk(chunk, parsed_by_id)
    refs = role_ref_map(active_roles)
    planned_prompt = seed_final_audio_prompt(chunk)
    if is_auto_planning() and planned_prompt:
        prompt = normalize_final_audio_prompt(planned_prompt, active_roles, refs, chunk, index, total)
        prompt = ensure_key_dialogue_in_prompt(prompt, chunk, parsed_by_id, active_roles, refs)
        prompt = ensure_narration_bridges_in_prompt(prompt, chunk, refs)
        prompt = ensure_english_nonspoken_rule(prompt)
        if prompt_needs_engineering_repair(prompt, chunk, active_roles):
            prompt = build_official_english_prompt_from_plan(chunk, parsed_by_id, active_roles, refs, index, total)
        return prompt, active_roles
    if is_auto_planning():
        return build_official_english_prompt_from_plan(chunk, parsed_by_id, active_roles, refs, index, total), active_roles
    first_seen: set[str] = set()
    ambience = clean_english_director_text(chunk.get("persistent_ambience", "cold night air, stone reverb, faint magical static"))
    sound_design = clean_english_director_text(chunk.get("sound_design", "stone ambience, movement, object and action sounds"))
    pace = clean_english_director_text(chunk.get("pace_note", "natural adaptive pacing"))
    lines = [
        "Create a complete cinematic audio drama mix, not dry narration: spoken voices, continuous background ambience, foreground action effects, and clearly audible background music are generated together.",
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
        lines.append("The shared score resolves after one brief final cliffhanger beat while the stone ambience fades naturally.")
    else:
        lines.append("The same ambience and shared score carry forward without a cadence.")
    return "\n".join(lines), active_roles


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
        f"[Music: {chunk.get('music_bed', music_default)}]",
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
    normalized_parsed = []
    for unit in parsed:
        source = source_by_id[unit["source_unit_id"]]
        merged = {**source, **unit}
        inferred_speaker = infer_speaker_from_source(source)
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
    data["parsed_source_units"] = normalized_parsed

    parsed_by_id = {unit["source_unit_id"]: unit for unit in normalized_parsed}
    seed_plans = default_chunk_plan(source_units)
    seed_has_final_prompts = any(seed_final_audio_prompt(plan) for plan in seed_plans)
    if is_auto_planning() and is_english_prompt() and seed_has_final_prompts:
        plans = seed_plans
        plans = enforce_chunk_role_limit(plans, parsed_by_id)
        plans = enforce_max_source_unit_count(plans, parsed_by_id, max_units=7)
        plans = enforce_min_chunk_shape(plans, parsed_by_id, min_units=3)
    elif is_auto_planning() and is_english_prompt():
        plans = capacity_first_plan(normalized_parsed, parsed_by_id, seed_plans)
        plans = enforce_max_source_unit_count(plans, parsed_by_id, max_units=7)
        plans = enforce_min_chunk_shape(plans, parsed_by_id, min_units=3)
    else:
        plans = seed_plans
        plans = enforce_chunk_role_limit(plans, parsed_by_id)
        plans = enforce_min_chunk_shape(plans, parsed_by_id)
        plans = enforce_max_chunk_count(plans, parsed_by_id, target_max=3 if is_english_prompt() else 6)
    if not seed_has_final_prompts:
        plans = enforce_prompt_length_limit(plans, parsed_by_id)
        plans = enforce_max_chunk_count(plans, parsed_by_id, target_max=3 if is_english_prompt() else 6)
        plans = merge_short_tail_chunk(plans, parsed_by_id)
    chunks = []
    for index, plan in enumerate(plans, start=1):
        prompt, active_roles = compose_director_prompt(plan, parsed_by_id, index, len(plans))
        unit_ids = plan.get("source_unit_ids", [])
        music_actions = english_music_actions(index, len(plans)) if is_english_prompt() else ["enter", "duck", "swell", "fade"]
        narrator_count = sum(1 for unit_id in unit_ids if parsed_by_id[unit_id].get("speaker") == "Narrator")
        dialogue_count = len(unit_ids) - narrator_count
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
        chunks.append(
            {
                "chunk_id": plan.get("chunk_id", f"chunk_{index:03d}"),
                "source_unit_ids": unit_ids,
                "active_roles": active_roles,
                "text_prompt": prompt,
                "expected_duration_sec": plan.get("expected_duration_sec", {"min": 25, "max": 90}),
                "coverage_summary": plan.get("coverage_summary", ""),
                "source_beats": plan.get("source_beats", []),
                "must_keep_dialogue": plan.get("must_keep_dialogue", []),
                "optional_dialogue": plan.get("optional_dialogue", []),
                "narration_bridge": plan.get("narration_bridge", []),
                "sfx_story_events": plan.get("sfx_story_events", []),
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
                    "speech_rate": plan.get("speech_rate", TARGET_SPEECH_RATE),
                    "uses_seed_final_audio_prompt": bool(is_auto_planning() and is_english_prompt() and seed_final_audio_prompt(plan)),
                    **performance_metrics,
                    **best_practice_metrics,
                    **coverage_metrics,
                },
                "continuity": plan.get("continuity", {}),
                "speech_rate": plan.get("speech_rate", TARGET_SPEECH_RATE),
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


def call_seed2_rewrite(run_dir: Path, source_units: list[dict]) -> dict:
    use_longform_section_plan = bool(is_auto_planning() and is_english_prompt() and STORY_CONFIG and STORY_CONFIG.get("lock_roles"))
    prompt = longform_section_planning_prompt(source_units) if use_longform_section_plan else compact_rewrite_prompt(source_units)
    write_text(run_dir / "logs" / "seed2_rewrite_prompt.txt", prompt)
    last_error = ""
    retryable_validation_patterns = [
        "quoted dialogue for roles outside active_roles",
        "has more than 3 active roles",
        "multiple active roles using the same speaker id",
        "has quoted dialogue lines without actor markers",
        "too many explicit narrator lines",
        "missing must-keep dialogue",
        "must keep real quoted character dialogue",
    ]
    for attempt in range(1, 4):
        prompt_for_attempt = prompt
        if last_error:
            prompt_for_attempt = (
                f"{prompt}\n\nPrevious attempt failed validation: {last_error}\n"
                "Fix the plan, do not explain. If a chunk needs quoted dialogue from more than three roles, split it into smaller chunks. "
                "Every quoted dialogue role must be listed in that chunk's active_roles and bound with actor is <<TGT_SPKn>>. "
                "Never assign one role's dialogue to another role's speaker marker. "
                "If there are too many Narrator lines, compress narration into 1-4 short spoken bridges and move visible action into ambience, music, and foreground sound events. "
                "Return valid JSON only. Escape every double quote inside final_audio_prompt as \\\"."
            )
        try:
            text = llm_chat.chat_text(
                prompt_for_attempt,
                system="You are a precise JSON-only audiobook workflow generator. Return one complete valid JSON object. Do not truncate.",
                model=REWRITE_MODEL,
                temperature=0.1,
                # A section-level plan has a bounded schema. Letting the model
                # reserve 30k tokens makes ordinary chapter sections stall.
                max_tokens=REWRITE_MAX_TOKENS if is_auto_planning() else 12000,
            )
            write_text(run_dir / "logs" / f"seed2_rewrite_response_attempt_{attempt}.txt", text)
            write_text(run_dir / "logs" / "seed2_rewrite_response.txt", text)
            data = normalize_rewrite(extract_json(text), source_units)
            validate_rewrite(data, source_units)
            return data
        except SystemExit as exc:
            message = str(exc)
            retryable = any(pattern in message for pattern in retryable_validation_patterns)
            last_error = message[:1000]
            if attempt < 3 and retryable:
                time.sleep(20 * attempt)
                continue
            raise
        except Exception as exc:
            retryable = "HTTP Error 429" in str(exc) or "Too Many Requests" in str(exc)
            retryable = retryable or "IncompleteRead" in str(exc) or "unexpected EOF" in str(exc) or "Remote end closed" in str(exc)
            retryable = retryable or any(pattern in str(exc) for pattern in retryable_validation_patterns)
            last_error = str(exc)[:1000]
            if attempt < 3 and (retryable or isinstance(exc, json.JSONDecodeError)):
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
            if "All audible speech must be English only. Do not translate or speak Chinese." not in prompt:
                raise SystemExit(f"{chunk.get('chunk_id')} missing English speech language lock.")
            semantic_markers = {
                "ambience": r"Ambient sound:|ambience|ambient|wind|stone echo",
                "music": r"Background music:|music|score|cello|strings|drone",
                "sound_design": r"Sound design:|foreground|whoosh|clang|impact|crack|shatter|spark|wand",
                "voice_pace": r"adaptive pace|one voice at a time",
            }
            for marker, pattern in semantic_markers.items():
                if not re.search(pattern, prompt, re.I):
                    raise SystemExit(f"{chunk.get('chunk_id')} missing prompt marker: {marker}")
        else:
            required_prompt_markers = ["Ambience", "Music", "SFX", "Pace"]
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
                if "Voice continuity:" not in prompt:
                    raise SystemExit(f"{chunk.get('chunk_id')} missing explicit Voice continuity mapping.")
                if metrics.get("unbound_quoted_dialogue_line_count", 0) > 0:
                    raise SystemExit(f"{chunk.get('chunk_id')} has quoted dialogue lines without actor markers.")
                unreferenced_roles = offstage_or_unreferenced_dialogue_roles(prompt, chunk.get("active_roles", []))
                if unreferenced_roles:
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} has quoted dialogue for roles outside active_roles: {unreferenced_roles}. "
                        "Split the chunk or include those roles in active_roles."
                    )
                if metrics.get("explicit_narrator_lines", 0) > 4:
                    raise SystemExit(f"{chunk.get('chunk_id')} has too many explicit narrator lines for sound-first official style.")
                if metrics.get("dialogue_unit_count", 0) >= 2 and metrics.get("quoted_dialogue_line_count", 0) < 2:
                    raise SystemExit(f"{chunk.get('chunk_id')} must keep real quoted character dialogue, not summary narration.")
                min_quote_ratio = 0.02 if metrics.get("dialogue_unit_count", 0) <= 3 else 0.04
                if metrics.get("dialogue_unit_count", 0) >= 2 and metrics.get("spoken_quote_word_ratio", 0) < min_quote_ratio:
                    raise SystemExit(
                        f"{chunk.get('chunk_id')} has too little real spoken dialogue: "
                        f"spoken_quote_word_ratio={metrics.get('spoken_quote_word_ratio')}, min={min_quote_ratio}"
                    )
                if metrics.get("spoken_quote_word_ratio", 0) > 0.48:
                    raise SystemExit(f"{chunk.get('chunk_id')} is too speech-heavy: spoken_quote_word_ratio={metrics.get('spoken_quote_word_ratio')}")
                if metrics.get("forbidden_summary_directive_count", 0) > 0:
                    raise SystemExit(f"{chunk.get('chunk_id')} contains summary-style dialogue directives such as narrate/deliver-line.")
                if metrics.get("soundscape_event_count", 0) < 8:
                    raise SystemExit(f"{chunk.get('chunk_id')} has too few soundscape events for official mixed-scene style.")
                if metrics.get("music_keyword_count", 0) < 2:
                    raise SystemExit(f"{chunk.get('chunk_id')} has weak music design.")
                if metrics.get("ambience_keyword_count", 0) < 2:
                    raise SystemExit(f"{chunk.get('chunk_id')} has weak ambience design.")
                if metrics.get("sfx_keyword_count", 0) < 1 and metrics.get("soundscape_event_count", 0) < 8:
                    raise SystemExit(f"{chunk.get('chunk_id')} has weak foreground SFX design.")
                if metrics.get("source_beat_count", 0) < 2:
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
            if not metrics.get("uses_chronological_performance_style"):
                raise SystemExit(f"{chunk.get('chunk_id')} is not using official chronological performance prompt style.")
            if not metrics.get("uses_official_reference_marker_style"):
                raise SystemExit(f"{chunk.get('chunk_id')} missing official actor reference marker style.")
        if not is_english_prompt() and "Narrator" not in chunk.get("active_roles", []):
            raise SystemExit(f"{chunk.get('chunk_id')} must include Narrator as continuity anchor.")
        if is_english_prompt() and "no overlapping narration and dialogue" not in prompt:
            raise SystemExit(f"{chunk.get('chunk_id')} missing mixing rule.")
        if is_english_prompt() and "one voice at a time" not in prompt:
            raise SystemExit(f"{chunk.get('chunk_id')} missing voice serialization rule.")
        sfx_limit = metrics.get("sfx_cue_limit")
        if is_english_prompt() and sfx_limit is not None and metrics.get("sfx_cue_count", 0) > sfx_limit:
            raise SystemExit(f"{chunk.get('chunk_id')} has too many rendered SFX cues.")
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
    prompt_lengths = []
    for chunk in rewrite["director_prompt_chunks"]:
        chunk_id = chunk["chunk_id"]
        prompt = chunk["text_prompt"]
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
            "omission_rationale": chunk.get("omission_rationale", []),
            "references": [role_reference_placeholder(role) for role in chunk.get("active_roles", [])],
            "expected_duration_sec": chunk.get("expected_duration_sec", {"min": 20, "max": 100}),
            "prompt_constraints": {
                "max_prompt_chars": MAX_PROMPT_CHARS,
                "must_keep_voice_binding": True,
                "must_keep_persistent_ambience": True,
                "must_keep_music_bed": True,
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
            str(path.relative_to(ROOT)),
            "-t",
            f"{keep_until:.3f}",
            "-ar",
            "24000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(hard_tmp.relative_to(ROOT)),
        ]
    )
    if hard_result.returncode == 0 and audio_decodes(hard_tmp):
        hard_tmp.replace(path)
    elif hard_tmp.exists():
        hard_tmp.unlink()


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
    if not POSTPROCESS_TIMING:
        # Let the generation strategy control pacing. Removing quiet audio here
        # can mistake a breath or weak final consonant for silence.
        return {
            "duration_before_timing_normalization_sec": before,
            "duration_after_timing_normalization_sec": before,
            "internal_silence_compressed": False,
            "postprocess_timing_applied": False,
        }
    trim_trailing_silence(path)
    compressed = compress_internal_silence(path)
    trim_trailing_silence(path)
    return {
        "duration_before_timing_normalization_sec": before,
        "duration_after_timing_normalization_sec": audio_duration(path),
        "internal_silence_compressed": compressed,
        "postprocess_timing_applied": True,
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
        trim_trailing_silence(clean_path)
    return outputs


def generate_scene_audio(run_dir: Path, force_chunk_ids: set[str] | None = None) -> dict:
    force_chunk_ids = force_chunk_ids or set()
    logs = []
    parts = []
    for prompt_path in sorted((run_dir / "05_director_prompt_chunks").glob("chunk_*.txt")):
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
        cmd = [
            sys.executable,
            str(SEED_AUDIO_CLIENT),
            "--text-file",
            str(prompt_path.relative_to(ROOT)),
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
    if POSTPROCESS_TIMING:
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


def asr_language_audit(run_dir: Path, scene_logs: list[dict]) -> dict:
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
            "source": "Seed Audio temporary URL captured from the same generation response.",
        },
        "chunks": reports,
    }


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
        long_internal_silences = [
            item for item in intervals
            if item["duration_sec"] > 2.0 and (item["end_sec"] or 0) < (duration or 0) - 0.5
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
        if report["long_internal_silence_count"]:
            report["needs_regeneration"] = True
            report["reasons"].append("long_internal_silence")
        if report["tail_silence_sec"] > 1.5:
            report["needs_regeneration"] = True
            report["reasons"].append("tail_silence_over_limit")
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
            report["reasons"].append("missing_music_continuity_prompt")
        if not report["has_voice_serialization_rule"]:
            report["needs_regeneration"] = True
            report["reasons"].append("missing_voice_serialization_rule")
        if not report["has_shared_score_bed"]:
            report["needs_regeneration"] = True
            report["reasons"].append("missing_shared_score_bed")
        if not report["has_audible_music_instruction"]:
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
            min_quote_ratio = 0.02 if metrics.get("dialogue_unit_count", 0) <= 3 else 0.04
            if metrics.get("dialogue_unit_count", 0) >= 2 and (report["spoken_quote_word_ratio"] or 0) < min_quote_ratio:
                report["needs_regeneration"] = True
                report["reasons"].append("dialogue_too_summary_like")
            if (report["forbidden_summary_directive_count"] or 0) > 0:
                report["needs_regeneration"] = True
                report["reasons"].append("summary_style_dialogue_directives")
            if (report["soundscape_event_count"] or 0) < 8:
                report["needs_regeneration"] = True
                report["reasons"].append("weak_soundscape_prompt")
            if (report["music_keyword_count"] or 0) < 2:
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
        if report["needs_regeneration"]:
            fail_reasons.append(f"{part.stem}: {', '.join(report['reasons'])}")
        chunk_reports.append(report)
    final_duration = audio_duration(full)
    final_silences = silence_intervals(full)
    final_tail = [
        item for item in final_silences
        if final_duration is not None and (item["end_sec"] or final_duration) >= final_duration - 0.25
    ]
    return {
        "status": "fail" if fail_reasons else "pass",
        "fail_reasons": fail_reasons,
        "policy": {
            "silence_threshold_db": "-45dB",
            "silence_detect_min_duration_sec": 1.0,
            "max_internal_silence_sec": 2.0,
            "max_tail_silence_sec": 1.5,
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


PERFORMANCE_ISSUES = {
    "rushed_delivery",
    "clipped_ending",
    "hard_boundary",
    "speech_masked_by_mix",
    "mechanical_narration",
    "unintelligible_speech",
}


def performance_review_prompt(request: dict) -> str:
    return f"""You are a strict audio-drama performance reviewer. Listen to the attached generated audio, not just its script.

Evaluate only these release-blocking defects: rushed delivery, clipped ending, hard boundary/edit, speech masked by music or SFX, mechanical narration, unintelligible speech.

Scene metadata:
{json.dumps({{"chunk_id": request.get("chunk_id"), "source_beats": request.get("source_beats", []), "pace_note": request.get("continuity", {}), "speech_rate": request.get("audio_config", {}).get("speech_rate")}}, ensure_ascii=False)}

Return one JSON object only:
{{"status":"pass|fail","issues":[{{"type":"one allowed defect name","severity":"minor|major","evidence":"short concrete listening evidence"}}],"repair_focus":["short regeneration instruction"],"summary":"short verdict"}}

A minor stylistic preference alone is pass. Fail if any major defect makes the audio feel cut off, rushed, masked, mechanical, or hard-edited."""


def performance_review_report(run_dir: Path, parts: list[Path]) -> dict:
    requests = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "06_generation_requests").glob("chunk_*.json"))
    }
    reports = []
    failures = []
    for part in parts:
        request = requests.get(part.stem, {})
        report = {
            "chunk_id": part.stem,
            "model": PERFORMANCE_REVIEW_MODEL,
            "status": "fail",
            "issues": [],
            "repair_focus": [],
            "summary": "review not completed",
        }
        try:
            raw = llm_chat.chat_text(
                performance_review_prompt(request),
                model=PERFORMANCE_REVIEW_MODEL,
                temperature=0,
                max_tokens=900,
                audios=[str(part)],
                timeout=PERFORMANCE_REVIEW_TIMEOUT,
            )
            write_text(run_dir / "logs" / f"performance_{part.stem}_raw.txt", raw)
            parsed = extract_json(raw)
            issues = [
                item for item in parsed.get("issues", [])
                if isinstance(item, dict) and item.get("type") in PERFORMANCE_ISSUES
            ]
            status = str(parsed.get("status", "fail")).lower()
            major = [item for item in issues if str(item.get("severity", "major")).lower() == "major"]
            report.update(
                {
                    "status": "pass" if status == "pass" and not major else "fail",
                    "issues": issues,
                    "repair_focus": [str(item) for item in parsed.get("repair_focus", []) if str(item).strip()][:4],
                    "summary": str(parsed.get("summary", "")),
                }
            )
        except Exception as exc:
            report["status"] = "unavailable"
            report["summary"] = f"performance reviewer unavailable: {exc}"
            report["issues"] = [{"type": "review_unavailable", "severity": "major", "evidence": "review unavailable"}]
        if report["status"] != "pass":
            failures.append(f"{part.stem}: {report['summary']}")
        reports.append(report)
    return {
        "status": "fail" if failures else "pass",
        "model": PERFORMANCE_REVIEW_MODEL,
        "fail_reasons": failures,
        "policy": "Audio-capable review is required after engineering and ASR checks. Major pacing, tail, masking, narration, or boundary defects block stitching.",
        "chunks": reports,
    }


def failed_performance_chunk_ids(performance: dict | None) -> set[str]:
    if not performance:
        return set()
    return {
        str(chunk["chunk_id"])
        for chunk in performance.get("chunks", [])
        if chunk.get("status") == "fail" and chunk.get("chunk_id")
    }


def apply_performance_repairs(run_dir: Path, performance: dict) -> dict:
    """Make a compact, generation-side repair contract for failed chunks."""
    repairs = []
    by_id = {str(item.get("chunk_id")): item for item in performance.get("chunks", [])}
    for chunk_id in failed_performance_chunk_ids(performance):
        request_path = run_dir / "06_generation_requests" / f"{chunk_id}.json"
        prompt_path = run_dir / "05_director_prompt_chunks" / f"{chunk_id}.txt"
        if not request_path.exists() or not prompt_path.exists():
            continue
        request = json.loads(request_path.read_text(encoding="utf-8"))
        review = by_id.get(chunk_id, {})
        issue_types = {str(item.get("type")) for item in review.get("issues", [])}
        focus = " ".join(review.get("repair_focus", []))[:220]
        overlay = (
            "Performance repair: use measured natural pacing; urgency comes from acting and sound, not fast reading. "
            "Articulate every final consonant and complete each line before the next sound. "
            "Keep music and effects below speech. End after a completed thought with a brief natural ambience tail."
        )
        if "mechanical_narration" in issue_types:
            overlay += " Narrator varies emphasis and breath by meaning, never metronomic."
        if focus:
            overlay += f" Reviewer focus: {focus}."
        original = prompt_path.read_text(encoding="utf-8").strip()
        repaired_prompt = f"{original}\n{overlay}"
        if len(repaired_prompt) > MAX_PROMPT_CHARS:
            # Keep the explicit repair priority without exceeding the provider's hard prompt limit.
            repaired_prompt = f"{overlay}\n{original[: MAX_PROMPT_CHARS - len(overlay) - 1].rstrip()}"
        prompt_path.write_text(repaired_prompt + "\n", encoding="utf-8")
        request["text_prompt"] = repaired_prompt
        current_rate = int(request.get("audio_config", {}).get("speech_rate", ENGLISH_TARGET_SPEECH_RATE))
        request.setdefault("audio_config", {})["speech_rate"] = max(-4, min(current_rate, 0))
        request["performance_repair"] = {
            "review_model": PERFORMANCE_REVIEW_MODEL,
            "issues": review.get("issues", []),
            "repair_focus": review.get("repair_focus", []),
            "applied": True,
        }
        write_json(request_path, request)
        repairs.append({"chunk_id": chunk_id, "speech_rate": request["audio_config"]["speech_rate"], "issues": sorted(issue_types)})
    return {"repairs": repairs}


def failed_quality_chunk_ids(quality: dict) -> set[str]:
    non_retriable = {
        "duration_much_shorter_than_playable_text_estimate",
        "audio_drama_duration_suspiciously_short",
        "long_internal_silence",
        "tail_silence_over_limit",
    }
    return {
        chunk.get("chunk_id")
        for chunk in quality.get("chunks", [])
        if chunk.get("needs_regeneration") and chunk.get("chunk_id")
        and not set(chunk.get("reasons", [])) <= non_retriable
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
        performance_text = f"- Model: `{performance.get('model')}`\n- Status: `{performance['status']}`\n- Fail reasons: {performance.get('fail_reasons', [])}"
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
- `seed-2-0-lite-260428` listens to each generated chunk for rushed delivery, clipped endings, hard boundaries, masking, and mechanical narration. Failed chunks are regenerated with a compact performance repair contract.
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
    parser.add_argument("--output-root", help="Override output root. Defaults to outputs/runs.")
    parser.add_argument(
        "--story-config",
        help="Story config JSON. Defaults to story_configs/moonlit_cloister_duel_en.json when present.",
    )
    parser.add_argument("--source-file", help="Plain novel/story text file. Enables full automatic planning from source text.")
    parser.add_argument("--language", default="en", help="Source language for --source-file: en or zh. Default: en.")
    parser.add_argument("--mode", default="audio_drama_adaptation", help="Production mode for --source-file.")
    parser.add_argument("--source-title", help="Optional source title for --source-file.")
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

    if args.resume_run_id:
        run_dir = OUTPUT_ROOT / args.resume_run_id
        if not run_dir.exists():
            raise SystemExit(f"Resume folder does not exist: {run_dir.relative_to(ROOT)}")
        apply_voice_registry_file(run_dir / "04_voice_registry.json")
        prompt_lengths = [len(path.read_text(encoding="utf-8")) for path in sorted((run_dir / "05_director_prompt_chunks").glob("chunk_*.txt"))]
        build = {"chunks": len(prompt_lengths), "prompt_lengths": prompt_lengths}
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
    performance = None
    audio = None
    if args.generate:
        references = generate_reference_audio(run_dir)
        scene = generate_scene_audio(run_dir)
        full = concat_audio(run_dir, scene["parts"])
        reference_paths = [Path(item["path"]) for item in references.values() if item.get("path")]
        validation = validate_audio(reference_paths + scene["parts"] + [full])
        quality = audio_quality_report(run_dir, scene["parts"], full)
        asr_report = asr_language_audit(run_dir, scene["logs"])
        performance = performance_review_report(run_dir, scene["parts"])
        repair_logs = []
        for _repair_attempt in range(1, 3):
            failed_chunks = (
                failed_quality_chunk_ids(quality)
                | failed_asr_chunk_ids(asr_report)
                | failed_performance_chunk_ids(performance)
            )
            if not failed_chunks:
                break
            performance_repair = apply_performance_repairs(run_dir, performance)
            write_json(run_dir / "logs" / f"performance_repair_{_repair_attempt}.json", performance_repair)
            repaired_scene = generate_scene_audio(run_dir, failed_chunks)
            full = concat_audio(run_dir, repaired_scene["parts"])
            validation = validate_audio(reference_paths + repaired_scene["parts"] + [full])
            quality = audio_quality_report(run_dir, repaired_scene["parts"], full)
            asr_report = asr_language_audit(run_dir, repaired_scene["logs"])
            performance = performance_review_report(run_dir, repaired_scene["parts"])
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
        write_json(run_dir / "logs" / "performance_review_report.json", performance)
        audio = {
            "full": str(full.relative_to(ROOT)),
            "validation": validation,
            "quality": quality,
            "asr": asr_report,
            "performance": performance,
        }
        update_manifest(run_dir, "completed")
    write_analysis(run_dir, build, validation, quality, performance)
    print(json.dumps({"run_dir": str(run_dir.relative_to(ROOT)), "build": build, "audio": audio}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
