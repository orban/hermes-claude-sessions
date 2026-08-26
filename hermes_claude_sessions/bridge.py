"""Durable, session-scoped state for Hermes-managed Claude permissions."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

SESSION_DIR_ENV = "HERMES_CLAUDE_PERMISSION_DIR"
TIMEOUT_ENV = "HERMES_CLAUDE_PERMISSION_TIMEOUT"
MAX_REQUEST_BYTES = 131_072
MAX_PENDING_REQUESTS = 20
MAX_REASON_CHARS = 2_048
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_NATIVE_ID = re.compile(r"^[0-9a-f]{8}$")


def state_root() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "state" / "claude-sessions" / "managed"


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def create_session(name: str, cwd: Path) -> Path:
    resolved_cwd = cwd.resolve()
    if not resolved_cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    session = root / uuid.uuid4().hex
    session.mkdir(mode=0o700)
    for child in ("requests", "responses", "resolved"):
        (session / child).mkdir(mode=0o700)
    _write_json_atomic(
        session / "metadata.json",
        {
            "version": 1,
            "name": name,
            "cwd": str(resolved_cwd),
            "native_id": None,
            "created_at": time.time(),
        },
    )
    return session


def remove_session(session: Path) -> None:
    try:
        session.resolve().relative_to(state_root().resolve())
    except (OSError, ValueError):
        return
    shutil.rmtree(session, ignore_errors=True)


def set_native_id(session: Path, native_id: str) -> None:
    if not _NATIVE_ID.fullmatch(native_id):
        raise ValueError(
            "native session id must be eight lowercase hexadecimal characters"
        )
    metadata_path = session / "metadata.json"
    metadata = _read_json(metadata_path)
    if metadata is None:
        raise ValueError("managed session metadata is unavailable")
    metadata["native_id"] = native_id
    _write_json_atomic(metadata_path, metadata)


def find_session(native_id: str) -> Path | None:
    if not _NATIVE_ID.fullmatch(native_id):
        return None
    root = state_root()
    if not root.is_dir():
        return None
    for session in sorted(root.iterdir()):
        metadata = _read_json(session / "metadata.json")
        if metadata is not None and metadata.get("native_id") == native_id:
            return session
    return None


def enqueue_request(session: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    metadata = _read_json(session / "metadata.json")
    if metadata is None:
        raise ValueError("managed session metadata is unavailable")
    encoded = json.dumps(hook_input, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("permission request exceeds the managed size limit")
    request = {
        "request_id": uuid.uuid4().hex,
        "created_at": time.time(),
        "name": metadata.get("name"),
        "native_id": metadata.get("native_id"),
        "managed_cwd": metadata.get("cwd"),
        "claude_session_id": hook_input.get("session_id"),
        "cwd": hook_input.get("cwd"),
        "permission_mode": hook_input.get("permission_mode"),
        "tool_name": hook_input.get("tool_name"),
        "tool_input": hook_input.get("tool_input"),
        "permission_suggestions": hook_input.get("permission_suggestions", []),
    }
    _write_json_atomic(session / "requests" / f"{request['request_id']}.json", request)
    return request


def _pending_paths() -> list[tuple[Path, Path]]:
    result: list[tuple[Path, Path]] = []
    root = state_root()
    if not root.is_dir():
        return result
    for session in sorted(root.iterdir()):
        requests = session / "requests"
        if not requests.is_dir():
            continue
        for request_path in sorted(requests.glob("*.json")):
            response = session / "responses" / request_path.name
            if not response.exists():
                result.append((session, request_path))
    return result


def list_pending() -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for _, request_path in _pending_paths()[:MAX_PENDING_REQUESTS]:
        request = _read_json(request_path)
        if request is not None:
            pending.append(request)
    return pending


def wait_for_pending(timeout_seconds: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        pending = list_pending()
        if pending:
            return pending[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def decide(request_id: str, decision: str, reason: str) -> dict[str, Any]:
    if not _REQUEST_ID.fullmatch(request_id):
        return {
            "ok": False,
            "error": "request_id must be 32 lowercase hexadecimal characters",
        }
    if decision not in {"allow", "deny"}:
        return {"ok": False, "error": "decision must be allow or deny"}
    if not isinstance(reason, str) or not reason.strip():
        return {"ok": False, "error": "reason must be a non-empty string"}
    if len(reason) > MAX_REASON_CHARS:
        return {"ok": False, "error": "reason is too long"}

    root = state_root()
    if not root.is_dir():
        return {"ok": False, "error": "permission request is no longer pending"}
    for session in sorted(root.iterdir()):
        request_path = session / "requests" / f"{request_id}.json"
        response_path = session / "responses" / f"{request_id}.json"
        if not request_path.is_file() or response_path.exists():
            continue
        response = {
            "request_id": request_id,
            "decision": decision,
            "reason": reason.strip(),
            "decided_at": time.time(),
        }
        if not _write_json_exclusive(response_path, response):
            break
        return {"ok": True, "request_id": request_id, "decision": decision}
    return {"ok": False, "error": "permission request is no longer pending"}


def read_response(session: Path, request_id: str) -> dict[str, Any] | None:
    return _read_json(session / "responses" / f"{request_id}.json")


def resolve_request(
    session: Path,
    request: dict[str, Any],
    decision: str,
    reason: str,
) -> None:
    request_id = str(request["request_id"])
    _write_json_atomic(
        session / "resolved" / f"{request_id}.json",
        {
            **request,
            "decision": decision,
            "reason": reason,
            "resolved_at": time.time(),
        },
    )
    (session / "requests" / f"{request_id}.json").unlink(missing_ok=True)
    (session / "responses" / f"{request_id}.json").unlink(missing_ok=True)
