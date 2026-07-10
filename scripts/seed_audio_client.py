#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import common


common.load_tool_env()

SEED_AUDIO_URL = os.getenv(
    "SEED_AUDIO_URL",
    "https://voice.ap-southeast-1.bytepluses.com/api/v3/tts/create",
)
SEED_AUDIO_MODEL = os.getenv("SEED_AUDIO_MODEL", "seed-audio-1.0")
SEED_AUDIO_FORMAT = os.getenv("SEED_AUDIO_FORMAT", "wav")
SEED_AUDIO_SAMPLE_RATE = int(os.getenv("SEED_AUDIO_SAMPLE_RATE", "24000"))
SEED_AUDIO_SPEECH_RATE = int(os.getenv("SEED_AUDIO_SPEECH_RATE", "0"))
SEED_AUDIO_LOUDNESS_RATE = int(os.getenv("SEED_AUDIO_LOUDNESS_RATE", "0"))
SEED_AUDIO_PITCH_RATE = int(os.getenv("SEED_AUDIO_PITCH_RATE", "0"))
SEED_AUDIO_TIMEOUT = int(os.getenv("SEED_AUDIO_TIMEOUT", "120"))
SEED_AUDIO_MAX_CHARS = int(os.getenv("SEED_AUDIO_MAX_CHARS", "3000"))

SUPPORTED_AUDIO_FORMATS = {"wav", "mp3", "pcm", "ogg_opus"}
SUPPORTED_SAMPLE_RATES = {8000, 16000, 24000, 32000, 44100, 48000}


def _resolve_api_key(explicit: str | None) -> str | None:
    return (
        explicit
        or os.getenv("SEED_AUDIO_API_KEY")
        or os.getenv("TTS_API_KEY")
        or os.getenv("BYTEPLUS_API_KEY")
        or ""
    ).strip() or None


def _resolve_legacy_auth(app_id: str | None, access_key: str | None) -> tuple[str | None, str | None]:
    resolved_app_id = (app_id or os.getenv("SEED_AUDIO_APP_ID") or "").strip() or None
    resolved_access_key = (access_key or os.getenv("SEED_AUDIO_ACCESS_KEY") or "").strip() or None
    return resolved_app_id, resolved_access_key


def _headers(
    *,
    credential: str | None,
    app_id: str | None,
    legacy_credential: str | None,
    request_id: str | None,
) -> dict[str, str]:
    if credential and (app_id or legacy_credential):
        raise RuntimeError("Use either SEED_AUDIO_API_KEY or legacy SEED_AUDIO_APP_ID/SEED_AUDIO_ACCESS_KEY, not both.")
    if credential:
        headers = {"X-Api-Key": credential}
    else:
        if not app_id or not legacy_credential:
            raise RuntimeError(
                "Missing Seed Audio credentials. Set SEED_AUDIO_API_KEY, or set both "
                "SEED_AUDIO_APP_ID and SEED_AUDIO_ACCESS_KEY."
            )
        headers = {
            "X-Api-App-Id": app_id,
            "X-Api-Access-Key": legacy_credential,
        }
    headers.update(
        {
            "Content-Type": "application/json",
            "X-Api-Request-Id": request_id or str(uuid.uuid4()),
        }
    )
    return headers


def _base64_file(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _reference_from_value(kind: str, value: str) -> dict[str, str]:
    if value.startswith(("http://", "https://")):
        return {f"{kind}_url": value}
    return {f"{kind}_data": _base64_file(value)}


def _build_references(
    *,
    speakers: list[str],
    audio_refs: list[str],
    image_refs: list[str],
) -> list[dict[str, str]]:
    audio_items: list[dict[str, str]] = []
    audio_items.extend({"speaker": speaker} for speaker in speakers)
    audio_items.extend(_reference_from_value("audio", value) for value in audio_refs)

    image_items = [_reference_from_value("image", value) for value in image_refs]
    if audio_items and image_items:
        raise RuntimeError("Seed Audio references cannot mix audio references with image references.")
    if len(audio_items) > 3:
        raise RuntimeError("Seed Audio supports at most 3 audio references per request.")
    if len(image_items) > 1:
        raise RuntimeError("Seed Audio supports at most 1 image reference per request.")
    return audio_items or image_items


def _audio_config(
    *,
    audio_format: str,
    sample_rate: int,
    speech_rate: int,
    loudness_rate: int,
    pitch_rate: int,
) -> dict[str, int | str]:
    fmt = audio_format.strip()
    if fmt not in SUPPORTED_AUDIO_FORMATS:
        raise RuntimeError(f"Unsupported audio format: {fmt}")
    if sample_rate not in SUPPORTED_SAMPLE_RATES:
        raise RuntimeError(f"Unsupported sample rate: {sample_rate}")
    for name, value in {
        "speech_rate": speech_rate,
        "loudness_rate": loudness_rate,
        "pitch_rate": pitch_rate,
    }.items():
        if value < -50 or value > 100:
            raise RuntimeError(f"{name} must be between -50 and 100.")
    return {
        "format": fmt,
        "sample_rate": sample_rate,
        "speech_rate": speech_rate,
        "loudness_rate": loudness_rate,
        "pitch_rate": pitch_rate,
    }


def build_payload(
    *,
    text_prompt: str,
    model: str,
    references: list[dict[str, str]],
    audio_config: dict[str, int | str],
    include_watermark: bool,
) -> dict:
    if not text_prompt.strip():
        raise RuntimeError("Missing text prompt.")
    if len(text_prompt) > SEED_AUDIO_MAX_CHARS:
        raise RuntimeError(
            f"Seed Audio text_prompt must be at most {SEED_AUDIO_MAX_CHARS} characters. "
            "Use --split-long for long audiobook text."
        )
    payload: dict = {
        "model": model,
        "text_prompt": text_prompt,
        "audio_config": audio_config,
    }
    if references:
        payload["references"] = references
    if include_watermark:
        payload["watermark"] = {}
    return payload


def create_audio(
    payload: dict,
    *,
    url: str,
    api_key: str | None = None,
    app_id: str | None = None,
    access_key: str | None = None,
    request_id: str | None = None,
    timeout: int = SEED_AUDIO_TIMEOUT,
) -> dict:
    resolved_credential = _resolve_api_key(api_key)
    resolved_app_id, resolved_legacy_credential = _resolve_legacy_auth(app_id, access_key)
    headers = _headers(
        credential=resolved_credential,
        app_id=resolved_app_id,
        legacy_credential=resolved_legacy_credential,
        request_id=request_id,
    )
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _find_first_string(obj: object, keys: set[str]) -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, str) and value:
                return value
        for value in obj.values():
            found = _find_first_string(value, keys)
            if found:
                return found
    if isinstance(obj, list):
        for value in obj:
            found = _find_first_string(value, keys)
            if found:
                return found
    return None


def extract_audio_base64(result: dict) -> str | None:
    return _find_first_string(result, {"audio", "audio_data", "audio_base64", "data"})


def extract_audio_url(result: dict) -> str | None:
    return _find_first_string(result, {"url", "audio_url"})


def write_audio(result: dict, output_path: str | Path) -> bool:
    audio_base64 = extract_audio_base64(result)
    if not audio_base64:
        return False
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(audio_base64))
    return True


def write_response_meta(result: dict, output_path: str | Path) -> None:
    out = Path(output_path)
    meta_path = out.with_suffix(out.suffix + ".meta.json")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    audio_url = extract_audio_url(result)
    audio_base64 = extract_audio_base64(result)
    meta = {
        "audio_url": audio_url or "",
        "has_audio_base64": bool(audio_base64),
        "response_keys": sorted(result.keys()),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def split_text_prompt(text: str, max_chars: int = SEED_AUDIO_MAX_CHARS) -> list[str]:
    paragraphs = [part.strip() for part in text.replace("\r\n", "\n").split("\n\n") if part.strip()]
    chunks: list[str] = []
    current = ""

    def push_current() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            push_current()
            sentences = paragraph.replace(". ", ".\n").replace("! ", "!\n").replace("? ", "?\n").splitlines()
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                if len(sentence) > max_chars:
                    raise RuntimeError(f"Cannot split a single text span under {max_chars} characters.")
                if current and len(current) + 1 + len(sentence) > max_chars:
                    push_current()
                current = f"{current} {sentence}".strip()
            continue

        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) > max_chars:
            push_current()
            current = paragraph
        else:
            current = candidate

    push_current()
    return chunks


def chunk_output_path(output_path: str | Path, index: int, total: int) -> Path:
    out = Path(output_path)
    width = max(3, len(str(total)))
    return out.with_name(f"{out.stem}_{index:0{width}d}{out.suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Call BytePlus Seed Audio 1.0 HTTP API.")
    parser.add_argument("--text", "-t", help="Text prompt to synthesize")
    parser.add_argument("--text-file", help="Read text prompt from a UTF-8 file")
    parser.add_argument("--out", "-o", default="outputs/seed_audio.wav", help="Output audio path")
    parser.add_argument("--url", default=SEED_AUDIO_URL, help="Seed Audio endpoint URL")
    parser.add_argument("--model", default=SEED_AUDIO_MODEL, help="Model id")
    parser.add_argument("--api-key", help="New console API key override")
    parser.add_argument("--app-id", help="Legacy console app id override")
    parser.add_argument("--access-key", help="Legacy console access key override")
    parser.add_argument("--request-id", help="Optional trace id")
    parser.add_argument("--speaker", action="append", default=[], help="Audio reference speaker id; repeat up to 3")
    parser.add_argument("--audio", action="append", default=[], help="Audio reference URL or local file; repeat up to 3")
    parser.add_argument("--image", action="append", default=[], help="Image reference URL or local file; only one")
    parser.add_argument("--format", default=SEED_AUDIO_FORMAT, choices=sorted(SUPPORTED_AUDIO_FORMATS))
    parser.add_argument("--sample-rate", type=int, default=SEED_AUDIO_SAMPLE_RATE)
    parser.add_argument("--speech-rate", type=int, default=SEED_AUDIO_SPEECH_RATE)
    parser.add_argument("--loudness-rate", type=int, default=SEED_AUDIO_LOUDNESS_RATE)
    parser.add_argument("--pitch-rate", type=int, default=SEED_AUDIO_PITCH_RATE)
    parser.add_argument("--timeout", type=int, default=SEED_AUDIO_TIMEOUT)
    parser.add_argument("--split-long", action="store_true", help="Split text over the API character limit into part files")
    parser.add_argument("--max-chars", type=int, default=SEED_AUDIO_MAX_CHARS, help="Max characters per split prompt")
    parser.add_argument("--no-watermark", action="store_true", help="Omit the watermark object from the payload")
    parser.add_argument("--json", action="store_true", help="Print raw JSON response")
    parser.add_argument("--url-only", action="store_true", help="Print temporary audio URL if returned")
    args = parser.parse_args()

    text = args.text
    if not text and args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    if not text:
        print("Missing --text or --text-file", file=sys.stderr)
        return 2

    try:
        references = _build_references(speakers=args.speaker, audio_refs=args.audio, image_refs=args.image)
        audio_config = _audio_config(
            audio_format=args.format,
            sample_rate=args.sample_rate,
            speech_rate=args.speech_rate,
            loudness_rate=args.loudness_rate,
            pitch_rate=args.pitch_rate,
        )

        text_parts = [text]
        if len(text) > args.max_chars:
            if not args.split_long:
                raise RuntimeError(
                    f"Seed Audio text_prompt must be at most {args.max_chars} characters. "
                    "Use --split-long for this input."
                )
            text_parts = split_text_prompt(text, args.max_chars)

        outputs: list[dict[str, str | int]] = []
        for idx, text_part in enumerate(text_parts, start=1):
            payload = build_payload(
                text_prompt=text_part,
                model=args.model,
                references=references,
                audio_config=audio_config,
                include_watermark=not args.no_watermark,
            )
            result = create_audio(
                payload,
                url=args.url,
                api_key=args.api_key,
                app_id=args.app_id,
                access_key=args.access_key,
                request_id=args.request_id,
                timeout=args.timeout,
            )
            part_out = chunk_output_path(args.out, idx, len(text_parts)) if len(text_parts) > 1 else Path(args.out)
            write_response_meta(result, part_out)
            if args.json:
                outputs.append({"part": idx, "chars": len(text_part), "response": json.dumps(result, ensure_ascii=False)})
                continue
            if args.url_only:
                outputs.append({"part": idx, "chars": len(text_part), "url": extract_audio_url(result) or ""})
                continue
            if write_audio(result, part_out):
                outputs.append({"part": idx, "chars": len(text_part), "path": str(part_out)})
                continue
            url = extract_audio_url(result)
            if url:
                outputs.append({"part": idx, "chars": len(text_part), "url": url})
                continue
            outputs.append({"part": idx, "chars": len(text_part), "response": json.dumps(result, ensure_ascii=False)})
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            pass
        print(f"HTTP {exc.code}: {body or exc.reason}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Seed Audio failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(outputs, ensure_ascii=False, indent=2))
        return 0
    for item in outputs:
        print(item.get("path") or item.get("url") or item.get("response") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
