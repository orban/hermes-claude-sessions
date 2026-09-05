"""Durable, session-scoped state for Hermes-managed Claude permissions."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
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
_SESSION_UUID = re.compile(r"^[0-9a-f]{32}$")
_NATIVE_ID = re.compile(r"^[0-9a-f]{8}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_EVENT_MESSAGE_CHARS = 4_096
MAX_EVENT_PAYLOAD_BYTES = 16_384
MAX_PENDING_EVENTS = 1_000
EVENT_CLAIM_STALE_SECONDS = 660.0
# A wake publish that keeps failing is retried this many times, then dead-lettered.
MAX_PUBLISH_ATTEMPTS = 5
# Mirrors the Hermes core rule: a wake needs at least one of these to route.
_ROUTABLE_FIELDS = ("session_key", "origin_ui_session_id", "origin_session_id")
EVENT_DISPOSITIONS = ("published", "consumed", "undeliverable")
EVENT_WAITER_STALE_SECONDS = 660.0
EVENT_SOCKET_ENV = "HERMES_CLAUDE_EVENT_SOCKET"
STALE_SESSION_TEMP_SECONDS = 3_600.0
STALE_ATOMIC_TEMP_SECONDS = 3_600.0
_TERMINAL_REQUEST_REASON = (
    "Managed Claude session became terminal before permission resolution"
)
_SESSION_TEMP = re.compile(r"^\.[0-9a-f]{32}\.[0-9a-f]{32}\.tmp$")
_ATOMIC_TEMP = re.compile(r"^\..+\.[0-9a-f]{32}\.tmp$")
_TERMINAL_STATES = {"completed", "done", "failed", "stopped", "terminated"}


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


def managed_session_directories() -> list[Path]:
    """Return only fully published managed-session directories."""
    root = state_root()
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and _SESSION_UUID.fullmatch(path.name)
    )


def _sweep_stale_session_temps(root: Path, *, now: float | None = None) -> int:
    current_time = time.time() if now is None else now
    removed = 0
    for path in root.iterdir():
        if not path.is_dir() or not _SESSION_TEMP.fullmatch(path.name):
            continue
        try:
            age = max(0.0, current_time - path.stat().st_mtime)
        except OSError:
            continue
        if age < STALE_SESSION_TEMP_SECONDS:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed += 1
    return removed


def sweep_stale_atomic_temps(*, now: float | None = None) -> int:
    """Remove only old adapter-owned atomic-write leftovers from known state dirs."""
    current_time = time.time() if now is None else now
    directories = [state_root().parent / "by-native"]
    for session in managed_session_directories():
        directories.extend(
            [
                session,
                session / "requests",
                session / "responses",
                session / "resolving",
                session / "resolved",
                session / "events" / "pending",
                session / "events" / "claimed",
                session / "events" / "waiters",
                session / "events" / "published",
                session / "events" / "consumed",
            ]
        )
    removed = 0
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file() or not _ATOMIC_TEMP.fullmatch(path.name):
                continue
            try:
                age = max(0.0, current_time - path.stat().st_mtime)
            except OSError:
                continue
            if age < STALE_ATOMIC_TEMP_SECONDS:
                continue
            path.unlink(missing_ok=True)
            if not path.exists():
                removed += 1
    return removed


def native_binding_path(native_id: str) -> Path:
    return state_root().parent / "by-native" / f"{native_id}.json"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


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
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def host_role() -> str:
    """Which kind of Hermes process this is: ``gateway`` or ``cli``.

    Recorded in session metadata so that, after the owning process dies, only a
    process of the same kind adopts its spool events (a restarted gateway takes
    over gateway-launched sessions; an interactive CLI never does)."""
    if "gateway.run" in sys.modules or "gateway" in sys.argv:
        return "gateway"
    return "cli"


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
    _sweep_stale_session_temps(root)
    session = root / uuid.uuid4().hex
    temporary = root / f".{session.name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        for child in (
            "requests",
            "responses",
            "resolving",
            "resolved",
            "events/pending",
            "events/claimed",
            "events/waiters",
            "events/published",
            "events/consumed",
        ):
            (temporary / child).mkdir(parents=True, mode=0o700)
        launch_id = uuid.uuid4().hex
        _write_json_atomic(
            temporary / "metadata.json",
            {
                "version": 3,
                "session_uuid": session.name,
                "name": name,
                "cwd": str(resolved_cwd),
                "model": model,
                "effort": effort,
                "native_id": None,
                "launch_state": "intent",
                "launch_id": launch_id,
                "launch_name": f"{name}-{launch_id[:8]}",
                "route": dict(route or {}),
                "event_socket": event_socket,
                "owner": {"pid": os.getpid(), "role": host_role()},
                "created_at": time.time(),
            },
        )
        os.replace(temporary, session)
        _fsync_directory(root)
        return session
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def create_hook_plugin(
    session: Path,
    source_plugin: Path,
    *,
    python_executable: str,
    include_permission_hook: bool = True,
) -> Path:
    """Materialize launch-specific hooks with an explicit trusted state argument."""
    hooks_template = _read_json(source_plugin / "hooks" / "hooks.json")
    plugin_manifest = _read_json(source_plugin / ".claude-plugin" / "plugin.json")
    if hooks_template is None or plugin_manifest is None:
        raise ValueError("Claude hook plugin template is unavailable")
    hooks = hooks_template.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("Claude hook plugin template is invalid")
    if not include_permission_hook:
        hooks.pop("PermissionRequest", None)
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
    metadata = _read_json(session / "metadata.json")
    native_id = metadata.get("native_id") if metadata else None
    if isinstance(native_id, str) and _NATIVE_ID.fullmatch(native_id):
        binding = native_binding_path(native_id)
        record = _read_json(binding)
        if record is not None and record.get("session_uuid") == session.name:
            binding.unlink(missing_ok=True)
            _fsync_directory(binding.parent)
    root = state_root()
    # Rename first so a crash mid-rmtree leaves a `.tmp` sibling (excluded by
    # managed_session_directories and swept later) rather than a half-deleted
    # session that the next tick would treat as real.
    temporary = root / f".{session.name}.{uuid.uuid4().hex}.tmp"
    try:
        os.replace(session, temporary)
    except OSError:
        temporary = session
    _fsync_directory(root)
    shutil.rmtree(temporary, ignore_errors=True)
    _fsync_directory(root)


def native_job_state_path(
    native_id: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    values = environ if environ is not None else os.environ
    claude_home = Path(values.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return claude_home / "jobs" / native_id / "state.json"


def _metadata_native_state_path(metadata: dict[str, Any]) -> Path | None:
    raw_path = metadata.get("native_state_path")
    if isinstance(raw_path, str) and raw_path:
        return Path(raw_path)
    native_id = metadata.get("native_id")
    if isinstance(native_id, str) and _NATIVE_ID.fullmatch(native_id):
        return native_job_state_path(native_id)
    return None


def set_native_id(
    session: Path,
    native_id: str,
    *,
    native_state_path: Path | None = None,
    preserve_launch_state: bool = False,
) -> None:
    if not _NATIVE_ID.fullmatch(native_id):
        raise ValueError(
            "native session id must be eight lowercase hexadecimal characters"
        )
    metadata_path = session / "metadata.json"
    metadata = _read_json(metadata_path)
    if metadata is None:
        raise ValueError("managed session metadata is unavailable")
    current_native_id = metadata.get("native_id")
    if current_native_id not in {None, native_id}:
        raise ValueError("managed session is already bound to another native id")
    binding = native_binding_path(native_id)
    binding.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    claim = {
        "version": 1,
        "native_id": native_id,
        "session_uuid": session.name,
        "launch_id": metadata.get("launch_id"),
        "created_at": time.time(),
    }
    if not _write_json_exclusive(binding, claim):
        existing = _read_json(binding)
        if existing is None or existing.get("session_uuid") != session.name:
            raise ValueError("native session id is already bound")
    metadata["native_id"] = native_id
    metadata["native_state_path"] = str(
        native_state_path or native_job_state_path(native_id)
    )
    if preserve_launch_state:
        metadata["launch_state"] = (
            "bound" if metadata.get("launch_state") == "bound" else "legacy"
        )
    else:
        metadata["launch_state"] = "bound"
        metadata["bound_at"] = time.time()
    _write_json_atomic(metadata_path, metadata)


def find_session(native_id: str) -> Path | None:
    if not _NATIVE_ID.fullmatch(native_id):
        return None
    root = state_root()
    binding = native_binding_path(native_id)
    if binding.exists():
        record = _read_json(binding)
        session_uuid = record.get("session_uuid") if record else None
        if not isinstance(session_uuid, str) or not _SESSION_UUID.fullmatch(
            session_uuid
        ):
            return None
        session = root / session_uuid
        metadata = _read_json(session / "metadata.json")
        if metadata is None or metadata.get("native_id") != native_id:
            return None
        return session
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for session in managed_session_directories():
        metadata = _read_json(session / "metadata.json")
        if metadata is not None and metadata.get("native_id") == native_id:
            candidates.append(session)
    if len(candidates) != 1:
        return None
    session = candidates[0]
    try:
        set_native_id(session, native_id, preserve_launch_state=True)
    except ValueError:
        return None
    return session


def route_is_deliverable(route: Mapping[str, Any] | None) -> bool:
    """Whether the host could ever route a wake for ``route`` (core identity rule)."""
    if not isinstance(route, Mapping):
        return False
    return any(str(route.get(field) or "") for field in _ROUTABLE_FIELDS)


def _existing_event(session: Path, event_id: str) -> dict[str, Any] | None:
    for disposition in ("pending", *EVENT_DISPOSITIONS):
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


def event_exists(session: Path, event_id: str) -> bool:
    """Return whether a durable event exists in any delivery state."""
    return _existing_event(session, event_id) is not None


def native_state_watch_directories() -> set[Path]:
    """Return existing directories whose vnode changes can signal job updates."""
    directories: set[Path] = set()
    root = state_root()
    if not root.is_dir():
        return directories
    for session in managed_session_directories():
        metadata = _read_json(session / "metadata.json")
        state_path = _metadata_native_state_path(metadata) if metadata else None
        if state_path is None:
            continue
        job_dir = state_path.parent
        jobs_root = job_dir.parent
        if job_dir.is_dir():
            directories.add(job_dir)
        elif jobs_root.is_dir():
            directories.add(jobs_root)
    return directories


def enqueue_terminal_event(session: Path, native_state: str) -> bool:
    """Persist one stable terminal event from an authoritative native observation."""
    event_id = f"terminal-{session.name}"
    if _existing_event(session, event_id) is not None:
        return False
    if native_state == "failed":
        kind = "failed"
        message = (
            "Managed Claude session failed. Inspect its authoritative logs and "
            "verify any partial artifacts before deciding whether to retry."
        )
    elif native_state in {"stopped", "terminated", "missing"}:
        kind = "session_ended"
        message = (
            "Managed Claude session ended. Inspect its archived logs and verify any "
            "produced artifacts."
        )
    elif native_state in {"completed", "done"}:
        kind = "completed"
        message = (
            "Managed Claude session completed. Read its logs, inspect the artifacts, "
            "and independently verify the work."
        )
    else:
        return False
    enqueue_event(
        session,
        kind=kind,
        message=message,
        event_id=event_id,
        payload={"native_state": native_state},
    )
    signal_dispatcher(session)
    return True


def enqueue_native_state_events() -> int:
    """Convert state-file hints into nonterminal events and lifecycle wakeups."""
    root = state_root()
    if not root.is_dir():
        return 0
    enqueued = 0
    for session in managed_session_directories():
        metadata = _read_json(session / "metadata.json")
        state_path = _metadata_native_state_path(metadata) if metadata else None
        if state_path is None:
            continue
        try:
            before = state_path.stat().st_mtime_ns
            state_record = _read_json(state_path)
            after = state_path.stat().st_mtime_ns
        except OSError:
            continue
        if before != after:
            continue
        state = str(state_record.get("state") or "") if state_record else ""
        if not state:
            continue
        observation_path = session / "native-observation.json"
        observation = _read_json(observation_path) or {}
        last_state = str(observation.get("state") or "")
        raw_generation = observation.get("blocked_generation")
        blocked_generation = (
            int(raw_generation)
            if isinstance(raw_generation, int) and raw_generation >= 0
            else 0
        )
        if state in _TERMINAL_STATES:
            if last_state != state:
                _write_json_atomic(
                    observation_path,
                    {
                        "version": 1,
                        "state": state,
                        "blocked_generation": blocked_generation,
                        "observed_at": time.time(),
                    },
                )
                signal_dispatcher(session)
            continue
        if state == "blocked":
            if last_state == "blocked":
                continue
            if any((session / "requests").glob("*.json")):
                _write_json_atomic(
                    observation_path,
                    {
                        "version": 1,
                        "state": state,
                        "blocked_generation": blocked_generation,
                        "observed_at": time.time(),
                    },
                )
                continue
            blocked_generation += 1
            event_id = f"blocked-{session.name}-{blocked_generation}"
            kind = "needs_input"
            message = (
                "Managed Claude session is blocked. Inspect its authoritative state "
                "and logs, then provide only task-scoped input."
            )
        else:
            if last_state != state:
                _write_json_atomic(
                    observation_path,
                    {
                        "version": 1,
                        "state": state,
                        "blocked_generation": blocked_generation,
                        "observed_at": time.time(),
                    },
                )
            continue
        if _existing_event(session, event_id) is None:
            enqueue_event(
                session,
                kind=kind,
                message=message,
                event_id=event_id,
                payload={"native_state": state},
            )
            signal_dispatcher(session)
            enqueued += 1
        _write_json_atomic(
            observation_path,
            {
                "version": 1,
                "state": state,
                "blocked_generation": blocked_generation,
                "observed_at": time.time(),
            },
        )
    return enqueued


def enqueue_native_terminal_events() -> int:
    """Compatibility alias; terminal hints are verified by lifecycle reconciliation."""
    return enqueue_native_state_events()


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
    for session in managed_session_directories():
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


def claim_event(
    session: Path, event_id: str, *, consumer: str, source: str = "pending"
) -> EventClaim | None:
    """Atomically claim one spool event for exactly one delivery path.

    ``source`` is ``pending`` by default, or ``undeliverable`` so the tool-wait path
    can still hand a dead-lettered event to its own session."""
    if not _EVENT_ID.fullmatch(event_id) or not consumer:
        return None
    if source not in ("pending", "undeliverable"):
        raise ValueError("event claim source must be pending or undeliverable")
    source = session / "events" / source / f"{event_id}.json"
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


def claim_next_event(
    session: Path, *, consumer: str, source: str = "pending"
) -> EventClaim | None:
    """Claim the oldest event of one managed session from ``source``."""
    pending_dir = session / "events" / source
    if not pending_dir.is_dir():
        return None
    candidates: list[tuple[float, str, Path]] = []
    for path in pending_dir.glob("*.json"):
        event = _read_json(path)
        created_at = event.get("created_at") if event is not None else None
        order = float(created_at) if isinstance(created_at, (int, float)) else 0.0
        candidates.append((order, path.name, path))
    for _created_at, _name, path in sorted(candidates):
        claim = claim_event(session, path.stem, consumer=consumer, source=source)
        if claim is not None:
            return claim
    return None


def complete_event_claim(claim: EventClaim, *, disposition: str) -> bool:
    """Archive a claimed event as host-published, tool-consumed, or undeliverable."""
    if disposition not in EVENT_DISPOSITIONS:
        raise ValueError(
            "event disposition must be published, consumed, or undeliverable"
        )
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


def _rewrite_claim(claim: EventClaim, updates: Mapping[str, Any]) -> bool:
    """Persist ``updates`` into the claimed record across release or archival."""
    if not claim.path.is_file():
        return False
    claim.event.update(updates)
    try:
        _write_json_atomic(claim.path, claim.event)
    except OSError:
        return False
    return True


def note_publish_failure(claim: EventClaim, error: str) -> int:
    """Count one failed wake publish on the claimed record; return the total."""
    attempts = claim.event.get("publish_attempts")
    attempts = (attempts if isinstance(attempts, int) and attempts >= 0 else 0) + 1
    _rewrite_claim(
        claim,
        {
            "publish_attempts": attempts,
            "publish_last_error": str(error)[:500],
            "publish_last_failed_at": time.time(),
        },
    )
    return attempts


def dead_letter_event_claim(claim: EventClaim, *, reason: str) -> bool:
    """Archive a claimed event as ``undeliverable``: the wake will never be published.

    The record stays readable (the tool-wait path may still consume it for its own
    session) and counts as settled for terminal-session retirement, so an unroutable
    terminal event can no longer pin a finished session forever."""
    _rewrite_claim(
        claim,
        {"undeliverable_reason": str(reason)[:500], "undeliverable_at": time.time()},
    )
    return complete_event_claim(claim, disposition="undeliverable")


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
    for session in managed_session_directories():
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
    for session in managed_session_directories():
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
    for session in managed_session_directories():
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
    resolved_path = session / "resolved" / f"{request_id}.json"
    _write_json_exclusive(
        resolved_path,
        {
            **request,
            "decision": decision,
            "reason": reason,
            "resolved_at": time.time(),
            "resolution_source": "permission_hook",
        },
    )
    (session / "requests" / f"{request_id}.json").unlink(missing_ok=True)
    (session / "responses" / f"{request_id}.json").unlink(missing_ok=True)


def expire_orphaned_requests(
    session: Path,
    *,
    stale_before: float,
    resolved_at: float,
) -> int:
    """Atomically tombstone terminal-session requests whose hook waiter is gone."""
    requests = session / "requests"
    resolving = session / "resolving"
    resolving.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidates: list[Path] = []
    if requests.is_dir():
        for request_path in sorted(requests.glob("*.json")):
            request = _read_json(request_path)
            created_at = request.get("created_at") if request is not None else None
            if isinstance(created_at, (int, float)):
                reference_time = float(created_at)
            else:
                try:
                    reference_time = request_path.stat().st_mtime
                except OSError:
                    continue
            if reference_time > stale_before:
                continue
            claim = resolving / request_path.name
            if claim.exists():
                continue
            try:
                os.replace(request_path, claim)
            except OSError:
                continue
            candidates.append(claim)
    candidates.extend(
        path for path in sorted(resolving.glob("*.json")) if path not in candidates
    )

    expired = 0
    for claim in candidates:
        raw_request = _read_json(claim)
        request_id = claim.stem
        if not _REQUEST_ID.fullmatch(request_id):
            continue
        request = dict(raw_request or {"request_id": request_id})
        response_path = session / "responses" / f"{request_id}.json"
        response = _read_json(response_path) or {}
        decision = response.get("decision")
        reason = response.get("reason")
        if decision not in {"allow", "deny"} or not isinstance(reason, str):
            decision = "deny"
            reason = _TERMINAL_REQUEST_REASON
        resolved_path = session / "resolved" / f"{request_id}.json"
        written = _write_json_exclusive(
            resolved_path,
            {
                **request,
                "decision": decision,
                "reason": reason,
                "resolved_at": resolved_at,
                "resolution_source": "terminal_reconciliation",
            },
        )
        if not written and _read_json(resolved_path) is None:
            continue
        claim.unlink(missing_ok=True)
        response_path.unlink(missing_ok=True)
        expired += 1
    return expired
