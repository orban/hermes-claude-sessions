"""Crash-safe terminal-session retirement for Hermes-managed Claude jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import bridge

COMMAND_TIMEOUT_SECONDS = 20
SNAPSHOT_STDOUT_CHARS = 5_500
SNAPSHOT_STDERR_CHARS = 1_000
RETIREMENT_RETRY_SECONDS = 3_600.0
MAX_RETIREMENT_RETRY_SECONDS = 24 * 60 * 60.0
RETIRED_STATE_RETENTION_SECONDS = 7 * 24 * 60 * 60.0
ORPHANED_REQUEST_GRACE_SECONDS = 660.0
MISSING_NATIVE_GRACE_SECONDS = 120.0
LAUNCH_INTENT_GRACE_SECONDS = 30.0
LAUNCH_RECONCILIATION_WINDOW_SECONDS = 5 * 60.0
CLAIM_STALE_SECONDS = 120.0
_TERMINAL_STATES = {"completed", "done", "failed", "stopped", "terminated"}
_MISSING_SNAPSHOT_REASON = "native session was absent before logs could be archived"
_process_lock = threading.Lock()

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], float]


def snapshot_path(session: Path) -> Path:
    return session / "terminal-snapshot.json"


def status_path(session: Path) -> Path:
    return session / "lifecycle.json"


def read_snapshot(session: Path) -> dict[str, Any] | None:
    return bridge._read_json(snapshot_path(session))


def write_snapshot(session: Path, snapshot: dict[str, Any]) -> None:
    bridge._write_json_atomic(snapshot_path(session), snapshot)


def read_status(session: Path) -> dict[str, Any] | None:
    return bridge._read_json(status_path(session))


def write_status(session: Path, status: dict[str, Any]) -> None:
    bridge._write_json_atomic(status_path(session), status)


def _bounded_tail(value: str | None, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return "[truncated]\n" + text[-(limit - len("[truncated]\n")) :]


def _managed_state(session: Path) -> tuple[str, str] | None:
    metadata = bridge._read_json(session / "metadata.json")
    if metadata is None:
        return None
    native_id = metadata.get("native_id")
    raw_state_path = metadata.get("native_state_path")
    if not isinstance(native_id, str) or not bridge._NATIVE_ID.fullmatch(native_id):
        return None
    if not isinstance(raw_state_path, str) or not raw_state_path:
        state_path = bridge.native_job_state_path(native_id)
        metadata["native_state_path"] = str(state_path)
        bridge._write_json_atomic(session / "metadata.json", metadata)
    else:
        state_path = Path(raw_state_path)
    state_record = bridge._read_json(state_path)
    state = str(state_record.get("state") or "") if state_record else ""
    return native_id, state


def _run(
    run: RunCommand,
    argv: list[str],
) -> subprocess.CompletedProcess[str] | None:
    try:
        return run(
            argv,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _roster_state(
    run: RunCommand, claude_bin: str, native_id: str
) -> tuple[bool | None, str]:
    completed = _run(run, [claude_bin, "agents", "--json", "--all"])
    if completed is None or completed.returncode != 0:
        return None, ""
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(payload, list) or any(
        not isinstance(row, dict) for row in payload
    ):
        return None, ""
    matching = [row for row in payload if row.get("id") == native_id]
    if not matching:
        return False, ""
    if len(matching) != 1:
        return None, ""
    return True, str(matching[0].get("state") or "")


def _roster_contains(run: RunCommand, claude_bin: str, native_id: str) -> bool | None:
    present, _state = _roster_state(run, claude_bin, native_id)
    return present


def mark_launch_uncertain(
    session: Path,
    detail: str,
    *,
    now: float | None = None,
    returncode: int | None = None,
) -> None:
    metadata_path = session / "metadata.json"
    metadata = bridge._read_json(metadata_path)
    if metadata is None:
        raise ValueError("managed session metadata is unavailable")
    metadata["launch_detail"] = detail
    if returncode is not None:
        metadata["launch_returncode"] = returncode
    already_bound = (
        metadata.get("launch_state") == "bound"
        and metadata.get("native_id") is not None
    )
    if not already_bound:
        metadata["launch_state"] = "uncertain"
        metadata["launch_uncertain_at"] = time.time() if now is None else now
    bridge._write_json_atomic(metadata_path, metadata)


def _launch_candidates(
    metadata: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    launch_name = metadata.get("launch_name")
    cwd = metadata.get("cwd")
    created_at = metadata.get("created_at")
    if not isinstance(launch_name, str) or not isinstance(cwd, str):
        return []
    earliest_ms = (
        (float(created_at) - 5.0) * 1000
        if isinstance(created_at, (int, float))
        else None
    )
    candidates = []
    for row in rows:
        native_id = row.get("id")
        native_session_id = row.get("sessionId")
        started_at = row.get("startedAt")
        if not isinstance(native_id, str) or not bridge._NATIVE_ID.fullmatch(native_id):
            continue
        if not isinstance(native_session_id, str):
            continue
        try:
            canonical_session_id = str(uuid.UUID(native_session_id))
        except ValueError:
            continue
        if native_id != canonical_session_id.replace("-", "")[: len(native_id)]:
            continue
        if row.get("name") != launch_name or row.get("cwd") != cwd:
            continue
        if earliest_ms is not None and (
            not isinstance(started_at, (int, float)) or started_at < earliest_ms
        ):
            continue
        candidates.append(row)
    return candidates


def _launch_failure_detail(candidates: list[dict[str, Any]], *, collision: bool) -> str:
    if collision:
        return "native session id is already bound to another managed session"
    if len(candidates) > 1:
        return "multiple matching native sessions"
    return "no matching native session appeared"


def reconcile_uncertain_launches(
    claude_bin: str,
    *,
    run: RunCommand = subprocess.run,
    now: Clock = time.time,
) -> dict[str, int]:
    result = {"observed": 0, "recovered": 0, "ambiguous": 0}
    root = bridge.state_root()
    if not claude_bin or not root.is_dir():
        return result
    current_time = now()
    uncertain: list[tuple[Path, dict[str, Any], bool]] = []
    for session in bridge.managed_session_directories():
        metadata = bridge._read_json(session / "metadata.json")
        if metadata is None or metadata.get("native_id") is not None:
            continue
        launch_state = metadata.get("launch_state")
        created_at = metadata.get("created_at")
        if launch_state == "intent":
            if not isinstance(created_at, (int, float)) or (
                current_time - float(created_at) < LAUNCH_INTENT_GRACE_SECONDS
            ):
                continue
        elif launch_state not in {"uncertain", "ambiguous"}:
            continue
        reference_time = metadata.get("launch_uncertain_at")
        if not isinstance(reference_time, (int, float)):
            reference_time = metadata.get("created_at")
        expired = isinstance(reference_time, (int, float)) and (
            current_time - float(reference_time) >= LAUNCH_RECONCILIATION_WINDOW_SECONDS
        )
        uncertain.append((session, metadata, expired))
    if not uncertain:
        return result
    completed = _run(run, [claude_bin, "agents", "--json", "--all"])
    if completed is None or completed.returncode != 0:
        return result
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError:
        return result
    if not isinstance(payload, list) or any(
        not isinstance(row, dict) for row in payload
    ):
        return result
    rows = payload
    for session, metadata, expired in uncertain:
        result["observed"] += 1
        candidates = _launch_candidates(metadata, rows)
        collision = False
        if len(candidates) == 1:
            native_id = str(candidates[0]["id"])
            try:
                bridge.set_native_id(session, native_id)
            except ValueError:
                collision = True
            else:
                event_id = f"launch-recovered-{session.name}"
                bridge.enqueue_event(
                    session,
                    kind="state_changed",
                    message=(
                        "Managed Claude launch was reconciled after an uncertain "
                        "command result. Inspect its authoritative state and logs."
                    ),
                    event_id=event_id,
                    payload={"native_id": native_id},
                )
                bridge.signal_dispatcher(session)
                result["recovered"] += 1
                continue
        if expired:
            metadata["launch_state"] = "failed"
            metadata["launch_detail"] = _launch_failure_detail(
                candidates, collision=collision
            )
            metadata["launch_failed_at"] = current_time
            metadata["launch_updated_at"] = current_time
            bridge._write_json_atomic(session / "metadata.json", metadata)
            event_id = f"launch-failed-{session.name}"
            bridge.enqueue_event(
                session,
                kind="failed",
                message=(
                    "Managed Claude launch could not be reconciled. Inspect the native "
                    "roster before deciding whether to retry."
                ),
                event_id=event_id,
                payload={},
            )
            bridge.signal_dispatcher(session)
            continue
        if len(candidates) > 1 or collision:
            metadata["launch_state"] = "ambiguous"
            metadata["launch_detail"] = _launch_failure_detail(
                candidates, collision=collision
            )
            metadata["launch_updated_at"] = current_time
            bridge._write_json_atomic(session / "metadata.json", metadata)
            result["ambiguous"] += 1
    return result


@contextmanager
def _claim(session: Path, clock: Clock) -> Iterator[bool]:
    claim = session / "lifecycle-claim"
    acquired = False
    try:
        try:
            claim.mkdir(mode=0o700)
            acquired = True
        except FileExistsError:
            try:
                age = max(0.0, clock() - claim.stat().st_mtime)
            except OSError:
                yield False
                return
            if age < CLAIM_STALE_SECONDS:
                yield False
                return
            shutil.rmtree(claim, ignore_errors=True)
            try:
                claim.mkdir(mode=0o700)
                acquired = True
            except OSError:
                yield False
                return
        if acquired:
            bridge._write_json_atomic(
                claim / "owner.json",
                {"pid": os.getpid(), "claimed_at": clock()},
            )
        yield acquired
    finally:
        if acquired:
            shutil.rmtree(claim, ignore_errors=True)


def _retired_status(session: Path, native_state: str, now: float) -> dict[str, Any]:
    previous = read_status(session) or {}
    status = {
        "version": 1,
        "state": "retired",
        "native_state": native_state,
        "updated_at": now,
    }
    for key in (
        "native_id",
        "snapshot_sha256",
        "attempt_id",
        "retirement_attempts",
    ):
        if key in previous:
            status[key] = previous[key]
    return status


def _blocked_status(
    session: Path,
    native_state: str,
    now: float,
    detail: str,
) -> dict[str, Any]:
    previous = read_status(session) or {}
    raw_attempts = previous.get("retirement_attempts")
    attempts = int(raw_attempts) if isinstance(raw_attempts, int) else 0
    attempts = min(attempts + 1, 1_000_000)
    exponent = min(attempts - 1, 5)
    delay = min(
        RETIREMENT_RETRY_SECONDS * (2**exponent),
        MAX_RETIREMENT_RETRY_SECONDS,
    )
    status = {
        "version": 1,
        "state": "retirement_blocked",
        "native_state": native_state,
        "updated_at": now,
        "retry_after": now + delay,
        "retirement_attempts": attempts,
        "detail": detail,
    }
    for key in ("native_id", "snapshot_sha256", "attempt_id"):
        if key in previous:
            status[key] = previous[key]
    return status


def _terminal_event_delivered(session: Path) -> bool:
    return _event_delivered(session, f"terminal-{session.name}")


def _event_delivered(session: Path, event_id: str) -> bool:
    """Settled = published to the host, consumed by the tool path, or dead-lettered as
    undeliverable (an unroutable outcome must not pin the session in place forever)."""
    filename = f"{event_id}.json"
    return any(
        (session / "events" / disposition / filename).is_file()
        for disposition in bridge.EVENT_DISPOSITIONS
    )


def _native_record_mtime(session: Path) -> float | None:
    metadata = bridge._read_json(session / "metadata.json")
    if metadata is None:
        return None
    state_path = bridge._metadata_native_state_path(metadata)
    if state_path is None:
        return None
    try:
        return state_path.stat().st_mtime
    except OSError:
        return None


def _snapshot_is_current(session: Path) -> bool:
    """An archived snapshot is trusted only until the native job's state record
    is rewritten after it: a resumed job produces logs the archive does not hold."""
    snapshot = read_snapshot(session)
    if snapshot is None:
        return False
    captured_at = snapshot.get("captured_at")
    record_mtime = _native_record_mtime(session)
    if record_mtime is None or not isinstance(captured_at, (int, float)):
        return True
    return record_mtime <= float(captured_at)


def _capture_snapshot(
    session: Path,
    *,
    native_id: str,
    native_state: str,
    claude_bin: str,
    run: RunCommand,
    now: float,
) -> bool:
    if _snapshot_is_current(session):
        return True
    completed = _run(run, [claude_bin, "logs", native_id])
    if completed is None or completed.returncode != 0:
        return False
    write_snapshot(
        session,
        {
            "version": 1,
            "id": native_id,
            "native_state": native_state,
            "returncode": completed.returncode,
            "stdout": _bounded_tail(completed.stdout, SNAPSHOT_STDOUT_CHARS),
            "stderr": _bounded_tail(completed.stderr, SNAPSHOT_STDERR_CHARS),
            "captured_at": now,
        },
    )
    return True


def _has_inflight_managed_work(session: Path) -> bool:
    requests = session / "requests"
    resolving = session / "resolving"
    claimed = session / "events" / "claimed"
    return (
        any(requests.glob("*.json"))
        or any(resolving.glob("*.json"))
        or any(claimed.glob("claim-*.json"))
    )


def _expire_orphaned_requests(session: Path, now: float) -> int:
    return bridge.expire_orphaned_requests(
        session,
        stale_before=now - ORPHANED_REQUEST_GRACE_SECONDS,
        resolved_at=now,
    )


def _has_prune_blocker(session: Path) -> bool:
    pending = session / "events" / "pending"
    return (
        _has_inflight_managed_work(session)
        or any(pending.glob("*.json"))
        or bridge.has_active_event_waiter(session)
    )


def _snapshot_sha256(session: Path) -> str:
    return hashlib.sha256(snapshot_path(session).read_bytes()).hexdigest()


def _rm_inflight_status(
    session: Path,
    *,
    native_id: str,
    native_state: str,
    now: float,
) -> dict[str, Any]:
    previous = read_status(session) or {}
    attempt_id = (
        previous.get("attempt_id")
        if previous.get("state") == "rm_inflight"
        and previous.get("native_id") == native_id
        else None
    )
    return {
        "version": 1,
        "state": "rm_inflight",
        "native_id": native_id,
        "native_state": native_state,
        "snapshot_sha256": _snapshot_sha256(session),
        "attempt_id": attempt_id or uuid.uuid4().hex,
        "updated_at": now,
        "retirement_attempts": previous.get("retirement_attempts", 0),
    }


def _replay_rm_inflight(
    session: Path,
    *,
    native_id: str,
    native_state: str,
    claude_bin: str,
    run: RunCommand,
    now: float,
) -> str:
    present, roster_state = _roster_state(run, claude_bin, native_id)
    if present is False:
        write_status(session, _retired_status(session, native_state, now))
        return "retired"
    if present is None or roster_state not in _TERMINAL_STATES:
        return "deferred"
    if not _snapshot_is_current(session):
        native_state = roster_state
        if not _capture_snapshot(
            session,
            native_id=native_id,
            native_state=native_state,
            claude_bin=claude_bin,
            run=run,
            now=now,
        ):
            write_status(
                session,
                _blocked_status(
                    session,
                    native_state,
                    now,
                    "terminal logs are not yet available",
                ),
            )
            return "deferred"
    status = read_status(session) or {}
    if status.get("snapshot_sha256") != _snapshot_sha256(session):
        write_status(
            session,
            _rm_inflight_status(
                session,
                native_id=native_id,
                native_state=native_state,
                now=now,
            ),
        )
    removed = _run(run, [claude_bin, "rm", native_id])
    if removed is not None and removed.returncode == 0:
        write_status(session, _retired_status(session, native_state, now))
        return "retired"
    present = _roster_contains(run, claude_bin, native_id)
    if present is False:
        write_status(session, _retired_status(session, native_state, now))
        return "retired"
    detail = "native retirement could not be confirmed"
    if removed is not None:
        detail = (
            _bounded_tail(
                removed.stdout or removed.stderr,
                SNAPSHOT_STDERR_CHARS,
            ).strip()
            or detail
        )
    write_status(session, _blocked_status(session, native_state, now, detail))
    return "protected" if present is True else "deferred"


def reconcile_terminal_sessions(
    claude_bin: str,
    *,
    run: RunCommand = subprocess.run,
    now: Clock = time.time,
) -> dict[str, int]:
    """Replay terminal retirement steps until each managed session is safe."""
    result = {
        "observed": 0,
        "retired": 0,
        "protected": 0,
        "deferred": 0,
        "pruned": 0,
    }
    if not claude_bin:
        return result
    if not _process_lock.acquire(blocking=False):
        result["deferred"] += 1
        return result
    try:
        bridge.sweep_stale_atomic_temps(now=now())
        reconcile_uncertain_launches(claude_bin, run=run, now=now)
        bridge.enqueue_native_terminal_events()
        root = bridge.state_root()
        if not root.is_dir():
            return result
        bridge._sweep_stale_session_temps(root, now=now())
        for session in bridge.managed_session_directories():
            current_time = now()
            status = read_status(session)
            if status is not None and status.get("state") == "retired":
                result["observed"] += 1
                _expire_orphaned_requests(session, current_time)
                updated_at = status.get("updated_at")
                retention_elapsed = isinstance(updated_at, (int, float)) and (
                    current_time - float(updated_at) >= RETIRED_STATE_RETENTION_SECONDS
                )
                if (
                    retention_elapsed
                    and _terminal_event_delivered(session)
                    and not _has_prune_blocker(session)
                ):
                    bridge.remove_session(session)
                    result["pruned"] += 1
                else:
                    result["deferred"] += 1
                continue
            metadata = bridge._read_json(session / "metadata.json")
            if metadata is not None and metadata.get("launch_state") == "failed":
                result["observed"] += 1
                _expire_orphaned_requests(session, current_time)
                failed_at = metadata.get("launch_failed_at")
                retention_elapsed = isinstance(failed_at, (int, float)) and (
                    current_time - float(failed_at) >= RETIRED_STATE_RETENTION_SECONDS
                )
                if (
                    retention_elapsed
                    and _event_delivered(session, f"launch-failed-{session.name}")
                    and not _has_prune_blocker(session)
                ):
                    bridge.remove_session(session)
                    result["pruned"] += 1
                else:
                    result["deferred"] += 1
                continue
            if metadata is None or metadata.get("launch_state") != "bound":
                continue
            managed = _managed_state(session)
            if status is not None and status.get("state") == "rm_inflight":
                native_id = status.get("native_id")
                native_state = str(status.get("native_state") or "")
                if not isinstance(native_id, str) or not bridge._NATIVE_ID.fullmatch(
                    native_id
                ):
                    continue
                result["observed"] += 1
                with _claim(session, now) as acquired:
                    if not acquired:
                        result["deferred"] += 1
                        continue
                    _expire_orphaned_requests(session, current_time)
                    if _has_inflight_managed_work(session):
                        result["deferred"] += 1
                        continue
                    outcome = _replay_rm_inflight(
                        session,
                        native_id=native_id,
                        native_state=native_state,
                        claude_bin=claude_bin,
                        run=run,
                        now=current_time,
                    )
                    result[outcome] += 1
                continue
            if managed is None:
                continue
            native_id, _state_file_hint = managed
            present, roster_state = _roster_state(run, claude_bin, native_id)
            if present is None:
                continue
            native_missing = False
            if present:
                if roster_state not in _TERMINAL_STATES:
                    continue
                native_state = roster_state
            else:
                bound_at = metadata.get("bound_at")
                if not isinstance(bound_at, (int, float)) or (
                    current_time - float(bound_at) < MISSING_NATIVE_GRACE_SECONDS
                ):
                    continue
                native_state = "missing"
                native_missing = True
            bridge.enqueue_terminal_event(session, native_state)
            result["observed"] += 1
            _expire_orphaned_requests(session, current_time)
            retry_after = status.get("retry_after") if status else None
            if isinstance(retry_after, (int, float)) and current_time < retry_after:
                result["deferred"] += 1
                continue
            with _claim(session, now) as acquired:
                if not acquired:
                    result["deferred"] += 1
                    continue
                if _has_inflight_managed_work(session):
                    result["deferred"] += 1
                    continue
                if not _terminal_event_delivered(session):
                    result["deferred"] += 1
                    continue
                if native_missing:
                    if read_snapshot(session) is None:
                        write_snapshot(
                            session,
                            {
                                "version": 1,
                                "id": native_id,
                                "native_state": native_state,
                                "returncode": 1,
                                "stdout": "",
                                "stderr": _MISSING_SNAPSHOT_REASON,
                                "captured_at": current_time,
                            },
                        )
                    write_status(
                        session,
                        _rm_inflight_status(
                            session,
                            native_id=native_id,
                            native_state=native_state,
                            now=current_time,
                        ),
                    )
                    write_status(
                        session,
                        _retired_status(session, native_state, current_time),
                    )
                    result["retired"] += 1
                    continue
                if not _capture_snapshot(
                    session,
                    native_id=native_id,
                    native_state=native_state,
                    claude_bin=claude_bin,
                    run=run,
                    now=current_time,
                ):
                    present = _roster_contains(run, claude_bin, native_id)
                    if present is False:
                        if read_snapshot(session) is None:
                            write_snapshot(
                                session,
                                {
                                    "version": 1,
                                    "id": native_id,
                                    "native_state": native_state,
                                    "returncode": 1,
                                    "stdout": "",
                                    "stderr": (
                                        "native session disappeared before logs "
                                        "were archived"
                                    ),
                                    "captured_at": current_time,
                                },
                            )
                        write_status(
                            session,
                            _rm_inflight_status(
                                session,
                                native_id=native_id,
                                native_state=native_state,
                                now=current_time,
                            ),
                        )
                        write_status(
                            session,
                            _retired_status(session, native_state, current_time),
                        )
                        result["retired"] += 1
                        continue
                    write_status(
                        session,
                        _blocked_status(
                            session,
                            native_state,
                            current_time,
                            "terminal logs are not yet available",
                        ),
                    )
                    result["deferred"] += 1
                    continue
                write_status(
                    session,
                    _rm_inflight_status(
                        session,
                        native_id=native_id,
                        native_state=native_state,
                        now=current_time,
                    ),
                )
                removed = _run(run, [claude_bin, "rm", native_id])
                if removed is not None and removed.returncode == 0:
                    write_status(
                        session,
                        _retired_status(session, native_state, current_time),
                    )
                    result["retired"] += 1
                    continue
                present = _roster_contains(run, claude_bin, native_id)
                if present is False:
                    write_status(
                        session,
                        _retired_status(session, native_state, current_time),
                    )
                    result["retired"] += 1
                    continue
                detail = "native retirement could not be confirmed"
                if removed is not None:
                    detail = (
                        _bounded_tail(
                            removed.stdout or removed.stderr,
                            SNAPSHOT_STDERR_CHARS,
                        ).strip()
                        or detail
                    )
                write_status(
                    session,
                    _blocked_status(session, native_state, current_time, detail),
                )
                if present is True:
                    result["protected"] += 1
                else:
                    result["deferred"] += 1
        return result
    finally:
        _process_lock.release()
