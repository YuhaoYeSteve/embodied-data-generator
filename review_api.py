"""Review existing media with Responses HTTP, bypassing the CLI enum decoder.

Uses only the named platform profile's selected key and its fixed official API.
No generation API, configuration changes, retries, or credential logging occur.
The saved response keeps the native API schema with credential/media redaction.
Wire fields follow volcengine-go-sdk v1.2.43 model/responses/responses.go:
ContentItemVideo (4254), ContentItemAudio (4033), ItemInputMessage (4328).
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from studio_config import SETTINGS


PROFILE = SETTINGS["review_profile"]
CONFIG_PATH = Path.home() / ".arkcli" / "config.yaml"
BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = SETTINGS["review_model"]
VERIFIED_ENDPOINTS = frozenset({DEFAULT_MODEL}) if DEFAULT_MODEL else frozenset()
TIMEOUT_SECONDS = 120


class ReviewError(RuntimeError):
    """Review failed or its outcome is unknown; no automatic retry is performed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_selected_key() -> str:
    """Read only the selected profile block; never expose YAML parser errors."""
    block = []
    inside = False
    try:
        with CONFIG_PATH.open(encoding="utf-8") as stream:
            for line in stream:
                if not inside:
                    if re.fullmatch(r"  " + re.escape(PROFILE) + r":\s*", line):
                        inside = True
                        block.append(line)
                    continue
                if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= 2:
                    break
                block.append(line)
    except OSError:
        raise ReviewError("could not read the selected platform profile") from None
    if not block:
        raise ReviewError("selected platform profile is missing")
    try:
        import yaml
    except ImportError:
        # The known disk schema is a two-space profile mapping with flat scalar
        # api_key/type/region fields. Reject YAML constructs needing a parser.
        selected = {}
        for line in block[1:]:
            match = re.fullmatch(r"    (api_key|type|region):\s*([^\r\n]*)\s*", line)
            if not match:
                continue
            name, value = match.groups()
            value = value.strip()
            try:
                if value.startswith('"'):
                    value = json.loads(value)
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1].replace("''", "'")
                elif not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ReviewError("selected profile scalar requires a YAML parser") from None
            if name in selected:
                raise ReviewError("selected profile contains duplicate scalar fields")
            selected[name] = value
    else:
        try:
            selected = yaml.safe_load("".join(block))[PROFILE]
        except Exception:
            # YAML errors may quote the credential-bearing source line.
            raise ReviewError("selected profile YAML could not be parsed") from None
    if selected.get("type") != "platform" or selected.get("region") != "cn-beijing":
        raise ReviewError("selected profile does not match the verified platform API")
    key = selected.get("api_key")  # CLI display calls this default_api_key.
    if not isinstance(key, str) or not key.strip() or "*" in key or any(c.isspace() for c in key):
        raise ReviewError("selected profile has no usable API key")
    return key


def _safe_string(value: str, secrets: list[str]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")

    def redact_url(match):
        try:
            parts = urlsplit(match.group(0))
            return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                               parts.path, "[REDACTED]" if parts.query else "", ""))
        except ValueError:
            return "[REDACTED_URL]"

    value = re.sub(r"https?://[^\s<>\"']+", redact_url, value)
    value = re.sub(r"data:audio/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[REDACTED_AUDIO]", value)
    return value


def _redact(value, secrets: list[str]):
    if isinstance(value, dict):
        return {_safe_string(str(key), secrets): _redact(item, secrets)
                for key, item in value.items()
                if str(key).lower() not in {"headers", "request_headers", "authorization", "proxy-authorization"}}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        return _safe_string(value, secrets)
    return value


def _save_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials belong to the fixed official endpoint only.
        return None


def _open_request(request: Request, *, timeout: int):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def extract_text(response: dict) -> str:
    """Join native output_text content in output order, excluding reasoning."""
    return "".join(
        part["text"]
        for item in response.get("output", []) if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
    )


def run_review(video_urls: list[str], prompt: str, output_path,
               audio_paths=None, *, model: str = None) -> dict:
    """Make one paid review request, save its evidence, and return native API JSON.

    ``output_path`` contains status, times, endpoint, request hashes/summary,
    response, usage and extracted text. Inputs, headers and keys are not saved.
    The returned API dictionary is also redacted if the service echoes secrets.
    HTTP errors, incomplete responses and empty text raise ``ReviewError``;
    transport/decode errors record ``unknown`` because usage may have occurred.
    """
    model = DEFAULT_MODEL if model is None else model
    path = Path(output_path)
    record = {"status": "preparing", "endpoint": model, "profile": PROFILE,
              "started_at": _utc_now(), "finished_at": None,
              "request_sha256": None, "request_summary": {},
              "response": None, "usage": None, "text": ""}
    secrets = []
    sent = False
    try:
        if not DEFAULT_MODEL:
            raise ReviewError("缺少 review_model 配置；请在本地配置文件中指定质检模型。")
        if model not in VERIFIED_ENDPOINTS:
            raise ReviewError("model must match the locally configured review_model")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ReviewError("review prompt must be nonempty text")
        if not isinstance(video_urls, list) or (not video_urls and not audio_paths):
            raise ReviewError("at least one existing video or audio input is required")
        content = []
        for url in video_urls:
            if not isinstance(url, str):
                raise ReviewError("video URLs must be strings")
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
                raise ReviewError("video URLs must use HTTPS without embedded credentials")
            content.append({"type": "input_video", "video_url": url})
            secrets.append(url)
        audio_paths = list(audio_paths or [])
        audio_hashes = []
        mime_types = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}
        for audio_path in audio_paths:
            source = Path(audio_path)
            mime = mime_types.get(source.suffix.lower())
            if mime is None:
                raise ReviewError("audio must be a WAV, MP3 or M4A file")
            data = source.read_bytes()
            if not data or len(data) > 25 * 1024 * 1024:
                raise ReviewError("audio must be nonempty and at most 25 MiB")
            audio_url = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
            content.append({"type": "input_audio", "audio_url": audio_url})
            audio_hashes.append(_sha256(data))
            secrets.append(audio_url)
        content.append({"type": "input_text", "text": prompt})
        body = {"model": model, "input": [{"role": "user", "content": content}],
                "max_output_tokens": 1800, "thinking": {"type": "disabled"},
                "store": False, "stream": False}
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        record["request_sha256"] = _sha256(encoded)
        record["request_summary"] = {"video_count": len(video_urls), "audio_count": len(audio_paths),
                                      "prompt_sha256": _sha256(prompt.encode("utf-8")),
                                      "video_url_sha256": [_sha256(url.encode("utf-8")) for url in video_urls],
                                      "audio_sha256": audio_hashes, "max_output_tokens": 1800,
                                      "thinking": "disabled", "store": False, "stream": False}
        key = _load_selected_key()
        secrets.append(key)
        request = Request(BASE_URL + "/responses", data=encoded, method="POST",
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json", "Accept": "application/json"})
        record["status"] = "submitting"
        _save_record(path, record)
        sent = True
        with _open_request(request, timeout=TIMEOUT_SECONDS) as response:
            record["http_status"] = response.status
            response_bytes = response.read()
        raw = json.loads(response_bytes)
        if not isinstance(raw, dict):
            raise ValueError("response is not a JSON object")
        raw = _redact(raw, secrets)
        record["response"] = raw
        record["usage"] = raw.get("usage")
        record["text"] = extract_text(raw)
        if raw.get("status") != "completed" or not record["text"].strip():
            raise ReviewError("review response is incomplete or has no output text")
        record["status"] = "succeeded"
        return raw
    except HTTPError as exc:
        detail = exc.read(1024 * 1024).decode("utf-8", "replace")
        try:
            detail = json.loads(detail)
        except ValueError:
            pass
        record["status"] = "unknown" if exc.code == 408 or exc.code >= 500 else "failed"
        record["http_status"] = exc.code
        record["error"] = {"type": "http_error", "message": f"HTTP {exc.code}",
                           "response": _redact(detail, secrets)}
        raise ReviewError(f"review HTTP {exc.code}; redacted details saved to the output file") from None
    except ReviewError as exc:
        record["status"] = "failed"
        record["error"] = {"type": "review_error", "message": _safe_string(str(exc), secrets)}
        raise ReviewError(record["error"]["message"]) from None
    except (OSError, URLError, ValueError, TypeError) as exc:
        record["status"] = "unknown" if sent else "failed"
        record["error"] = {"type": type(exc).__name__, "message": _safe_string(str(exc), secrets)}
        raise ReviewError("review failed; details saved to the output file; no retry was made") from None
    finally:
        record["finished_at"] = _utc_now()
        _save_record(path, _redact(record, secrets))


def _video_url_from_response(path) -> str:
    """Read the observed CLI/API generation shapes without making a query."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ReviewError("video response file must contain a JSON object")
    envelope = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    task = envelope.get("task") if isinstance(envelope.get("task"), dict) else envelope
    for candidate in (envelope, task):
        url = candidate.get("output_url")
        if not url and isinstance(candidate.get("content"), dict):
            url = candidate["content"].get("video_url")
        if isinstance(url, str) and url:
            return url
    raise ReviewError("video response file has no output_url or content.video_url")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-response", action="append", default=[], help="Existing generation response JSON; repeat for comparison")
    parser.add_argument("--audio", action="append", default=[], help="Optional existing WAV/MP3/M4A file")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", choices=sorted(VERIFIED_ENDPOINTS), default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    try:
        urls = [_video_url_from_response(path) for path in args.video_response]
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        raw = run_review(urls, prompt, args.output, args.audio, model=args.model)
    except ReviewError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print("could not read review input files", file=sys.stderr)
        return 1
    print(extract_text(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
