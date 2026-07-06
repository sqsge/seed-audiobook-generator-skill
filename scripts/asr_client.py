#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import common


common.load_tool_env()

ASR_SUBMIT_URL = os.getenv(
    "ASR_SUBMIT_URL",
    "https://operator.las.ap-southeast-1.bytepluses.com/api/v1/submit",
)
ASR_QUERY_URL = os.getenv(
    "ASR_QUERY_URL",
    "https://operator.las.ap-southeast-1.bytepluses.com/api/v1/poll",
)
ASR_OPERATOR_ID = os.getenv("ASR_OPERATOR_ID", "las_asr")
ASR_OPERATOR_VERSION = os.getenv("ASR_OPERATOR_VERSION", "v2")
ASR_MODEL_NAME = os.getenv("ASR_MODEL_NAME", "bigmodel")
ASR_POLL_INTERVAL = int(os.getenv("ASR_POLL_INTERVAL", "5"))
ASR_POLL_TIMEOUT = int(os.getenv("ASR_POLL_TIMEOUT", "900"))
ASR_ENABLE_SPEAKER_INFO = os.getenv("ASR_ENABLE_SPEAKER_INFO", "true").strip().lower() not in {"0", "false", "no"}


def _resolve_api_key(explicit: str | None = None) -> str:
    """
    Prefer LAS_API_KEY, then ASR_API_KEY for backward compatibility.
    """
    api_key = (explicit or os.getenv("LAS_API_KEY") or os.getenv("ASR_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing LAS_API_KEY (or ASR_API_KEY fallback) in tool/.env.")
    return api_key


def _headers(credential: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": credential,
    }


def submit_asr_task(
    audio_url: str,
    *,
    language: str = "zh-CN",
    api_key: str | None = None,
    audio_format: str = "wav",
    enable_itn: bool = True,
    enable_punc: bool = True,
    enable_speaker_info: bool = True,
) -> str:
    credential = _resolve_api_key(api_key)
    enable_speaker_info = bool(enable_speaker_info) and ASR_ENABLE_SPEAKER_INFO
    payload = {
        "operator_id": ASR_OPERATOR_ID,
        "operator_version": ASR_OPERATOR_VERSION,
        "data": {
            "audio": {
                "url": audio_url,
                "format": audio_format,
            },
            "request": {
                "model_name": ASR_MODEL_NAME,
                "enable_itn": enable_itn,
                "enable_punc": enable_punc,
                "enable_speaker_info": enable_speaker_info,
                # Keep source language as a hint in case LAS supports it in this deployment.
                "language": language,
            },
        },
    }
    result = common.post_json(ASR_SUBMIT_URL, payload, _headers(credential), timeout=120)
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    task_id = metadata.get("task_id") or result.get("task_id") or result.get("id")
    if not task_id:
        raise RuntimeError(f"ASR submit failed: {json.dumps(result, ensure_ascii=False)[:1000]}")
    return str(task_id)


def get_asr_task(task_id: str, api_key: str | None = None) -> dict:
    credential = _resolve_api_key(api_key)
    payload = {
        "operator_id": ASR_OPERATOR_ID,
        "operator_version": ASR_OPERATOR_VERSION,
        "task_id": task_id,
    }
    return common.post_json(ASR_QUERY_URL, payload, _headers(credential), timeout=120)


def poll_asr_task(task_id: str, api_key: str | None = None, verbose: bool = False) -> dict:
    start = time.time()
    while True:
        if time.time() - start > ASR_POLL_TIMEOUT:
            raise TimeoutError(f"ASR task timeout after {ASR_POLL_TIMEOUT}s: {task_id}")
        result = get_asr_task(task_id, api_key=api_key)
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        status = str(metadata.get("task_status") or result.get("status") or result.get("Status") or "").upper()
        if verbose:
            print(f"[asr] task={task_id} status={status or 'unknown'} elapsed={int(time.time()-start)}s", flush=True)
        if status in {"COMPLETED", "SUCCEEDED", "SUCCESS", "DONE"}:
            return result
        if status in {"FAILED", "ERROR"}:
            raise RuntimeError(f"ASR task failed: {json.dumps(result, ensure_ascii=False)[:1200]}")
        time.sleep(ASR_POLL_INTERVAL)


def _collect_texts(node, acc: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = key.lower()
            if key_l in {"text", "transcript", "utterance", "content"} and isinstance(value, str):
                text = value.strip()
                if text:
                    acc.append(text)
            else:
                _collect_texts(value, acc)
    elif isinstance(node, list):
        for item in node:
            _collect_texts(item, acc)


def extract_transcript_text(result: dict) -> str:
    """
    Best-effort transcript extraction across possible response shapes.
    """
    candidates: list[str] = []
    data = result.get("data")
    if isinstance(data, dict):
        inner_result = data.get("result")
        if isinstance(inner_result, dict):
            if isinstance(inner_result.get("text"), str) and inner_result["text"].strip():
                candidates.append(inner_result["text"].strip())
            utterances = inner_result.get("utterances")
            if isinstance(utterances, list):
                for item in utterances:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        text = item["text"].strip()
                        if text:
                            candidates.append(text)
    for top_key in ("result", "data", "payload"):
        if top_key in result:
            _collect_texts(result[top_key], candidates)
    if not candidates:
        _collect_texts(result, candidates)

    seen: set[str] = set()
    uniq: list[str] = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return "\n".join(uniq).strip()


def extract_utterances(result: dict) -> list[dict]:
    """
    Extract LAS ASR utterances with timing info.

    Returns items like:
      {
        "start_ms": 640,
        "end_ms": 2320,
        "text": "...",
        "speaker": "A" | None,
      }
    """
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    inner = data.get("result")
    if not isinstance(inner, dict):
        return []
    utts = inner.get("utterances")
    if not isinstance(utts, list):
        return []

    out: list[dict] = []
    for item in utts:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        start = item.get("start_time")
        end = item.get("end_time")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        start_ms = int(round(float(start)))
        end_ms = int(round(float(end)))
        if end_ms <= start_ms:
            continue

        speaker = None
        # Different LAS deployments may return speaker labels in different places.
        if isinstance(item.get("speaker_id"), str):
            speaker = item.get("speaker_id")
        elif isinstance(item.get("additions"), dict):
            add = item["additions"]
            if isinstance(add.get("speaker_id"), str):
                speaker = add.get("speaker_id")
            elif isinstance(add.get("speaker"), str):
                # Observed in LAS ASR v2 responses: additions.speaker = "1" / "2" / ...
                speaker = add.get("speaker")
            elif isinstance(add.get("channel_id"), str):
                speaker = add.get("channel_id")

        out.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
                "speaker": speaker,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit and poll BytePlus ASR HTTP recognition task.")
    parser.add_argument("--audio-url", required=True, help="Publicly accessible audio URL")
    parser.add_argument("--language", default=os.getenv("ASR_SOURCE_LANGUAGE", "zh-CN"))
    parser.add_argument("--audio-format", default="wav", help="Audio format for LAS ASR, e.g. wav/mp3/ogg/raw")
    parser.add_argument("--json", action="store_true", help="Print raw result JSON")
    args = parser.parse_args()

    try:
        task_id = submit_asr_task(args.audio_url, language=args.language, audio_format=args.audio_format)
        print(f"[asr] submitted task_id={task_id}", flush=True)
        result = poll_asr_task(task_id, verbose=True)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(extract_transcript_text(result))
        return 0
    except Exception as exc:
        print(f"ASR failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
