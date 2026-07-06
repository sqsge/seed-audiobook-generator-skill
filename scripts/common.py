from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


TOOL_DIR = Path(__file__).resolve().parent
SKILL_DIR = TOOL_DIR.parent


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


def load_tool_env() -> None:
    candidates = [
        SKILL_DIR / ".env",
        SKILL_DIR / ".env.local",
        TOOL_DIR / ".env",
        TOOL_DIR / ".env.local",
        Path.home() / ".openclaw" / ".env",
        Path.home() / ".openclaw" / "workspace" / "tool" / ".env",
    ]
    for candidate in candidates:
        load_env_file(candidate)


def get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except Exception:
        return default


def file_to_data_url(path: str) -> str:
    file_path = Path(path)
    mime_type, _ = mimetypes.guess_type(file_path.name)
    if not mime_type:
        mime_type = "application/octet-stream"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def download_url(url: str, output_path: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    timeout = get_env_int("DOWNLOAD_TIMEOUT", 600)
    retries = get_env_int("DOWNLOAD_RETRIES", 3)
    for attempt in range(1, max(1, retries) + 1):
        try:
            with urlopen(req, timeout=timeout) as response:
                data = response.read()
            break
        except Exception:
            if attempt >= retries:
                raise
            # Exponential backoff to avoid hammering transient network issues.
            import time

            time.sleep(min(30, 2 ** (attempt - 1)))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return str(output)


def post_json(url: str, payload: dict, headers: dict[str, str], timeout: int = 120) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        # Bubble up a message that includes the response body for debugging.
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body or exc.reason}") from exc


def get_env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
