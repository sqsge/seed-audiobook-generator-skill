#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import load_tool_env


load_tool_env()


def _file_to_data_url(path: str) -> str:
    file_path = Path(path)
    mime_type, _ = mimetypes.guess_type(file_path.name)
    if not mime_type:
        mime_type = "application/octet-stream"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _resolve_api_key(explicit: str | None) -> str:
    api_key = explicit or os.getenv("LLM_API_KEY")
    if not api_key:
        api_key = os.getenv("ARK_API_KEY") or os.getenv("BYTEPLUS_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing API key. Set LLM_API_KEY, ARK_API_KEY, BYTEPLUS_API_KEY, or OPENAI_API_KEY.")
    return api_key


def _resolve_base_url(explicit: str | None) -> str:
    if explicit:
        return explicit

    env_base_url = os.getenv("LLM_BASE_URL") or os.getenv("ARK_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if env_base_url:
        return env_base_url

    if os.getenv("OPENAI_API_KEY") and not (os.getenv("ARK_API_KEY") or os.getenv("BYTEPLUS_API_KEY")):
        return "https://api.openai.com/v1"

    return "https://ark.ap-southeast.bytepluses.com/api/v3"


def _resolve_model(explicit: str | None, base_url: str) -> str:
    if explicit:
        return explicit
    env_model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("ARK_MODEL")
    if env_model:
        return env_model
    if "openai.com" in base_url:
        return "gpt-4.1-mini"
    return "seed-2-0-pro-260328"


def _media_url(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://", "data:", "tos://")):
        return path_or_url
    return _file_to_data_url(path_or_url)


def _audio_content(path_or_url: str) -> dict:
    """Build the OpenAI-compatible audio input block used by Seed multimodal chat."""
    if path_or_url.startswith(("http://", "https://")):
        return {"type": "input_audio", "input_audio": {"url": path_or_url}}
    path = Path(path_or_url)
    return {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "format": path.suffix.lstrip(".").lower() or "wav",
        },
    }


def _build_messages(
    prompt: str,
    system: str | None,
    images: list[str],
    videos: list[str] | None = None,
    audios: list[str] | None = None,
) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})

    videos = videos or []
    audios = audios or []
    if images or videos or audios:
        content: list[dict] = []
        for image in images:
            image_url = _media_url(image)
            content.append({"type": "image_url", "image_url": {"url": image_url}})
        for video in videos:
            video_url = _media_url(video)
            content.append({"type": "video_url", "video_url": {"url": video_url}})
        for audio in audios:
            content.append(_audio_content(audio))
        content.append({"type": "text", "text": prompt})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    return messages


def _chat_completion(base_url: str, credential: str, payload: dict) -> dict:
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {credential}",
    }
    request = Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=int(os.getenv("LLM_TIMEOUT", "300"))) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_text(result: dict) -> str:
    choices = result.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


def chat_text(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    """
    Small reusable helper for other scripts in this repo.
    """
    resolved_base_url = _resolve_base_url(base_url)
    resolved_credential = _resolve_api_key(api_key)
    resolved_model = _resolve_model(model, resolved_base_url)
    payload: dict = {
        "model": resolved_model,
        "messages": _build_messages(prompt, system, []),
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    result = _chat_completion(resolved_base_url, resolved_credential, payload)
    return _extract_text(result).strip()


def chat_audio(
    prompt: str,
    audio_paths: list[str],
    *,
    system: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> str:
    """Ask a multimodal chat model to review one or more local audio files."""
    if not audio_paths:
        raise ValueError("chat_audio requires at least one audio path")
    resolved_base_url = _resolve_base_url(base_url)
    resolved_credential = _resolve_api_key(api_key)
    resolved_model = _resolve_model(model, resolved_base_url)
    payload: dict = {
        "model": resolved_model,
        "messages": _build_messages(prompt, system, [], audios=audio_paths),
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    result = _chat_completion(resolved_base_url, resolved_credential, payload)
    return _extract_text(result).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Call an OpenAI-compatible chat model.")
    parser.add_argument("-p", "--prompt", help="User prompt")
    parser.add_argument("--prompt-file", help="Read the user prompt from a file")
    parser.add_argument("-s", "--system", default=os.getenv("LLM_SYSTEM_PROMPT", ""), help="System prompt")
    parser.add_argument("-m", "--model", help="Model name")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", help="API key override")
    parser.add_argument("--image", nargs="*", default=[], help="Reference images as local paths or URLs")
    parser.add_argument("--video", nargs="*", default=[], help="Reference videos as local paths or URLs")
    parser.add_argument("--audio", nargs="*", default=[], help="Reference audio as local paths or URLs")
    parser.add_argument("--temperature", type=float, default=0.7, help="Sampling temperature")
    parser.add_argument("--max-tokens", type=int, help="Max output tokens")
    parser.add_argument("--json", action="store_true", help="Print raw JSON response")
    args = parser.parse_args()

    prompt = args.prompt
    if not prompt and args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not prompt:
        print("Missing --prompt or --prompt-file", file=sys.stderr)
        return 2

    base_url = _resolve_base_url(args.base_url)
    credential = _resolve_api_key(args.api_key)
    model = _resolve_model(args.model, base_url)

    payload: dict = {
        "model": model,
        "messages": _build_messages(prompt, args.system or None, args.image, args.video, args.audio),
        "temperature": args.temperature,
    }
    if args.max_tokens is not None:
        payload["max_tokens"] = args.max_tokens

    try:
        result = _chat_completion(base_url, credential, payload)
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            pass
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Chat failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    text = _extract_text(result)
    if text:
        print(text)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
