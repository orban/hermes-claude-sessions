"""Shell-free handlers for Hermes-managed native Claude sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import bridge
from .safety import without_prompt

ALLOWED_PERMISSION_MODES = ("auto", "manual", "acceptEdits", "plan")
DEFAULT_PERMISSION_MODE = "auto"
COMMAND_TIMEOUT_SECONDS = 20
MAX_OUTPUT_CHARS = 8_192
LOG_STDOUT_CHARS = 5_500
LOG_STDERR_CHARS = 1_000
_TRUNCATION_MARKER = "\n[truncated]"
_LEADING_TRUNCATION_MARKER = "[truncated]\n"
_LAUNCH_ID = re.compile(r"backgrounded\s+·\s+([0-9a-f]{8})\s+·")
_NATIVE_ID = re.compile(r"^[0-9a-f]{8}$")
_CLAUDE_PLUGIN_DIR = Path(__file__).resolve().parent / "claude_plugin"


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


def _bounded_tail(value: str | None, limit: int) -> str:
    output = value or ""
    if len(output) <= limit:
        return output
    return (
        _LEADING_TRUNCATION_MARKER
        + output[-(limit - len(_LEADING_TRUNCATION_MARKER)) :]
    )


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _bounded_json(payload: dict[str, Any], message: str) -> str:
    result = _json(payload)
    if len(result) <= MAX_OUTPUT_CHARS:
        return result
    return _error(message, output_omitted=True)


def _bounded_log_json(payload: dict[str, Any]) -> str:
    """Fit recent log tails to the serialized tool-result byte budget."""
    while True:
        result = _json(payload)
        if len(result.encode("utf-8")) <= MAX_OUTPUT_CHARS:
            return result
        shrunk = False
        for field in ("stdout", "stderr"):
            value = payload.get(field)
            if isinstance(value, str) and len(value) > 256:
                payload[field] = _bounded_tail(value, max(256, len(value) // 2))
                shrunk = True
        if not shrunk:
            return _error(
                "Claude session logs are too large to return safely",
                output_omitted=True,
            )


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

    mode = args.get("permission_mode", DEFAULT_PERMISSION_MODE)
    if mode not in ALLOWED_PERMISSION_MODES:
        allowed = ", ".join(ALLOWED_PERMISSION_MODES)
        return _error(f"permission_mode must be one of: {allowed}")
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

    managed_session: Path | None = None
    environment: dict[str, str] | None = None
    argv = [
        claude_bin,
        "--bg",
        "--name",
        name,
        "--permission-mode",
        mode,
    ]
    if mode == "auto":
        managed_session = bridge.create_session(name, cwd)
        environment = os.environ.copy()
        environment[bridge.SESSION_DIR_ENV] = str(managed_session)
        argv.extend(["--plugin-dir", str(_CLAUDE_PLUGIN_DIR)])
    argv.extend(["--", prompt])

    run_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "timeout": COMMAND_TIMEOUT_SECONDS,
        "shell": False,
        "check": False,
    }
    if environment is not None:
        run_kwargs["env"] = environment
    try:
        completed = subprocess.run(argv, **run_kwargs)
    except subprocess.TimeoutExpired:
        if managed_session is not None:
            bridge.remove_session(managed_session)
        return _error(
            "Claude command timed out after 20 seconds",
            **details,
            stdout="",
            stderr="",
        )
    except OSError:
        if managed_session is not None:
            bridge.remove_session(managed_session)
        return _error(
            "Claude command could not be started", **details, stdout="", stderr=""
        )

    payload: dict[str, Any] = {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        **details,
        "stdout": _bounded_output(completed.stdout, prompt),
        "stderr": _bounded_output(completed.stderr, prompt),
    }
    if completed.returncode != 0:
        if managed_session is not None:
            bridge.remove_session(managed_session)
        payload["error"] = "Claude command exited with a non-zero status"
        return _json(payload)

    if managed_session is not None:
        match = _LAUNCH_ID.search(completed.stdout or "")
        if match is None:
            bridge.remove_session(managed_session)
            return _error(
                "Claude managed launch returned no reconcilable session id",
                **details,
                returncode=completed.returncode,
                stdout=payload["stdout"],
                stderr=payload["stderr"],
            )
        native_id = match.group(1)
        bridge.set_native_id(managed_session, native_id)
        payload["id"] = native_id
        payload["managed_permissions"] = True
    return _json(payload)


def _run_claude(argv: list[str]) -> subprocess.CompletedProcess[str] | str:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error("Claude command timed out after 20 seconds", stdout="", stderr="")
    except OSError:
        return _error("Claude command could not be started", stdout="", stderr="")


def _native_sessions(claude_bin: str) -> list[dict[str, Any]] | str:
    completed = _run_claude([claude_bin, "agents", "--json", "--all"])
    if isinstance(completed, str):
        return completed
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
    if not isinstance(sessions, list) or not all(
        isinstance(session, dict) for session in sessions
    ):
        return _list_error(
            "Claude session list must be a top-level JSON list",
            stdout=stdout,
            stderr=stderr,
        )
    return sessions


def claude_session_list(args: object, **kwargs: object) -> str:
    """Return the native Claude CLI's unmodified session rows."""
    del args, kwargs
    try:
        claude_bin = resolve_claude_bin()
        if claude_bin is None:
            return _list_error(
                "Claude CLI is unavailable; set CLAUDE_BIN or install an executable "
                "claude binary",
                stdout="",
                stderr="",
            )
        sessions = _native_sessions(claude_bin)
        if isinstance(sessions, str):
            return sessions
        return _bounded_json(
            {"ok": True, "sessions": sessions},
            "Claude session list is too large to return safely",
        )
    except Exception:
        return _error("Claude session list failed unexpectedly")


def claude_permission_list(args: object, **kwargs: object) -> str:
    """List pending Claude permission requests awaiting Hermes judgment."""
    del args, kwargs
    try:
        return _bounded_json(
            {"ok": True, "requests": bridge.list_pending()},
            "Claude permission requests are too large to return safely",
        )
    except Exception:
        return _error("Claude permission list failed unexpectedly")


def claude_permission_decide(args: object, **kwargs: object) -> str:
    """Resolve one pending managed Claude permission request exactly once."""
    del kwargs
    if not isinstance(args, Mapping):
        return _error("arguments must be an object")
    request_id = args.get("request_id")
    decision = args.get("decision")
    reason = args.get("reason")
    if not isinstance(request_id, str):
        return _error("request_id must be a string")
    if not isinstance(decision, str):
        return _error("decision must be allow or deny")
    if not isinstance(reason, str):
        return _error("reason must be a non-empty string")
    try:
        return _bounded_json(
            bridge.decide(request_id, decision, reason),
            "Claude permission decision response is too large",
        )
    except Exception:
        return _error("Claude permission decision failed unexpectedly")


def _owned_id(args: object) -> tuple[str, str] | str:
    if not isinstance(args, Mapping):
        return _error("arguments must be an object")
    native_id = args.get("id")
    if not isinstance(native_id, str) or not _NATIVE_ID.fullmatch(native_id):
        return _error("id must be eight lowercase hexadecimal characters")
    if bridge.find_session(native_id) is None:
        return _error("session is not managed by Hermes")
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        return _error(
            "Claude CLI is unavailable; set CLAUDE_BIN or install an executable "
            "claude binary"
        )
    return native_id, claude_bin


def claude_session_logs(args: object, **kwargs: object) -> str:
    """Read logs for a Claude session launched under Hermes management."""
    del kwargs
    parsed = _owned_id(args)
    if isinstance(parsed, str):
        return parsed
    native_id, claude_bin = parsed
    completed = _run_claude([claude_bin, "logs", native_id])
    if isinstance(completed, str):
        return completed
    payload = {
        "ok": completed.returncode == 0,
        "id": native_id,
        "returncode": completed.returncode,
        "stdout": _bounded_tail(completed.stdout, LOG_STDOUT_CHARS),
        "stderr": _bounded_tail(completed.stderr, LOG_STDERR_CHARS),
    }
    if completed.returncode != 0:
        payload["error"] = "Claude command exited with a non-zero status"
    return _bounded_log_json(payload)


def claude_session_stop(args: object, **kwargs: object) -> str:
    """Stop and verify one Claude session launched under Hermes management."""
    del kwargs
    parsed = _owned_id(args)
    if isinstance(parsed, str):
        return parsed
    native_id, claude_bin = parsed
    completed = _run_claude([claude_bin, "stop", native_id])
    if isinstance(completed, str):
        return completed
    if completed.returncode != 0:
        return _list_error(
            "Claude command exited with a non-zero status",
            id=native_id,
            returncode=completed.returncode,
            stdout=_bounded_output(completed.stdout),
            stderr=_bounded_output(completed.stderr),
        )

    terminal_states = {"completed", "done", "failed", "stopped", "terminated"}
    for _ in range(10):
        sessions = _native_sessions(claude_bin)
        if isinstance(sessions, str):
            return sessions
        matching = [session for session in sessions if session.get("id") == native_id]
        if not matching or all(
            session.get("state") in terminal_states for session in matching
        ):
            return _json({"ok": True, "id": native_id, "confirmed": True})
        time.sleep(0.2)
    return _error("Claude session stop could not be confirmed", id=native_id)
