"""ArkCLI video-task backend for experiments with complete request provenance.

Authentication is delegated to the selected ArkCLI profile. The adapter never
loads API keys or invokes a shell. Request bodies and raw CLI responses may
contain signed media URLs; persist those only in private experiment artifacts.
``raw`` is the complete parsed CLI JSON response, not the original API response.
The CLI may omit API fields, including the requested last-frame URL; this adapter
cannot restore omitted fields and does not invent them.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit
from studio_models import get_model, infer_model_family


def _sanitize(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value or "")
    text = re.sub(r"(?i)data:(?:image|audio|video)/[^,\s<>\"']*,[^\s<>\"']*",
                  "[REDACTED_MEDIA_DATA]", text)

    def redact_url(match):
        try:
            parts = urlsplit(match.group(0))
            host = parts.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parts.scheme, host, parts.path,
                               "[REDACTED]" if parts.query else "", ""))
        except ValueError:
            return "[REDACTED_URL]"

    text = re.sub(r"https?://[^\s<>\"']+", redact_url, text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,\"'}]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(\b(?:(?:ARK|OPENAI)_)?(?:api[_ -]?key|access[_ -]?token|"
        r"refresh[_ -]?token|token|password|secret[_ -]?key)[\"']?\s*[:=]\s*[\"']?)"
        r"[^\s,\"'}]+", r"\1[REDACTED]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{6,}", "[REDACTED]", text)


class ArkCLIError(RuntimeError):
    """CLI/query failure with sanitized diagnostics and an optional CLI response.

    ``raw`` is the parsed CLI JSON, which may contain private media URLs.

    A submit failure is definite only when it is this type rather than its
    ``SubmissionUnknown`` subclass. No error causes an automatic retry here.
    """

    def __init__(self, message: str, *, stderr="", returncode=None, raw=None):
        self.stderr = _sanitize(stderr)
        self.returncode = returncode
        self.raw = raw
        detail = _sanitize(message)
        if self.stderr:
            detail += "\n" + self.stderr
        super().__init__(detail[:16384])


class SubmissionUnknown(ArkCLIError):
    """The server may have accepted the task; reconcile before submitting again."""


def _parse_json_output(stdout: str) -> dict:
    """Accept one JSON object, optionally preceded by complete human notice lines.

    Do not search for arbitrary braces: that can recover a nested object from a
    truncated response or silently accept a second conflicting JSON document.
    """
    text = stdout.lstrip("\ufeff \t\r\n")
    decoder = json.JSONDecoder()
    while text:
        if text.startswith("{"):
            result, end = decoder.raw_decode(text)
            if text[end:].strip() or not isinstance(result, dict):
                raise ValueError("expected one complete JSON object")
            return result
        if text.startswith("["):
            try:
                decoder.raw_decode(text)
            except json.JSONDecodeError:
                pass  # A human prefix such as [INFO] is allowed.
            else:
                raise ValueError("expected a JSON object, not a list")
        _, separator, text = text.partition("\n")
        if not separator:
            break
        text = text.lstrip(" \t\r")
    raise ValueError("CLI did not return a complete JSON object")


def _normalize(raw: dict) -> dict:
    envelope = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    task = envelope.get("task") if isinstance(envelope.get("task"), dict) else {}
    result = dict(task)
    result.update({key: value for key, value in envelope.items() if key != "task"})
    task_id = envelope.get("task_id") or envelope.get("id") or task.get("task_id") or task.get("id")
    if isinstance(task_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", task_id):
        result["id"] = task_id
    else:
        result.pop("id", None)
    status = result.get("status")
    if isinstance(status, dict) and isinstance(status.get("phase"), str):
        result["status"] = status["phase"]
    result["raw"] = raw
    return result


def _error_object(raw: dict | None):
    if raw is None:
        return None
    envelope = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    return envelope.get("error")


def _definite_rejection(returncode: int, error) -> bool:
    if returncode == 2:
        return True  # CLI argument parsing failure.
    if not isinstance(error, dict):
        return False
    code = str(error.get("code", ""))
    return bool(re.match(
        r"^(Invalid|Missing|Authentication|Unauthorized|Permission|Forbidden|"
        r"AccessDenied|NotFound|QuotaExceeded|InsufficientBalance|Throttl)", code,
        flags=re.IGNORECASE))


# Keep this allowlist narrow: --input infers media type from the extension and
# silently drops unknown extensions. Unsupported content must fail locally.
_MEDIA_INPUTS = {
    "image_url": {
        "roles": {"first_frame", "last_frame", "reference_image"},
        "mimes": {"image/png": ".png", "image/jpeg": ".jpg",
                  "image/webp": ".webp", "image/gif": ".gif"},
        "extensions": {".png", ".jpg", ".jpeg", ".webp", ".gif"},
    },
    "video_url": {
        "roles": {"reference_video"},
        "mimes": {"video/mp4": ".mp4", "video/webm": ".webm"},
        "extensions": {".mp4", ".webm", ".mov"},
    },
    "audio_url": {
        "roles": {"reference_audio"},
        "mimes": {"audio/mpeg": ".mp3", "audio/wav": ".wav",
                  "audio/ogg": ".ogg", "audio/flac": ".flac"},
        "extensions": {".mp3", ".wav", ".ogg", ".flac", ".m4a"},
    },
}


def _has_inline_media(body: dict) -> bool:
    for item in body["content"]:
        if not isinstance(item, dict):
            continue
        media = item.get(item.get("type")) if isinstance(item.get("type"), str) else None
        url = media.get("url") if isinstance(media, dict) else None
        if isinstance(url, str) and url.lower().startswith("data:"):
            return True
    return False


def _stage_media_inputs(content: list, directory: Path) -> list[str]:
    """Convert only losslessly representable content into ordered CLI inputs."""
    if not content or not isinstance(content[0], dict) or set(content[0]) != {"type", "text"} or content[0]["type"] != "text":
        raise ValueError("inline media requires the text item first, without extra metadata")
    inputs = []
    for index, item in enumerate(content[1:], 1):
        if not isinstance(item, dict):
            raise ValueError(f"content[{index}] must be a media object")
        kind = item.get("type")
        spec = _MEDIA_INPUTS.get(kind) if isinstance(kind, str) else None
        if spec is None or set(item) != {"type", kind, "role"}:
            raise ValueError(f"content[{index}] has unsupported media fields or missing explicit role")
        role = item["role"]
        if not isinstance(role, str) or role not in spec["roles"]:
            raise ValueError(f"content[{index}] has an unsupported media role")
        media = item[kind]
        if not isinstance(media, dict) or set(media) != {"url"}:
            raise ValueError(f"content[{index}] URL metadata cannot be represented by CLI inputs")
        url = media["url"]
        if not isinstance(url, str) or not url or "\x00" in url:
            raise ValueError(f"content[{index}] requires a nonempty media URL")
        if url.lower().startswith("data:"):
            match = re.fullmatch(r"data:([^;,]+);base64,(.*)", url, re.DOTALL)
            suffix = spec["mimes"].get(match[1]) if match else None
            if suffix is None:
                raise ValueError(f"content[{index}] requires a supported matching base64 media MIME type")
            try:
                data = base64.b64decode(match[2], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"content[{index}] contains invalid base64 media") from None
            if not data or base64.b64encode(data).decode("ascii") != match[2]:
                raise ValueError(f"content[{index}] requires nonempty canonical base64 media")
            path = directory / f"input-{index:03d}{suffix}"
            path.write_bytes(data)
            reference = "@" + str(path)
        else:
            try:
                parts = urlsplit(url)
                supported = (parts.scheme in {"https", "http", "tos"} and parts.netloc
                             and Path(parts.path).suffix.lower() in spec["extensions"])
            except ValueError:
                supported = False
            if not supported:
                raise ValueError(f"content[{index}] requires a remote URL with a matching media extension")
            reference = url
        inputs.extend(["--input", f"{role}:{reference}"])
    return inputs


class ArkCLIClient:
    """Submit/query through a named CLI profile and one configured endpoint."""

    def __init__(self, profile: str, model: str, *, timeout: float = 120.0):
        for field, value in (("profile", profile), ("model", model)):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{field} must be a nonempty string")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number of seconds")
        self.profile = profile
        self.model = model
        self.timeout = timeout

    def build_submit_argv(self, body: dict) -> list[str]:
        """Build a reviewable argv without executing it or changing ``body``.

        Standard video fields must be explicit, so CLI defaults never silently
        alter a recorded request. ``model`` may be omitted and uses this client.
        The complete request uses ``--extra-body`` to preserve remote references
        and extra server fields. Use ``submit`` to execute: it stages inline
        media in temporary files so large data URIs never enter subprocess argv.
        """
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        if body.get("model", self.model) != self.model:
            raise ValueError("body model must match the configured client model")
        content = body.get("content")
        if not isinstance(content, list):
            raise ValueError("content must contain exactly one nonempty text item")
        texts = [part.get("text") for part in content
                 if isinstance(part, dict) and part.get("type") == "text"]
        if len(texts) != 1 or not isinstance(texts[0], str) or not texts[0].strip():
            raise ValueError("content must contain exactly one nonempty text item")
        duration = body.get("duration")
        if type(duration) is not int or (duration <= 0 and duration != -1):
            raise ValueError("duration must be a positive integer or -1")
        for field in ("ratio", "resolution"):
            if not isinstance(body.get(field), str) or not body[field].strip():
                raise ValueError(f"{field} must be an explicit nonempty string")
        for field in ("generate_audio", "return_last_frame"):
            if type(body.get(field)) is not bool:
                raise ValueError(f"{field} must be an explicit boolean")
        family = infer_model_family(self.model)
        if family:
            capability = get_model(family)
            if duration != -1 and duration not in capability['durations']:
                raise ValueError('%s 不支持该 duration' % capability['label'])
            if body['resolution'] not in capability['resolutions']:
                raise ValueError('%s 不支持该 resolution' % capability['label'])
        wire = dict(body, model=self.model)
        extra_body = json.dumps(wire, ensure_ascii=False, allow_nan=False)
        return [
            "arkcli", "+gen", texts[0],
            "--model", self.model,
            "--modality", "video",
            "--profile", self.profile,
            "--duration", str(duration),
            "--ratio", body["ratio"],
            "--resolution", body["resolution"],
            "--generate-audio" if body["generate_audio"] else "--generate-audio=false",
            "--return-last-frame" if body["return_last_frame"] else "--return-last-frame=false",
            "--extra-body", extra_body,
            "--save-to", "", "--no-open", "--format", "json",
        ]

    def submit(self, body: dict) -> dict:
        """Submit once; return task fields plus the complete CLI JSON as ``raw``.

        Catch ``SubmissionUnknown`` before ``ArkCLIError`` and persist that state;
        an unknown outcome must be reconciled instead of automatically retried.
        Local input errors raise ``ValueError`` without executing the CLI.
        """
        argv = self.build_submit_argv(body)
        if not _has_inline_media(body):
            return self._execute(argv, submitting=True)
        # This scope must enclose _execute, including all rejection/unknown paths.
        # The recorded body remains unchanged; only transport representation differs.
        with tempfile.TemporaryDirectory(prefix="arkcli-media-") as directory:
            inputs = _stage_media_inputs(body["content"], Path(directory))
            wire = {key: value for key, value in body.items() if key != "content"}
            wire["model"] = self.model
            argv[argv.index("--extra-body") + 1] = json.dumps(
                wire, ensure_ascii=False, allow_nan=False)
            argv.extend(inputs)
            # Leave headroom for the environment on macOS. Extra fields/prompt
            # can also be large; fail definitely before starting a paid request.
            if sum(len(arg.encode("utf-8")) + 1 for arg in argv) > 65_536:
                raise ValueError("CLI arguments remain too large after staging inline media")
            return self._execute(argv, submitting=True)

    def get(self, task_id: str) -> dict:
        """Query once; preserve all CLI response fields and its JSON as ``raw``.

        ``status.phase`` is normalized to ``status``. CLI ``output_url`` stays
        ``output_url``. API fields omitted by the CLI are unavailable here.
        """
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", task_id):
            raise ValueError("task_id must be a nonempty CLI-safe identifier")
        return self._execute([
            "arkcli", "gen", "get", task_id, "--profile", self.profile,
            "--save-to", "", "--no-open", "--format", "json",
        ], submitting=False)

    def _execute(self, argv: list[str], *, submitting: bool) -> dict:
        env = os.environ.copy()
        env.update({
            "ARKCLI_NO_UPDATE_NOTIFIER": "1",
            "ARKCLI_CALLER_TYPE": "ai_agent",
            "ARKCLI_CALLER_NAME": "codex",
            "ARKCLI_SKILL_NAME": "arkcli-gen",
        })
        unknown_type = SubmissionUnknown if submitting else ArkCLIError
        action = "submission outcome unknown; do not resubmit" if submitting else "query failed; keep the existing task_id"
        try:
            completed = subprocess.run(
                argv, shell=False, capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace", timeout=self.timeout, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired.__str__ includes argv, potentially exposing prompts
            # and signed URLs. Use only its captured, sanitized stderr instead.
            raise unknown_type(f"ArkCLI timed out: {action}", stderr=exc.stderr) from None
        except OSError as exc:
            raise ArkCLIError("could not start ArkCLI", stderr=str(exc)) from None
        try:
            raw = _parse_json_output(completed.stdout)
        except (ValueError, TypeError):
            definite = _definite_rejection(completed.returncode, None)
            error_type = ArkCLIError if definite else unknown_type
            reason = "CLI argument validation failed" if definite else action
            raise error_type(f"ArkCLI returned no valid JSON (exit {completed.returncode}): {reason}",
                             stderr=completed.stderr, returncode=completed.returncode) from None
        result = _normalize(raw)
        error = _error_object(raw)
        # A valid created task ID is useful even when a later CLI-local step
        # failed; losing it could trigger a duplicate paid submission.
        if result.get("id") and (submitting or completed.returncode == 0):
            return result
        if completed.returncode or error:
            error_type = ArkCLIError if _definite_rejection(completed.returncode, error) else unknown_type
            detail = json.dumps(error, ensure_ascii=False) if error else "unspecified CLI failure"
            raise error_type(f"ArkCLI exit {completed.returncode}: {detail}",
                             stderr=completed.stderr, returncode=completed.returncode, raw=raw)
        if submitting:
            raise SubmissionUnknown("submission response has no task ID; reconcile before resubmitting",
                                    stderr=completed.stderr, returncode=completed.returncode, raw=raw)
        return result
