"""Shell-free handlers for native Claude background sessions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .safety import without_prompt

ALLOWED_PERMISSION_MODES = ("manual", "acceptEdits", "plan")
COMMAND_TIMEOUT_SECONDS = 20
MAX_OUTPUT_CHARS = 8_192
_TRUNCATION_MARKER = "\n[truncated]"


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_claude_bin() -> str | None:
    """Find an executable Claude CLI with predictable local precedence."""
    configured = os.environ.get("CLAUDE_BIN")
    if configured:
        path = Path(configured).expanduser()
        if _is_executable(path):
            return str(path)

    discovered = shutil.which("claude")
    if discovered:
        return discovered

    fallback = Path.home() / ".local" / "bin" / "claude"
    if _is_executable(fallback):
        return str(fallback)
    return None


def _bounded_output(value: str | None, prompt: str | None = None) -> str:
    output = value or ""
    if prompt:
        output = without_prompt(output, prompt)
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    return output[: MAX_OUTPUT_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _redact_prompt_value(value: Any, prompt: str) -> Any:
    if isinstance(value, str):
        return without_prompt(value, prompt)
    if isinstance(value, list):
        return [_redact_prompt_value(item, prompt) for item in value]
    if isinstance(value, dict):
        return {key: _redact_prompt_value(item, prompt) for key, item in value.items()}
    return value


def _redact_prompt_from_json(value: str, prompt: str | None) -> str:
    if not prompt:
        return value
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return _error("Claude session start returned malformed internal JSON")
    if not isinstance(payload, dict):
        return _error("Claude session start returned malformed internal JSON")
    redacted = _redact_prompt_value(payload, prompt)
    result = json.dumps(redacted)
    if len(result) <= MAX_OUTPUT_CHARS:
        return result
    compact: dict[str, Any] = {
        "ok": redacted.get("ok") is True,
        "output_omitted": True,
    }
    if isinstance(redacted.get("returncode"), int):
        compact["returncode"] = redacted["returncode"]
    if isinstance(redacted.get("error"), str):
        compact["error"] = redacted["error"]
    return _json(compact)


def _error(message: str, **details: Any) -> str:
    return _json({"ok": False, "error": message, **details})


def _list_error(message: str, **details: Any) -> str:
    result = _error(message, **details)
    if len(result) <= MAX_OUTPUT_CHARS:
        return result
    compact: dict[str, Any] = {"output_omitted": True}
    if isinstance(details.get("returncode"), int):
        compact["returncode"] = details["returncode"]
    return _error(message, **compact)


def _parse_start_args(args: object) -> tuple[str, str, Path, str] | str:
    if not isinstance(args, Mapping):
        return _error("arguments must be an object")

    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error("name must be a non-empty string")

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("prompt must be a non-empty string")

    cwd = args.get("cwd")
    if not isinstance(cwd, str):
        return _error("cwd must be an absolute existing directory")
    path = Path(cwd)
    if not path.is_absolute() or not path.is_dir():
        return _error("cwd must be an absolute existing directory")

    mode = args.get("permission_mode", "manual")
    if mode not in ALLOWED_PERMISSION_MODES:
        return _error("permission_mode must be one of: manual, acceptEdits, plan")
    return name, prompt, path, mode


def claude_session_start(args: object, **kwargs: object) -> str:
    """Start one native Claude background session without exposing its prompt."""
    del kwargs
    prompt: str | None = None
    try:
        if isinstance(args, Mapping):
            candidate = args.get("prompt")
            if isinstance(candidate, str):
                prompt = candidate
        result = _claude_session_start(args)
    except Exception:
        result = _error("Claude session start failed unexpectedly")
    return _redact_prompt_from_json(result, prompt)


def _claude_session_start(args: object) -> str:
    parsed = _parse_start_args(args)
    if isinstance(parsed, str):
        return parsed
    name, prompt, cwd, mode = parsed
    details = {"name": name, "cwd": str(cwd), "mode": mode}

    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        return _error(
            "Claude CLI is unavailable; set CLAUDE_BIN or install an executable "
            "claude binary",
            **details,
            stdout="",
            stderr="",
        )

    argv = [
        claude_bin,
        "--bg",
        "--name",
        name,
        "--permission-mode",
        mode,
        "--",
        prompt,
    ]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error(
            "Claude command timed out after 20 seconds",
            **details,
            stdout="",
            stderr="",
        )
    except OSError:
        return _error(
            "Claude command could not be started", **details, stdout="", stderr=""
        )

    payload = {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        **details,
        "stdout": _bounded_output(completed.stdout, prompt),
        "stderr": _bounded_output(completed.stderr, prompt),
    }
    if completed.returncode != 0:
        payload["error"] = "Claude command exited with a non-zero status"
    return _json(payload)


def claude_session_list(args: object, **kwargs: object) -> str:
    """Return the native Claude CLI's unmodified session rows."""
    del args, kwargs
    try:
        return _claude_session_list()
    except Exception:
        return _error("Claude session list failed unexpectedly")


def _claude_session_list() -> str:
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        return _list_error(
            "Claude CLI is unavailable; set CLAUDE_BIN or install an executable "
            "claude binary",
            stdout="",
            stderr="",
        )

    try:
        completed = subprocess.run(
            [claude_bin, "agents", "--json", "--all"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _list_error(
            "Claude command timed out after 20 seconds", stdout="", stderr=""
        )
    except OSError:
        return _list_error("Claude command could not be started", stdout="", stderr="")

    stdout = _bounded_output(completed.stdout)
    stderr = _bounded_output(completed.stderr)
    if completed.returncode != 0:
        return _list_error(
            "Claude command exited with a non-zero status",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    if len(completed.stdout) > MAX_OUTPUT_CHARS:
        return _error("Claude session list is too large to return safely")
    try:
        sessions = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return _list_error(
            "Claude session list returned malformed JSON", stdout=stdout, stderr=stderr
        )
    if not isinstance(sessions, list):
        return _list_error(
            "Claude session list must be a top-level JSON list",
            stdout=stdout,
            stderr=stderr,
        )
    result = _json({"ok": True, "sessions": sessions})
    if len(result) > MAX_OUTPUT_CHARS:
        return _error("Claude session list is too large to return safely")
    return result
