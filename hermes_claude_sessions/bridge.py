"""Durable, session-scoped state for Hermes-managed Claude permissions."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_DIR_ENV = "HERMES_CLAUDE_PERMISSION_DIR"
TIMEOUT_ENV = "HERMES_CLAUDE_PERMISSION_TIMEOUT"
MAX_REQUEST_BYTES = 131_072
MAX_PENDING_REQUESTS = 20
MAX_REASON_CHARS = 2_048
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_NATIVE_ID = re.compile(r"^[0-9a-f]{8}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_EVENT_MESSAGE_CHARS = 4_096
MAX_EVENT_PAYLOAD_BYTES = 16_384
MAX_PENDING_EVENTS = 1_000
EVENT_CLAIM_STALE_SECONDS = 60.0
EVENT_WAITER_STALE_SECONDS = 660.0
EVENT_SOCKET_ENV = "HERMES_CLAUDE_EVENT_SOCKET"


@dataclass(frozen=True, slots=True)
class EventClaim:
    """One atomically claimed spool event owned by a single consumer."""

    session: Path
    event_id: str
    consumer: str
    path: Path
    event: dict[str, Any]


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


def create_session(
    name: str,
    cwd: Path,
    *,
    model: str | None = None,
    effort: str | None = None,
    route: dict[str, Any] | None = None,
    event_socket: str = "",
) -> Path:
    resolved_cwd = cwd.resolve()
    if not resolved_cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    root = state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    session = root / uuid.uuid4().hex
    session.mkdir(mode=0o700)
    for child in (
        "requests",
        "responses",
        "resolved",
        "events/pending",
        "events/claimed",
        "events/waiters",
        "events/published",
        "events/consumed",
    ):
        (session / child).mkdir(parents=True, mode=0o700)
    _write_json_atomic(
        session / "metadata.json",
        {
            "version": 2,
            "name": name,
            "cwd": str(resolved_cwd),
            "model": model,
            "effort": effort,
            "native_id": None,
            "route": dict(route or {}),
            "event_socket": event_socket,
            "created_at": time.time(),
        },
    )
    return session


def create_hook_plugin(
    session: Path,
    source_plugin: Path,
    *,
    python_executable: str,
) -> Path:
    """Materialize launch-specific hooks with an explicit trusted state argument."""
    hooks_template = _read_json(source_plugin / "hooks" / "hooks.json")
    plugin_manifest = _read_json(source_plugin / ".claude-plugin" / "plugin.json")
    if hooks_template is None or plugin_manifest is None:
        raise ValueError("Claude hook plugin template is unavailable")
    hooks = hooks_template.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("Claude hook plugin template is invalid")
    source_hooks = (source_plugin / "hooks").resolve()
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise ValueError("Claude hook plugin template is invalid")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("Claude hook plugin template is invalid")
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise ValueError("Claude hook plugin template is invalid")
            for handler in handlers:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    raise ValueError("Claude hook plugin template is invalid")
                command = str(handler.get("command") or "")
                script_name = Path(command).name
                if script_name not in {
                    "event_bridge.py",
                    "permission_bridge.py",
                }:
                    raise ValueError("Claude hook plugin template is invalid")
                script = source_hooks / script_name
                if not script.is_file():
                    raise ValueError("Claude hook entry point is unavailable")
                handler["command"] = python_executable
                handler["args"] = [str(script), str(session)]
    destination = session / "claude-plugin"
    (destination / ".claude-plugin").mkdir(parents=True, exist_ok=True, mode=0o700)
    (destination / "hooks").mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_json_atomic(destination / ".claude-plugin" / "plugin.json", plugin_manifest)
    _write_json_atomic(destination / "hooks" / "hooks.json", hooks_template)
    return destination


def remove_session(session: Path) -> None:
    try:
        session.resolve().relative_to(state_root().resolve())
    except (OSError, ValueError):
        return
    shutil.rmtree(session, ignore_errors=True)


def native_job_state_path(
    native_id: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = environ if environ is not None else os.environ
    claude_home = Path(values.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return claude_home / "jobs" / native_id / "state.json"


def set_native_id(
    session: Path,
    native_id: str,
    *,
    native_state_path: Path | None = None,
) -> None:
    if not _NATIVE_ID.fullmatch(native_id):
        raise ValueError(
            "native session id must be eight lowercase hexadecimal characters"
        )
    metadata_path = session / "metadata.json"
    metadata = _read_json(metadata_path)
    if metadata is None:
        raise ValueError("managed session metadata is unavailable")
    metadata["native_id"] = native_id
    metadata["native_state_path"] = str(
        native_state_path or native_job_state_path(native_id)
    )
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


def _existing_event(session: Path, event_id: str) -> dict[str, Any] | None:
    for disposition in ("pending", "published", "consumed"):
        event = _read_json(session / "events" / disposition / f"{event_id}.json")
        if event is not None:
            return event
    claimed = session / "events" / "claimed"
    if claimed.is_dir():
        for path in claimed.glob("claim-*.json"):
            event = _read_json(path)
            if event is not None and event.get("event_id") == event_id:
                return event
    return None


def native_state_watch_directories() -> set[Path]:
    """Return existing directories whose vnode changes can signal job updates."""
    directories: set[Path] = set()
    root = state_root()
    if not root.is_dir():
        return directories
    for session in root.iterdir():
        metadata = _read_json(session / "metadata.json")
        raw_path = metadata.get("native_state_path") if metadata else None
        if not isinstance(raw_path, str) or not raw_path:
            continue
        state_path = Path(raw_path)
        job_dir = state_path.parent
        jobs_root = job_dir.parent
        if job_dir.is_dir():
            directories.add(job_dir)
        elif jobs_root.is_dir():
            directories.add(jobs_root)
    return directories


def enqueue_native_terminal_events() -> int:
    """Convert authoritative Claude job-state transitions into semantic events."""
    root = state_root()
    if not root.is_dir():
        return 0
    enqueued = 0
    for session in sorted(path for path in root.iterdir() if path.is_dir()):
        metadata = _read_json(session / "metadata.json")
        raw_path = metadata.get("native_state_path") if metadata else None
        if not isinstance(raw_path, str) or not raw_path:
            continue
        state_record = _read_json(Path(raw_path))
        state = str(state_record.get("state") or "") if state_record else ""
        if state not in {
            "completed",
            "done",
            "failed",
            "stopped",
            "terminated",
        }:
            continue
        event_id = f"terminal-{session.name}"
        if _existing_event(session, event_id) is not None:
            continue
        if state == "failed":
            kind = "failed"
            message = (
                "Managed Claude session failed. Inspect its authoritative logs and "
                "verify any partial artifacts before deciding whether to retry."
            )
        elif state in {"stopped", "terminated"}:
            kind = "session_ended"
            message = (
                "Managed Claude session stopped. Inspect its logs and verify any "
                "produced artifacts."
            )
        else:
            kind = "completed"
            message = (
                "Managed Claude session completed. Read its logs, inspect the "
                "artifacts, and independently verify the work."
            )
        enqueue_event(
            session,
            kind=kind,
            message=message,
            event_id=event_id,
            payload={"native_state": state},
        )
        signal_dispatcher(session)
        enqueued += 1
    return enqueued


def enqueue_event(
    session: Path,
    *,
    kind: str,
    message: str,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one bounded semantic Claude event before signalling Hermes."""
    metadata = _read_json(session / "metadata.json")
    if metadata is None:
        raise ValueError("managed session metadata is unavailable")
    candidate_id = event_id or uuid.uuid4().hex
    if not _EVENT_ID.fullmatch(candidate_id):
        raise ValueError("event_id has an invalid format")
    clean_kind = str(kind).strip()
    clean_message = str(message).strip()
    clean_payload = dict(payload or {})
    if not clean_kind or len(clean_kind) > 64:
        raise ValueError("event kind must contain between 1 and 64 characters")
    if not clean_message or len(clean_message) > MAX_EVENT_MESSAGE_CHARS:
        raise ValueError(
            "event message must contain between 1 and "
            f"{MAX_EVENT_MESSAGE_CHARS} characters"
        )
    encoded_payload = json.dumps(clean_payload, ensure_ascii=False).encode("utf-8")
    if len(encoded_payload) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("event payload exceeds the managed size limit")
    event_path = session / "events" / "pending" / f"{candidate_id}.json"
    existing = _existing_event(session, candidate_id)
    if existing is not None:
        return existing
    event = {
        "version": 1,
        "event_id": candidate_id,
        "kind": clean_kind,
        "message": clean_message,
        "producer_id": metadata.get("native_id") or "",
        "route": dict(metadata.get("route") or {}),
        "payload": clean_payload,
        "created_at": time.time(),
    }
    if not _write_json_exclusive(event_path, event):
        raced = _read_json(event_path)
        if raced is not None:
            return raced
        raise RuntimeError("managed event could not be persisted")
    return event


def list_pending_events() -> list[tuple[Path, dict[str, Any]]]:
    """Return bounded pending event records across managed Claude sessions."""
    pending: list[tuple[Path, dict[str, Any]]] = []
    root = state_root()
    if not root.is_dir():
        return pending
    for session in sorted(root.iterdir()):
        directory = session / "events" / "pending"
        if not directory.is_dir():
            continue
        for event_path in sorted(directory.glob("*.json")):
            event = _read_json(event_path)
            if event is not None:
                pending.append((session, event))
            if len(pending) >= MAX_PENDING_EVENTS:
                return pending
    return pending


def claim_event(session: Path, event_id: str, *, consumer: str) -> EventClaim | None:
    """Atomically claim one pending event for exactly one delivery path."""
    if not _EVENT_ID.fullmatch(event_id) or not consumer:
        return None
    source = session / "events" / "pending" / f"{event_id}.json"
    claimed_dir = session / "events" / "claimed"
    claimed_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = claimed_dir / f"claim-{os.getpid()}-{uuid.uuid4().hex}.json"
    try:
        os.replace(source, destination)
    except OSError:
        return None
    event = _read_json(destination)
    if event is None:
        destination.unlink(missing_ok=True)
        return None
    return EventClaim(
        session=session,
        event_id=event_id,
        consumer=consumer,
        path=destination,
        event=event,
    )


def claim_next_event(session: Path, *, consumer: str) -> EventClaim | None:
    """Claim the oldest pending event from one managed session."""
    pending_dir = session / "events" / "pending"
    if not pending_dir.is_dir():
        return None
    candidates: list[tuple[float, str, Path]] = []
    for path in pending_dir.glob("*.json"):
        event = _read_json(path)
        created_at = event.get("created_at") if event is not None else None
        order = float(created_at) if isinstance(created_at, (int, float)) else 0.0
        candidates.append((order, path.name, path))
    for _created_at, _name, path in sorted(candidates):
        claim = claim_event(session, path.stem, consumer=consumer)
        if claim is not None:
            return claim
    return None


def complete_event_claim(claim: EventClaim, *, disposition: str) -> bool:
    """Archive a claimed event as either host-published or tool-consumed."""
    if disposition not in {"published", "consumed"}:
        raise ValueError("event disposition must be published or consumed")
    destination = claim.session / "events" / disposition / f"{claim.event_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_file():
        claim.path.unlink(missing_ok=True)
        return True
    try:
        os.replace(claim.path, destination)
    except OSError:
        return False
    return True


def release_event_claim(claim: EventClaim) -> bool:
    """Return a failed delivery claim to the pending spool for retry."""
    pending = claim.session / "events" / "pending" / f"{claim.event_id}.json"
    if not claim.path.is_file():
        return False
    if pending.is_file():
        claim.path.unlink(missing_ok=True)
        return True
    try:
        os.replace(claim.path, pending)
    except OSError:
        return False
    return True


def _claim_owner_pid(path: Path) -> int | None:
    parts = path.stem.split("-", 2)
    if len(parts) != 3 or parts[0] != "claim":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def recover_abandoned_event_claims() -> int:
    """Return claims from dead or stale consumers to the pending spool."""
    recovered = 0
    now = time.time()
    root = state_root()
    if not root.is_dir():
        return 0
    for session in sorted(path for path in root.iterdir() if path.is_dir()):
        claimed_dir = session / "events" / "claimed"
        if not claimed_dir.is_dir():
            continue
        for path in sorted(claimed_dir.glob("claim-*.json")):
            owner_pid = _claim_owner_pid(path)
            try:
                age = max(0.0, now - path.stat().st_mtime)
            except OSError:
                continue
            if (
                owner_pid is not None
                and _pid_is_alive(owner_pid)
                and age < EVENT_CLAIM_STALE_SECONDS
            ):
                continue
            event = _read_json(path)
            if event is None:
                path.unlink(missing_ok=True)
                continue
            event_id = str(event.get("event_id") or "")
            if not _EVENT_ID.fullmatch(event_id):
                path.unlink(missing_ok=True)
                continue
            claim = EventClaim(
                session=session,
                event_id=event_id,
                consumer="recovery",
                path=path,
                event=event,
            )
            if release_event_claim(claim):
                recovered += 1
    return recovered


def register_event_waiter(session: Path) -> Path:
    """Create a cross-process marker before a blocking tool wait begins."""
    waiters = session / "events" / "waiters"
    waiters.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = waiters / f"wait-{os.getpid()}-{uuid.uuid4().hex}.json"
    if not _write_json_exclusive(
        marker,
        {"pid": os.getpid(), "created_at": time.time()},
    ):
        raise OSError("event waiter marker could not be persisted")
    return marker


def unregister_event_waiter(marker: Path) -> None:
    marker.unlink(missing_ok=True)


def has_active_event_waiter(session: Path) -> bool:
    """Return whether any live process has reserved the tool-wait path."""
    waiters = session / "events" / "waiters"
    if not waiters.is_dir():
        return False
    now = time.time()
    active = False
    for marker in sorted(waiters.glob("wait-*.json")):
        record = _read_json(marker)
        pid = record.get("pid") if record is not None else None
        created_at = record.get("created_at") if record is not None else None
        age = (
            max(0.0, now - float(created_at))
            if isinstance(created_at, (int, float))
            else EVENT_WAITER_STALE_SECONDS
        )
        if (
            isinstance(pid, int)
            and _pid_is_alive(pid)
            and age < EVENT_WAITER_STALE_SECONDS
        ):
            active = True
            continue
        marker.unlink(missing_ok=True)
    return active


def mark_event_published(session: Path, event_id: str) -> bool:
    """Move a spool record to the published audit directory atomically."""
    if not _EVENT_ID.fullmatch(event_id):
        return False
    published = session / "events" / "published" / f"{event_id}.json"
    if published.is_file():
        return True
    claim = claim_event(session, event_id, consumer="publisher")
    if claim is None:
        return False
    if complete_event_claim(claim, disposition="published"):
        return True
    release_event_claim(claim)
    return False


def signal_dispatcher(session: Path) -> bool:
    """Send a best-effort low-latency hint after the durable event write."""
    metadata = _read_json(session / "metadata.json")
    socket_path = str((metadata or {}).get("event_socket") or "")
    if not socket_path or not hasattr(socket, "AF_UNIX"):
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.settimeout(0.2)
        client.sendto(b"event", socket_path)
    except OSError:
        return False
    finally:
        client.close()
    return True


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
        "model": metadata.get("model"),
        "effort": metadata.get("effort"),
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
