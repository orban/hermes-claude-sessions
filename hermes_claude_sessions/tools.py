"""Shell-free handlers for Hermes-managed native Claude sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bridge, events, lifecycle
from .safety import without_prompt

ALLOWED_PERMISSION_MODES = ("auto", "manual", "acceptEdits", "plan")
ALLOWED_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_PERMISSION_MODE = "auto"
COMMAND_TIMEOUT_SECONDS = 20
DEFAULT_WAIT_SECONDS = 600.0
MAX_WAIT_SECONDS = 600.0
MAX_OUTPUT_CHARS = 8_192
DEFAULT_SESSION_LIST_LIMIT = 20
MAX_SESSION_LIST_LIMIT = 100
LOG_STDOUT_CHARS = 5_500
LOG_STDERR_CHARS = 1_000
MAX_PERMISSION_TOOL_INPUT_CHARS = 1_500
MAX_PERMISSION_FIELD_CHARS = 128
_TRUNCATION_MARKER = "\n[truncated]"
_LEADING_TRUNCATION_MARKER = "[truncated]\n"
_LAUNCH_LINE = re.compile(
    r"^backgrounded\s+·\s+([0-9a-f]{8})\s+·\s+([^\r\n]+?)\s*$",
    re.MULTILINE,
)
_NATIVE_ID = re.compile(r"^[0-9a-f]{8}$")
_CLAUDE_PLUGIN_DIR = Path(__file__).resolve().parent / "claude_plugin"


@dataclass(frozen=True, slots=True)
class StartOptions:
    name: str
    prompt: str
    cwd: Path
    permission_mode: str
    model: str | None
    effort: str | None


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _desktop_version_key(path: Path, root: Path) -> tuple[int, ...]:
    version = path.relative_to(root).parts[0]
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def _claude_desktop_fallbacks() -> list[Path]:
    root = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
    candidates = root.glob("*/claude.app/Contents/MacOS/claude")
    return sorted(
        (candidate for candidate in candidates if _is_executable(candidate)),
        key=lambda candidate: _desktop_version_key(candidate, root),
        reverse=True,
    )


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

    fallbacks = [
        Path.home() / ".local" / "bin" / "claude",
        *_claude_desktop_fallbacks(),
    ]
    for fallback in fallbacks:
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


def _parse_launch_id(stdout: str | None, launch_name: str) -> str | None:
    matches = [
        match.group(1)
        for match in _LAUNCH_LINE.finditer(stdout or "")
        if match.group(2) == launch_name
    ]
    return matches[0] if len(matches) == 1 else None


def _parse_start_args(args: object) -> StartOptions | str:
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

    model: str | None = None
    if "model" in args:
        candidate_model = args.get("model")
        if not isinstance(candidate_model, str) or not candidate_model.strip():
            return _error("model must be a non-empty string")
        model = candidate_model

    effort: str | None = None
    if "effort" in args:
        candidate_effort = args.get("effort")
        if candidate_effort not in ALLOWED_EFFORT_LEVELS:
            allowed = ", ".join(ALLOWED_EFFORT_LEVELS)
            return _error(f"effort must be one of: {allowed}")
        effort = candidate_effort

    return StartOptions(
        name=name,
        prompt=prompt,
        cwd=path,
        permission_mode=mode,
        model=model,
        effort=effort,
    )


def claude_session_start(args: object, **kwargs: object) -> str:
    """Start one native Claude background session without exposing its prompt."""
    prompt: str | None = None
    try:
        if isinstance(args, Mapping):
            candidate = args.get("prompt")
            if isinstance(candidate, str):
                prompt = candidate
        route = events.capture_route(
            parent_session_id=str(kwargs.get("session_id") or "")
        )
        result = (
            _claude_session_start(args, route=route)
            if route
            else _claude_session_start(args)
        )
    except Exception:
        result = _error("Claude session start failed unexpectedly")
    return _redact_prompt_from_json(result, prompt)


def _claude_session_start(args: object, *, route: dict[str, Any] | None = None) -> str:
    parsed = _parse_start_args(args)
    if isinstance(parsed, str):
        return parsed
    details = {
        "name": parsed.name,
        "cwd": str(parsed.cwd),
        "mode": parsed.permission_mode,
    }
    if parsed.model is not None:
        details["model"] = parsed.model
    if parsed.effort is not None:
        details["effort"] = parsed.effort

    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        return _error(
            "Claude CLI is unavailable; set CLAUDE_BIN or install an executable "
            "claude binary",
            **details,
            stdout="",
            stderr="",
        )

    managed_session = bridge.create_session(
        parsed.name,
        parsed.cwd,
        model=parsed.model,
        effort=parsed.effort,
        route=route,
        event_socket=events.socket_path(),
    )
    metadata = bridge._read_json(managed_session / "metadata.json")
    launch_name = str(metadata.get("launch_name")) if metadata is not None else ""
    if not launch_name:
        bridge.remove_session(managed_session)
        return _error("Claude managed launch name could not be created", **details)
    environment: dict[str, str] | None = None
    argv = [
        claude_bin,
        "--bg",
        "--name",
        launch_name,
        "--permission-mode",
        parsed.permission_mode,
    ]
    if parsed.model is not None:
        argv.extend(["--model", parsed.model])
    if parsed.effort is not None:
        argv.extend(["--effort", parsed.effort])
    if parsed.permission_mode == "auto":
        environment = os.environ.copy()
        environment[bridge.SESSION_DIR_ENV] = str(managed_session)
    session_plugin = bridge.create_hook_plugin(
        managed_session,
        _CLAUDE_PLUGIN_DIR,
        python_executable=sys.executable,
        include_permission_hook=parsed.permission_mode == "auto",
    )
    argv.extend(["--plugin-dir", str(session_plugin)])
    argv.extend(["--", parsed.prompt])

    run_kwargs: dict[str, Any] = {
        "cwd": str(parsed.cwd),
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
        lifecycle.mark_launch_uncertain(managed_session, "launch command timed out")
        return _error(
            "Claude command timed out after 20 seconds",
            **details,
            launch_state="uncertain",
            managed_session=True,
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
        "stdout": _bounded_output(completed.stdout, parsed.prompt),
        "stderr": _bounded_output(completed.stderr, parsed.prompt),
    }
    if completed.returncode != 0:
        lifecycle.mark_launch_uncertain(
            managed_session,
            "launch command exited with a non-zero status",
            returncode=completed.returncode,
        )
        payload["launch_state"] = "uncertain"
        payload["managed_session"] = True
        payload["error"] = "Claude command exited with a non-zero status"
        return _json(payload)

    if managed_session is not None:
        native_id = _parse_launch_id(completed.stdout, launch_name)
        if native_id is None:
            lifecycle.mark_launch_uncertain(
                managed_session,
                "launch output did not contain a native session id",
                returncode=completed.returncode,
            )
            return _error(
                "Claude managed launch returned no reconcilable session id",
                **details,
                returncode=completed.returncode,
                launch_state="uncertain",
                managed_session=True,
                stdout=payload["stdout"],
                stderr=payload["stderr"],
            )
        try:
            bridge.set_native_id(
                managed_session,
                native_id,
                native_state_path=bridge.native_job_state_path(native_id, environment),
            )
        except ValueError:
            lifecycle.mark_launch_uncertain(
                managed_session,
                "native session id binding could not be established",
            )
            return _error(
                "Claude managed launch could not establish exclusive ownership",
                **details,
                returncode=completed.returncode,
                launch_state="uncertain",
                managed_session=True,
                stdout=payload["stdout"],
                stderr=payload["stderr"],
            )
        events.watch_session(managed_session)
        payload["id"] = native_id
        payload["managed_session"] = True
        payload["managed_permissions"] = parsed.permission_mode == "auto"
        payload.update(_wake_fields(route))
    return _json(payload)


WAKE_NONE_HINT = (
    "This turn has no return address (CLI, cron, or kanban worker), so Hermes "
    "cannot be woken when the session finishes. Call claude_session_wait before "
    "ending the turn, or the outcome will only be visible via claude_session_list."
)


def _wake_fields(route: dict[str, Any] | None) -> dict[str, Any]:
    """Tell the caller whether a completion can re-enter this turn's session."""
    if bridge.route_is_deliverable(route):
        return {"wake": "routable"}
    return {"wake": "none", "wake_hint": WAKE_NONE_HINT}


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


def _parse_list_args(args: object) -> tuple[str | None, int] | str:
    if not isinstance(args, Mapping):
        return _error("arguments must be an object")
    unknown = sorted(set(args) - {"id", "limit"})
    if unknown:
        return _error("unknown argument(s): " + ", ".join(unknown))
    native_id = args.get("id")
    if native_id is not None and (
        not isinstance(native_id, str) or not _NATIVE_ID.fullmatch(native_id)
    ):
        return _error("id must be eight lowercase hexadecimal characters")
    limit = args.get("limit", DEFAULT_SESSION_LIST_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int):
        return _error("limit must be an integer")
    if limit < 1 or limit > MAX_SESSION_LIST_LIMIT:
        return _error(f"limit must be between 1 and {MAX_SESSION_LIST_LIMIT}")
    return native_id, limit


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
    """Return bounded native Claude session rows, optionally filtered by exact ID."""
    del kwargs
    try:
        parsed = _parse_list_args(args)
        if isinstance(parsed, str):
            return parsed
        native_id, limit = parsed
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
        if native_id is not None:
            sessions = [
                session for session in sessions if session.get("id") == native_id
            ]
        total = len(sessions)
        selected = sessions[:limit]
        return _bounded_json(
            {
                "ok": True,
                "sessions": selected,
                "total": total,
                "returned": len(selected),
                "has_more": total > len(selected),
            },
            "Claude session list is too large to return safely",
        )
    except Exception:
        return _error("Claude session list failed unexpectedly")


def claude_permission_list(args: object, **kwargs: object) -> str:
    """List pending Claude permission requests awaiting Hermes judgment."""
    del args, kwargs
    try:
        requests = bridge.list_pending()
        summaries: list[dict[str, Any]] = []
        details: list[dict[str, Any]] = []
        for request in requests:
            raw_input = request.get("tool_input")
            try:
                encoded_input = json.dumps(raw_input, ensure_ascii=False)
            except (TypeError, ValueError):
                encoded_input = ""
            summary = {
                "request_id": _permission_field(request.get("request_id"), 64),
                "tool_name": _permission_field(request.get("tool_name")),
                "native_id": _permission_field(request.get("native_id"), 16),
                "name": _permission_field(request.get("name")),
                "created_at": request.get("created_at"),
                "tool_input_omitted": True,
                "tool_input_chars": len(encoded_input),
            }
            if isinstance(raw_input, Mapping):
                summary["tool_input_keys"] = [
                    _permission_field(key, 64) for key in list(raw_input.keys())[:20]
                ]
            detail = dict(summary)
            for key in ("managed_cwd", "cwd", "permission_mode", "model", "effort"):
                if request.get(key) is not None:
                    detail[key] = _permission_field(request.get(key), 256)
            if encoded_input and len(encoded_input) <= MAX_PERMISSION_TOOL_INPUT_CHARS:
                detail["tool_input"] = raw_input
                detail["tool_input_omitted"] = False
            summaries.append(summary)
            details.append(detail)

        payload: dict[str, Any] = {"ok": True, "requests": summaries}
        for index, detail in enumerate(details):
            previous = summaries[index]
            summaries[index] = detail
            if len(_json(payload)) > MAX_OUTPUT_CHARS:
                summaries[index] = previous
        result = _json(payload)
        if len(result) <= MAX_OUTPUT_CHARS:
            return result
        minimal = [
            {
                "request_id": summary["request_id"],
                "tool_name": summary["tool_name"],
                "tool_input_omitted": True,
            }
            for summary in summaries
        ]
        return _json({"ok": True, "requests": minimal, "details_omitted": True})
    except Exception:
        return _error("Claude permission list failed unexpectedly")


def _permission_field(value: object, limit: int = MAX_PERMISSION_FIELD_CHARS) -> object:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return value[: limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


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


def claude_session_wait(args: object, **kwargs: object) -> str:
    """Block once for a semantic event without racing fresh-turn delivery."""
    del kwargs
    if not isinstance(args, Mapping):
        return _error("arguments must be an object")
    native_id = args.get("id")
    if not isinstance(native_id, str) or not _NATIVE_ID.fullmatch(native_id):
        return _error("id must be eight lowercase hexadecimal characters")
    if bridge.find_session(native_id) is None:
        return _error("session is not managed by Hermes")
    timeout = args.get("timeout_seconds", DEFAULT_WAIT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return _error("timeout_seconds must be a number")
    timeout_seconds = float(timeout)
    if timeout_seconds < 1 or timeout_seconds > MAX_WAIT_SECONDS:
        return _error(f"timeout_seconds must be between 1 and {int(MAX_WAIT_SECONDS)}")
    try:
        event = events.wait_for_event(native_id, timeout_seconds=timeout_seconds)
    except Exception:
        return _error("Claude session wait failed unexpectedly")
    if event is None:
        return _json(
            {
                "ok": True,
                "id": native_id,
                "status": "timeout",
                "event": None,
            }
        )
    model_event = {
        key: event[key]
        for key in ("event_id", "kind", "message", "payload", "created_at")
        if key in event
    }
    return _bounded_json(
        {
            "ok": True,
            "id": native_id,
            "status": "event",
            "event": model_event,
        },
        "Claude session event is too large to return safely",
    )


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


def _archived_logs(session: Path | None, native_id: str) -> str | None:
    snapshot = lifecycle.read_snapshot(session) if session is not None else None
    if snapshot is not None and session is not None:
        status = lifecycle.read_status(session)
        retirement = {
            key: status[key]
            for key in ("state", "native_state", "updated_at", "retry_after", "detail")
            if status is not None and key in status
        }
        return _bounded_log_json(
            {
                "ok": snapshot.get("returncode") == 0,
                "id": native_id,
                "returncode": snapshot.get("returncode"),
                "stdout": str(snapshot.get("stdout") or ""),
                "stderr": str(snapshot.get("stderr") or ""),
                "archived": True,
                "retirement": retirement,
            }
        )
    return None


def claude_session_logs(args: object, **kwargs: object) -> str:
    """Read logs for a Claude session launched under Hermes management."""
    del kwargs
    parsed = _owned_id(args)
    if isinstance(parsed, str):
        return parsed
    native_id, claude_bin = parsed
    session = bridge.find_session(native_id)
    archived = _archived_logs(session, native_id)
    if archived is not None:
        return archived
    completed = _run_claude([claude_bin, "logs", native_id])
    if isinstance(completed, str):
        archived = _archived_logs(session, native_id)
        if archived is not None:
            return archived
        return completed
    payload = {
        "ok": completed.returncode == 0,
        "id": native_id,
        "returncode": completed.returncode,
        "stdout": _bounded_tail(completed.stdout, LOG_STDOUT_CHARS),
        "stderr": _bounded_tail(completed.stderr, LOG_STDERR_CHARS),
    }
    if completed.returncode != 0:
        archived = _archived_logs(session, native_id)
        if archived is not None:
            return archived
        payload["error"] = "Claude command exited with a non-zero status"
    return _bounded_log_json(payload)


def _capture_pre_stop_logs(claude_bin: str, native_id: str) -> dict[str, Any] | None:
    completed = _run_claude([claude_bin, "logs", native_id])
    if isinstance(completed, str) or completed.returncode != 0:
        return None
    return {
        "version": 1,
        "id": native_id,
        "returncode": completed.returncode,
        "stdout": _bounded_tail(completed.stdout, LOG_STDOUT_CHARS),
        "stderr": _bounded_tail(completed.stderr, LOG_STDERR_CHARS),
        "captured_at": time.time(),
    }


def _archive_pre_stop_logs(
    session: Path | None,
    snapshot: dict[str, Any] | None,
    native_state: str,
) -> None:
    if (
        session is None
        or snapshot is None
        or lifecycle.read_snapshot(session) is not None
    ):
        return
    lifecycle.write_snapshot(session, {**snapshot, "native_state": native_state})


def claude_session_stop(args: object, **kwargs: object) -> str:
    """Stop and verify one Claude session launched under Hermes management."""
    del kwargs
    parsed = _owned_id(args)
    if isinstance(parsed, str):
        return parsed
    native_id, claude_bin = parsed
    session = bridge.find_session(native_id)
    snapshot = _capture_pre_stop_logs(claude_bin, native_id)
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
        if not matching:
            _archive_pre_stop_logs(session, snapshot, "stopped")
            return _json({"ok": True, "id": native_id, "confirmed": True})
        if all(item.get("state") in terminal_states for item in matching):
            native_state = str(matching[0].get("state") or "stopped")
            _archive_pre_stop_logs(session, snapshot, native_state)
            return _json({"ok": True, "id": native_id, "confirmed": True})
        time.sleep(0.2)
    return _error("Claude session stop could not be confirmed", id=native_id)
