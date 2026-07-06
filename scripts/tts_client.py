#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from pathlib import Path

import requests

import common


common.load_tool_env()

TTS_URL = os.getenv("TTS_URL", "https://voice.ap-southeast-1.bytepluses.com/api/v3/tts/unidirectional")
TTS_RESOURCE_ID = os.getenv("TTS_RESOURCE_ID", "seed-tts-2.0")
TTS_APP_KEY = os.getenv("TTS_APP_KEY", "aGjiRDfUWi")
TTS_FORMAT = os.getenv("TTS_FORMAT", "mp3")
TTS_SAMPLE_RATE = int(os.getenv("TTS_SAMPLE_RATE", "24000"))
TTS_EXPLICIT_LANGUAGE = os.getenv("TTS_EXPLICIT_LANGUAGE", "es-mx")
TTS_CONTEXT_LANGUAGE = os.getenv("TTS_CONTEXT_LANGUAGE", "es")
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "").strip()


def _resolve_api_key(explicit: str | None = None) -> str:
    api_key = (explicit or os.getenv("TTS_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing TTS_API_KEY in tool/.env.")
    return api_key


def _resolve_speaker(explicit: str | None = None) -> str:
    speaker = (explicit or TTS_SPEAKER or "").strip()
    if not speaker:
        raise RuntimeError("Missing TTS_SPEAKER in tool/.env.")
    return speaker


def _headers(credential: str) -> dict[str, str]:
    return {
        "X-Api-Key": credential,
        "X-Api-Resource-Id": TTS_RESOURCE_ID,
        "X-Api-App-Key": TTS_APP_KEY,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }


def synthesize_to_bytes(
    text: str,
    *,
    api_key: str | None = None,
    speaker: str | None = None,
    audio_format: str | None = None,
    sample_rate: int | None = None,
    explicit_language: str | None = None,
    context_language: str | None = None,
    user_id: str = "video-edit-to-spanish",
) -> bytes:
    if not text.strip():
        raise RuntimeError("Empty TTS text.")

    credential = _resolve_api_key(api_key)
    speaker = _resolve_speaker(speaker)
    fmt = (audio_format or TTS_FORMAT or "mp3").strip()
    sr = int(sample_rate or TTS_SAMPLE_RATE or 24000)
    explicit_lang = (explicit_language or TTS_EXPLICIT_LANGUAGE or "").strip()
    context_lang = (context_language or TTS_CONTEXT_LANGUAGE or "").strip()

    additions: dict[str, object] = {
        "disable_markdown_filter": True,
        "disable_emoji_filter": True,
    }
    if explicit_lang:
        additions["explicit_language"] = explicit_lang
    if context_lang:
        additions["context_language"] = context_lang

    payload = {
        "user": {"id": user_id},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {
                "format": fmt,
                "sample_rate": sr,
            },
            "additions": json.dumps(additions, ensure_ascii=False),
        },
    }

    session = requests.Session()
    try:
        response = session.post(
            TTS_URL,
            headers=_headers(credential),
            json=payload,
            stream=True,
            timeout=300,
        )
        response.raise_for_status()
        audio_chunks: list[bytes] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            try:
                obj = json.loads(line)
            except Exception as exc:
                raise RuntimeError(f"Cannot parse TTS stream chunk: {line[:500]}") from exc
            code = obj.get("code")
            if code == 0:
                # Streaming chunks may contain either:
                # - audio base64 in "data"
                # - metadata only (e.g., "sentence"/"phonemes") with data=null
                data = obj.get("data")
                if isinstance(data, str) and data:
                    audio_chunks.append(base64.b64decode(data))
                # If data is null, just keep reading until the end marker.
                continue
            if code == 20000000:
                break
            raise RuntimeError(f"TTS failed: {json.dumps(obj, ensure_ascii=False)[:1200]}")
        if not audio_chunks:
            raise RuntimeError("TTS returned no audio chunks.")
        return b"".join(audio_chunks)
    finally:
        session.close()


def synthesize_to_file(
    text: str,
    output_path: str | Path,
    *,
    api_key: str | None = None,
    speaker: str | None = None,
    audio_format: str | None = None,
    sample_rate: int | None = None,
    explicit_language: str | None = None,
    context_language: str | None = None,
) -> str:
    data = synthesize_to_bytes(
        text,
        api_key=api_key,
        speaker=speaker,
        audio_format=audio_format,
        sample_rate=sample_rate,
        explicit_language=explicit_language,
        context_language=context_language,
    )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return str(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="BytePlus TTS unidirectional HTTP helper.")
    parser.add_argument("--text", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--speaker", help="Optional TTS speaker override")
    parser.add_argument("--format", default=TTS_FORMAT)
    args = parser.parse_args()
    try:
        out = synthesize_to_file(args.text, args.out, speaker=args.speaker, audio_format=args.format)
        print(out)
        return 0
    except Exception as exc:
        print(f"TTS failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
