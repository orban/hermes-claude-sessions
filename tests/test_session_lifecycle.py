from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_claude_sessions import bridge, lifecycle


def _managed_terminal_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str = "done",
) -> Path:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("lifecycle", tmp_path)
    state_path = tmp_path / "jobs" / "a1b2c3d4" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"state": state}), encoding="utf-8")
    bridge.set_native_id(session, "a1b2c3d4", native_state_path=state_path)
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (True, state),
    )
    return session


def _native_state_path(session: Path) -> Path:
    metadata = json.loads((session / "metadata.json").read_text())
    return Path(metadata["native_state_path"])


def _age_native_record(session: Path, mtime: float) -> None:
    """Pin the native state record's mtime so a fake clock can order it against
    a snapshot's captured_at."""
    os.utime(_native_state_path(session), (mtime, mtime))


def _publish_terminal_event(session: Path) -> None:
    managed = lifecycle._managed_state(session)
    assert managed is not None
    assert bridge.enqueue_terminal_event(session, managed[1]) is True
    claim = bridge.claim_next_event(session, consumer="dispatcher")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="published") is True


def test_terminal_reconcile_persists_event_and_logs_before_native_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    _publish_terminal_event(session)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        if argv[1] == "logs":
            assert not lifecycle.snapshot_path(session).exists()
            return subprocess.CompletedProcess(argv, 0, "final output", "")
        assert argv[1] == "rm"
        assert lifecycle.snapshot_path(session).is_file()
        status = lifecycle.read_status(session)
        assert status is not None
        assert status["state"] == "rm_inflight"
        assert status["native_id"] == "a1b2c3d4"
        assert status["snapshot_sha256"]
        assert status["attempt_id"]
        published = bridge._read_json(
            session / "events" / "published" / f"terminal-{session.name}.json"
        )
        assert published is not None
        assert published["kind"] == "completed"
        return subprocess.CompletedProcess(argv, 0, "removed a1b2c3d4", "")

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result == {
        "observed": 1,
        "retired": 1,
        "protected": 0,
        "deferred": 0,
        "pruned": 0,
    }
    assert calls == [
        ["/mock/claude", "logs", "a1b2c3d4"],
        ["/mock/claude", "rm", "a1b2c3d4"],
    ]
    assert lifecycle.read_snapshot(session) == {
        "version": 1,
        "id": "a1b2c3d4",
        "native_state": "done",
        "returncode": 0,
        "stdout": "final output",
        "stderr": "",
        "captured_at": 1000.0,
    }
    assert lifecycle.read_status(session)["state"] == "retired"


def test_reconcile_recovers_after_native_removal_before_status_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path, "failed")
    _publish_terminal_event(session)
    lifecycle.write_snapshot(
        session,
        {
            "version": 1,
            "id": "a1b2c3d4",
            "native_state": "failed",
            "returncode": 0,
            "stdout": "archived failure",
            "stderr": "",
            "captured_at": 900.0,
        },
    )
    _age_native_record(session, 800.0)
    calls: list[list[str]] = []
    roster = iter(((True, "failed"), (False, "")))
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: next(roster),
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        if argv[1] == "rm":
            return subprocess.CompletedProcess(argv, 1, "session not found", "")
        raise AssertionError(argv)

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result["retired"] == 1
    assert calls == [["/mock/claude", "rm", "a1b2c3d4"]]
    assert lifecycle.read_status(session)["state"] == "retired"


def test_reconcile_records_worktree_protection_without_losing_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path, "stopped")
    _publish_terminal_event(session)

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "stopped output", "")
        if argv[1] == "rm":
            return subprocess.CompletedProcess(
                argv,
                1,
                "kept a1b2c3d4 — worktree has uncommitted changes",
                "",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps([{"id": "a1b2c3d4", "state": "stopped"}]),
            "",
        )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result["protected"] == 1
    assert lifecycle.read_snapshot(session)["stdout"] == "stopped output"
    status = lifecycle.read_status(session)
    assert status is not None
    assert {
        key: status[key]
        for key in (
            "version",
            "state",
            "native_state",
            "updated_at",
            "retry_after",
            "retirement_attempts",
            "detail",
            "native_id",
        )
    } == {
        "version": 1,
        "state": "retirement_blocked",
        "native_state": "stopped",
        "updated_at": 1000.0,
        "retry_after": 4600.0,
        "retirement_attempts": 1,
        "detail": "kept a1b2c3d4 — worktree has uncommitted changes",
        "native_id": "a1b2c3d4",
    }
    assert status["attempt_id"]
    assert status["snapshot_sha256"]
    assert bridge.find_session("a1b2c3d4") == session


def test_repeated_retirement_failures_use_capped_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path, "stopped")
    _publish_terminal_event(session)

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "stopped output", "")
        if argv[1] == "rm":
            return subprocess.CompletedProcess(argv, 1, "worktree protected", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps([{"id": "a1b2c3d4", "state": "stopped"}]),
            "",
        )

    first = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )
    second = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 4600.0
    )

    assert first["protected"] == 1
    assert second["protected"] == 1
    status = lifecycle.read_status(session)
    assert status is not None
    assert status["retirement_attempts"] == 2
    assert status["retry_after"] == 11_800.0
    assert status["retry_after"] - status["updated_at"] == (
        2 * lifecycle.RETIREMENT_RETRY_SECONDS
    )


def test_missing_native_job_after_log_failure_is_retired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    _publish_terminal_event(session)
    calls: list[list[str]] = []
    roster = iter(((True, "done"), (False, "")))
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: next(roster),
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 1, "", "session not found")
        raise AssertionError(argv)

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result["retired"] == 1
    assert calls == [["/mock/claude", "logs", "a1b2c3d4"]]
    assert lifecycle.read_status(session)["state"] == "retired"
    assert lifecycle.read_snapshot(session)["stderr"] == (
        "native session disappeared before logs were archived"
    )


def test_bound_session_missing_from_state_and_roster_retires_after_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    metadata = json.loads((session / "metadata.json").read_text())
    Path(metadata["native_state_path"]).unlink()
    metadata["bound_at"] = 0.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (False, ""),
    )
    archived = {
        "version": 1,
        "id": "a1b2c3d4",
        "native_state": "stopped",
        "returncode": 0,
        "stdout": "REAL FINAL LOGS",
        "stderr": "",
        "captured_at": 900.0,
    }
    lifecycle.write_snapshot(session, archived)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        assert argv[1:] == ["agents", "--json", "--all"]
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    first = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert first["observed"] == 1
    assert first["deferred"] == 1
    event = bridge.list_pending_events()[0][1]
    assert event["kind"] == "session_ended"
    assert event["payload"] == {"native_state": "missing"}
    claim = bridge.claim_next_event(session, consumer="wait")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="consumed") is True

    second = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1001.0
    )

    assert second["retired"] == 1
    assert lifecycle.read_status(session)["state"] == "retired"
    assert lifecycle.read_snapshot(session) == archived
    assert calls == []


def test_authoritative_roster_absence_overrides_stale_working_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path, "working")
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["bound_at"] = 0.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (False, ""),
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail(
            "stale state file triggered native I/O"
        ),
        now=lambda: 1000.0,
    )

    assert result["observed"] == 1
    assert result["deferred"] == 1
    event = bridge.list_pending_events()[0][1]
    assert event["payload"] == {"native_state": "missing"}


def test_recently_bound_session_is_not_retired_during_roster_propagation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    metadata = json.loads((session / "metadata.json").read_text())
    Path(metadata["native_state_path"]).unlink()
    metadata["bound_at"] = 950.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (False, ""),
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "[]", ""),
        now=lambda: 1000.0,
    )

    assert result == {
        "observed": 0,
        "retired": 0,
        "protected": 0,
        "deferred": 0,
        "pruned": 0,
    }
    assert bridge.list_pending_events() == []
    assert lifecycle.read_status(session) is None


def test_authoritative_roster_terminal_overrides_stale_working_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path, "working")
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (True, "done"),
    )

    first = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail(
            "retirement ran before terminal event delivery"
        ),
        now=lambda: 1000.0,
    )

    assert first["observed"] == 1
    assert first["deferred"] == 1
    event = bridge.list_pending_events()[0][1]
    assert event["event_id"] == f"terminal-{session.name}"
    assert event["payload"] == {"native_state": "done"}
    claim = bridge.claim_next_event(session, consumer="wait")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="consumed") is True

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "final logs", "")
        assert argv[1] == "rm"
        return subprocess.CompletedProcess(argv, 0, "removed", "")

    second = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1001.0
    )

    assert second["retired"] == 1
    assert lifecycle.read_snapshot(session)["native_state"] == "done"


def test_authoritative_roster_active_overrides_stale_terminal_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path, "done")
    _publish_terminal_event(session)
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (True, "working"),
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail(
            "stale state file caused native retirement"
        ),
        now=lambda: 1000.0,
    )

    assert result["observed"] == 0
    assert lifecycle.read_status(session) is None


def test_reconcile_is_idempotent_after_retirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    lifecycle.write_status(
        session,
        {
            "version": 1,
            "state": "retired",
            "native_state": "done",
            "updated_at": 900.0,
        },
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("retired session was replayed"),
        now=lambda: 1000.0,
    )

    assert result == {
        "observed": 1,
        "retired": 0,
        "protected": 0,
        "deferred": 1,
        "pruned": 0,
    }


def test_recent_retirement_block_is_backed_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    lifecycle.write_status(
        session,
        {
            "version": 1,
            "state": "retirement_blocked",
            "native_state": "done",
            "updated_at": 900.0,
            "retry_after": 1100.0,
            "detail": "protected",
        },
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("backoff was ignored"),
        now=lambda: 1000.0,
    )

    assert result["deferred"] == 1


def test_snapshot_fields_are_bounded_and_untrusted_output_stays_out_of_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    _publish_terminal_event(session)

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "x" * 20_000, "y" * 20_000)
        return subprocess.CompletedProcess(argv, 0, "removed", "")

    lifecycle.reconcile_terminal_sessions("/mock/claude", run=run)

    snapshot = lifecycle.read_snapshot(session)
    assert len(snapshot["stdout"]) <= lifecycle.SNAPSHOT_STDOUT_CHARS
    assert len(snapshot["stderr"]) <= lifecycle.SNAPSHOT_STDERR_CHARS
    event = bridge._read_json(
        session / "events" / "published" / f"terminal-{session.name}.json"
    )
    assert event is not None
    assert "stdout" not in json.dumps(event)
    assert "x" * 100 not in json.dumps(event)


def test_retired_state_is_pruned_only_after_event_delivery_and_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    bridge.enqueue_terminal_event(session, "done")
    lifecycle.write_status(
        session,
        {
            "version": 1,
            "state": "retired",
            "native_state": "done",
            "updated_at": 1000.0,
        },
    )
    after_retention = 1000.0 + lifecycle.RETIRED_STATE_RETENTION_SECONDS + 1

    pending = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("retired session ran native commands"),
        now=lambda: after_retention,
    )

    assert pending["pruned"] == 0
    assert session.exists()

    claim = bridge.claim_next_event(session, consumer="dispatcher")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="published") is True

    delivered = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("retired session ran native commands"),
        now=lambda: after_retention,
    )

    assert delivered["pruned"] == 1
    assert not session.exists()


def test_recent_retired_state_remains_available_for_archived_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    bridge.enqueue_terminal_event(session, "done")
    claim = bridge.claim_next_event(session, consumer="wait")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="consumed") is True
    lifecycle.write_status(
        session,
        {
            "version": 1,
            "state": "retired",
            "native_state": "done",
            "updated_at": 1000.0,
        },
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("retired session ran native commands"),
        now=lambda: 1001.0,
    )

    assert result["pruned"] == 0
    assert session.exists()


@pytest.mark.parametrize("blocker", ["request", "claim"])
def test_retirement_waits_for_inflight_managed_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, blocker: str
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    if blocker == "request":
        _publish_terminal_event(session)
        bridge._write_json_atomic(
            session / "requests" / ("a" * 32 + ".json"),
            {"request_id": "a" * 32, "created_at": 900.0},
        )
    else:
        bridge.enqueue_terminal_event(session, "done")
        assert bridge.claim_next_event(session, consumer="wait") is not None

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("retirement ran while work was active"),
        now=lambda: 1000.0,
    )

    assert result["deferred"] == 1
    assert lifecycle.read_snapshot(session) is None
    assert session.exists()


@pytest.mark.parametrize("with_response", [False, True])
def test_stale_terminal_permission_request_is_tombstoned_before_retirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_response: bool,
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    _publish_terminal_event(session)
    request_id = "a" * 32
    bridge._write_json_atomic(
        session / "requests" / f"{request_id}.json",
        {
            "request_id": request_id,
            "created_at": 100.0,
            "tool_name": "Bash",
        },
    )
    if with_response:
        bridge._write_json_atomic(
            session / "responses" / f"{request_id}.json",
            {
                "request_id": request_id,
                "decision": "allow",
                "reason": "already decided",
                "decided_at": 200.0,
            },
        )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "final output", "")
        assert argv[1] == "rm"
        return subprocess.CompletedProcess(argv, 0, "removed", "")

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result["retired"] == 1
    assert bridge.list_pending() == []
    assert list((session / "requests").glob("*.json")) == []
    assert list((session / "responses").glob("*.json")) == []
    resolved = bridge._read_json(session / "resolved" / f"{request_id}.json")
    assert resolved is not None
    assert resolved["resolution_source"] == "terminal_reconciliation"
    assert resolved["decision"] == ("allow" if with_response else "deny")
    assert resolved["reason"] == (
        "already decided"
        if with_response
        else "Managed Claude session became terminal before permission resolution"
    )


def test_terminal_request_expiration_replays_an_abandoned_resolution_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    request_id = "a" * 32
    source = session / "requests" / f"{request_id}.json"
    bridge._write_json_atomic(
        source,
        {"request_id": request_id, "created_at": 100.0},
    )
    resolving = session / "resolving" / source.name
    resolving.parent.mkdir(mode=0o700, exist_ok=True)
    source.replace(resolving)

    assert (
        bridge.expire_orphaned_requests(
            session,
            stale_before=340.0,
            resolved_at=1000.0,
        )
        == 1
    )
    assert not resolving.exists()
    resolved = bridge._read_json(session / "resolved" / source.name)
    assert resolved is not None
    assert resolved["decision"] == "deny"


def test_retirement_waits_until_terminal_event_is_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    assert bridge.enqueue_terminal_event(session, "done") is True

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail(
            "retirement ran before terminal event delivery"
        ),
        now=lambda: 1000.0,
    )

    assert result["deferred"] == 1
    assert lifecycle.read_snapshot(session) is None
    assert lifecycle.read_status(session) is None


def test_legacy_managed_session_is_never_automatically_retired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    session = bridge.state_root() / ("a" * 32)
    for child in (
        "events/pending",
        "events/claimed",
        "events/published",
        "events/consumed",
    ):
        (session / child).mkdir(parents=True, mode=0o700)
    state_path = tmp_path / "jobs" / "a1b2c3d4" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"state": "done"}), encoding="utf-8")
    bridge._write_json_atomic(
        session / "metadata.json",
        {
            "version": 1,
            "session_uuid": session.name,
            "native_id": "a1b2c3d4",
            "native_state_path": str(state_path),
        },
    )
    assert bridge.enqueue_terminal_event(session, "done") is True
    claim = bridge.claim_next_event(session, consumer="dispatcher")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="published") is True
    assert bridge.find_session("a1b2c3d4") == session
    assert json.loads((session / "metadata.json").read_text())["launch_state"] == (
        "legacy"
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("legacy session was retired"),
        now=lambda: 1000.0,
    )

    assert result["observed"] == 0
    assert lifecycle.read_snapshot(session) is None
    assert lifecycle.read_status(session) is None


def test_rm_inflight_recovery_finishes_when_native_state_was_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    lifecycle.write_snapshot(
        session,
        {
            "version": 1,
            "id": "a1b2c3d4",
            "native_state": "done",
            "returncode": 0,
            "stdout": "archived",
            "stderr": "",
            "captured_at": 900.0,
        },
    )
    lifecycle.write_status(
        session,
        lifecycle._rm_inflight_status(
            session,
            native_id="a1b2c3d4",
            native_state="done",
            now=900.0,
        ),
    )
    metadata = json.loads((session / "metadata.json").read_text())
    Path(metadata["native_state_path"]).unlink()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (False, ""),
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise AssertionError(argv)

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result["retired"] == 1
    assert calls == []
    assert lifecycle.read_status(session)["state"] == "retired"


def test_rm_inflight_recovery_defers_if_native_job_resumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    lifecycle.write_snapshot(
        session,
        {
            "version": 1,
            "id": "a1b2c3d4",
            "native_state": "done",
            "returncode": 0,
            "stdout": "archived",
            "stderr": "",
            "captured_at": 900.0,
        },
    )
    lifecycle.write_status(
        session,
        lifecycle._rm_inflight_status(
            session,
            native_id="a1b2c3d4",
            native_state="done",
            now=900.0,
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: (True, "working"),
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("active native job was removed"),
        now=lambda: 1000.0,
    )

    assert result["observed"] == 1
    assert result["deferred"] == 1
    assert lifecycle.read_status(session)["state"] == "rm_inflight"


def test_retired_state_with_pending_event_is_not_pruned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    bridge.enqueue_terminal_event(session, "done")
    claim = bridge.claim_next_event(session, consumer="dispatcher")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="published") is True
    bridge.enqueue_event(
        session,
        kind="state_changed",
        message="pending diagnostic event",
        event_id="pending-diagnostic",
    )
    lifecycle.write_status(
        session,
        {
            "version": 1,
            "state": "retired",
            "native_state": "done",
            "updated_at": 1000.0,
        },
    )

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("retired session ran native commands"),
        now=lambda: 1000.0 + lifecycle.RETIRED_STATE_RETENTION_SECONDS + 1,
    )

    assert result["pruned"] == 0
    assert session.exists()


def test_uncertain_launch_reconciles_exact_native_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("recover-me", tmp_path)
    lifecycle.mark_launch_uncertain(session, "launch timeout", now=1000.0)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    rows = [
        {
            "id": "a1b2c3d4",
            "name": metadata["launch_name"],
            "cwd": str(tmp_path),
            "sessionId": "a1b2c3d4-0000-4000-8000-000000000000",
            "startedAt": 1000_001,
            "state": "working",
        }
    ]

    result = lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows), ""
        ),
        now=lambda: 1001.0,
    )

    assert result == {"observed": 1, "recovered": 1, "ambiguous": 0}
    assert bridge.find_session("a1b2c3d4") == session
    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "bound"
    event = bridge.list_pending_events()[0][1]
    assert event["kind"] == "state_changed"
    assert event["payload"] == {"native_id": "a1b2c3d4"}


def test_uncertain_launch_never_guesses_between_multiple_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("duplicate", tmp_path)
    lifecycle.mark_launch_uncertain(session, "malformed output", now=1000.0)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    rows = [
        {
            "id": native_id,
            "name": metadata["launch_name"],
            "cwd": str(tmp_path),
            "sessionId": f"{native_id}-0000-4000-8000-000000000000",
            "startedAt": 1000_001,
            "state": "working",
        }
        for native_id in ("a1b2c3d4", "deadbeef")
    ]

    result = lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows), ""
        ),
        now=lambda: 1001.0,
    )

    assert result == {"observed": 1, "recovered": 0, "ambiguous": 1}
    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "ambiguous"
    assert metadata["native_id"] is None


def test_uncertain_launch_does_not_adopt_a_different_conversation_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("same-name", tmp_path)
    lifecycle.mark_launch_uncertain(session, "launch timeout", now=1000.0)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    rows = [
        {
            "id": "a1b2c3d4",
            "name": "same-name",
            "cwd": str(tmp_path),
            "sessionId": "00000000-0000-4000-8000-000000000000",
            "startedAt": 1000_001,
            "state": "working",
        }
    ]

    result = lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows), ""
        ),
        now=lambda: 1001.0,
    )

    assert result == {"observed": 1, "recovered": 0, "ambiguous": 0}
    assert json.loads((session / "metadata.json").read_text())["native_id"] is None


def test_stale_launch_intent_is_reconciled_after_process_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("crash-recovery", tmp_path)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    rows = [
        {
            "id": "a1b2c3d4",
            "name": metadata["launch_name"],
            "cwd": str(tmp_path),
            "sessionId": "a1b2c3d4-0000-4000-8000-000000000000",
            "startedAt": 1000_001,
            "state": "working",
        }
    ]

    result = lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows), ""
        ),
        now=lambda: 1000.0 + lifecycle.LAUNCH_INTENT_GRACE_SECONDS + 1,
    )

    assert result["recovered"] == 1
    assert bridge.find_session("a1b2c3d4") == session


def test_unmatched_launch_stops_polling_after_reconciliation_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("never-appeared", tmp_path)
    lifecycle.mark_launch_uncertain(session, "nonzero launch", now=1000.0)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    after_window = 1000.0 + lifecycle.LAUNCH_RECONCILIATION_WINDOW_SECONDS + 1

    lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "[]", ""),
        now=lambda: after_window,
    )

    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "failed"
    event = bridge.list_pending_events()[0][1]
    assert event["event_id"] == f"launch-failed-{session.name}"
    assert lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("failed launch kept polling"),
        now=lambda: after_window + 1,
    ) == {"observed": 0, "recovered": 0, "ambiguous": 0}


def test_expired_launch_waits_for_an_authoritative_roster_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("roster-unavailable", tmp_path)
    lifecycle.mark_launch_uncertain(session, "launch timeout", now=1000.0)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    after_window = 1000.0 + lifecycle.LAUNCH_RECONCILIATION_WINDOW_SECONDS + 1

    result = lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, "", "roster unavailable"
        ),
        now=lambda: after_window,
    )

    assert result == {"observed": 0, "recovered": 0, "ambiguous": 0}
    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "uncertain"
    assert bridge.list_pending_events() == []


def test_undeliverable_terminal_event_counts_as_settled_for_retirement(
    tmp_path, monkeypatch
):
    from hermes_claude_sessions import bridge, lifecycle

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    session = bridge.create_session(
        "retire-unroutable", tmp_path, route={}, event_socket=""
    )
    assert lifecycle._terminal_event_delivered(session) is False
    event_id = f"terminal-{session.name}"
    bridge.enqueue_event(session, kind="completed", message="done", event_id=event_id)
    claim = bridge.claim_event(session, event_id, consumer="dispatcher")
    assert claim is not None
    assert bridge.dead_letter_event_claim(
        claim, reason="route has no routable session identity"
    )

    assert lifecycle._terminal_event_delivered(session) is True
    assert bridge.list_pending_events() == []


def _blocked_then_unblocked_rm(
    outputs: dict[str, str], rm_results: list[int]
) -> tuple[list[list[str]], object]:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, outputs["logs"], "")
        if argv[1] == "rm":
            returncode = rm_results.pop(0)
            return subprocess.CompletedProcess(
                argv, returncode, "kept a1b2c3d4 — worktree" if returncode else "", ""
            )
        return subprocess.CompletedProcess(
            argv, 0, json.dumps([{"id": "a1b2c3d4", "state": "done"}]), ""
        )

    return calls, run


def test_snapshot_is_recaptured_when_native_record_outlives_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    _publish_terminal_event(session)
    _age_native_record(session, 900.0)
    outputs = {"logs": "run-1 output"}
    calls, run = _blocked_then_unblocked_rm(outputs, [1, 0])

    first = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert first["protected"] == 1
    assert lifecycle.read_snapshot(session)["stdout"] == "run-1 output"
    retry_after = lifecycle.read_status(session)["retry_after"]

    # The user resumed the idle job, it did more work, and its state record was
    # rewritten after the archive was taken.
    _age_native_record(session, retry_after + 10.0)
    outputs["logs"] = "run-2 output"
    calls.clear()

    second = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: retry_after + 100.0
    )

    assert second["retired"] == 1
    assert calls == [
        ["/mock/claude", "logs", "a1b2c3d4"],
        ["/mock/claude", "rm", "a1b2c3d4"],
    ]
    snapshot = lifecycle.read_snapshot(session)
    assert snapshot["stdout"] == "run-2 output"
    assert snapshot["captured_at"] == retry_after + 100.0
    assert lifecycle.read_status(session)["state"] == "retired"


def test_snapshot_is_trusted_while_native_record_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    _publish_terminal_event(session)
    _age_native_record(session, 900.0)
    outputs = {"logs": "run-1 output"}
    calls, run = _blocked_then_unblocked_rm(outputs, [1, 0])

    first = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )
    assert first["protected"] == 1
    retry_after = lifecycle.read_status(session)["retry_after"]
    outputs["logs"] = "must not be requested"
    calls.clear()

    second = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: retry_after + 100.0
    )

    assert second["retired"] == 1
    assert calls == [["/mock/claude", "rm", "a1b2c3d4"]]
    assert lifecycle.read_snapshot(session)["stdout"] == "run-1 output"


def test_rm_inflight_replay_recaptures_stale_snapshot_before_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    lifecycle.write_snapshot(
        session,
        {
            "version": 1,
            "id": "a1b2c3d4",
            "native_state": "done",
            "returncode": 0,
            "stdout": "archived before resume",
            "stderr": "",
            "captured_at": 900.0,
        },
    )
    lifecycle.write_status(
        session,
        lifecycle._rm_inflight_status(
            session, native_id="a1b2c3d4", native_state="done", now=900.0
        ),
    )
    stale_status = lifecycle.read_status(session)
    _age_native_record(session, 950.0)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "resumed output", "")
        assert argv[1] == "rm"
        status = lifecycle.read_status(session)
        assert status["state"] == "rm_inflight"
        assert status["snapshot_sha256"] == lifecycle._snapshot_sha256(session)
        assert status["snapshot_sha256"] != stale_status["snapshot_sha256"]
        assert status["attempt_id"] == stale_status["attempt_id"]
        return subprocess.CompletedProcess(argv, 0, "removed", "")

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result["retired"] == 1
    assert calls == [
        ["/mock/claude", "logs", "a1b2c3d4"],
        ["/mock/claude", "rm", "a1b2c3d4"],
    ]
    assert lifecycle.read_snapshot(session)["stdout"] == "resumed output"
    assert lifecycle.read_status(session)["state"] == "retired"


def test_stale_snapshot_survives_when_native_job_vanished_before_recapture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _managed_terminal_session(monkeypatch, tmp_path)
    _publish_terminal_event(session)
    lifecycle.write_snapshot(
        session,
        {
            "version": 1,
            "id": "a1b2c3d4",
            "native_state": "done",
            "returncode": 0,
            "stdout": "only surviving archive",
            "stderr": "",
            "captured_at": 900.0,
        },
    )
    _age_native_record(session, 950.0)
    roster = iter(((True, "done"), (False, "")))
    monkeypatch.setattr(
        lifecycle,
        "_roster_state",
        lambda _run, _claude_bin, _native_id: next(roster),
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 1, "", "no such session")
        raise AssertionError(argv)

    result = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 1000.0
    )

    assert result["retired"] == 1
    assert lifecycle.read_snapshot(session)["stdout"] == "only surviving archive"


def test_remove_session_stages_directory_out_of_listing_before_deleting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "managed"
    monkeypatch.setattr(bridge, "state_root", lambda: root)
    session = bridge.create_session("half-removed", tmp_path)
    real_rmtree = bridge.shutil.rmtree
    crashed: list[Path] = []

    def interrupted_rmtree(path: object, *args: object, **kwargs: object) -> None:
        if not crashed:
            crashed.append(Path(str(path)))
            return
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(bridge.shutil, "rmtree", interrupted_rmtree)

    bridge.remove_session(session)

    assert not session.exists()
    assert bridge.managed_session_directories() == []
    leftovers = [
        path for path in root.iterdir() if bridge._SESSION_TEMP.fullmatch(path.name)
    ]
    assert leftovers == crashed
    assert leftovers[0].name.startswith(f".{session.name}.")
    assert (leftovers[0] / "metadata.json").is_file()

    swept = lifecycle.reconcile_terminal_sessions(
        "/mock/claude",
        run=lambda *args, **kwargs: pytest.fail("leftover ran native commands"),
        now=lambda: time.time() + bridge.STALE_SESSION_TEMP_SECONDS + 1,
    )

    assert swept["observed"] == 0
    assert not leftovers[0].exists()
    assert list(root.iterdir()) == []


def test_mark_launch_uncertain_keeps_a_binding_the_reconciler_established(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    session = bridge.create_session("slow-launch", tmp_path)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    rows = [
        {
            "id": "a1b2c3d4",
            "name": metadata["launch_name"],
            "cwd": str(tmp_path),
            "sessionId": "a1b2c3d4-0000-4000-8000-000000000000",
            "startedAt": 1000_001,
            "state": "working",
        }
    ]
    recovered = lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows), ""
        ),
        now=lambda: 1000.0 + lifecycle.LAUNCH_INTENT_GRACE_SECONDS + 1,
    )
    assert recovered["recovered"] == 1

    # The stalled tool thread reports its timeout after the reconciler bound it.
    lifecycle.mark_launch_uncertain(
        session, "launch command timed out", now=1032.0, returncode=None
    )

    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "bound"
    assert metadata["native_id"] == "a1b2c3d4"
    assert metadata["launch_detail"] == "launch command timed out"
    assert "launch_uncertain_at" not in metadata
    assert bridge.find_session("a1b2c3d4") == session

    state_path = _native_state_path(session)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"state": "done"}), encoding="utf-8")
    monkeypatch.setattr(
        lifecycle, "_roster_state", lambda _run, _bin, _id: (True, "done")
    )
    # Deliver the launch-recovered wake first so the terminal one is next.
    recovered_claim = bridge.claim_next_event(session, consumer="dispatcher")
    assert recovered_claim is not None
    assert recovered_claim.event_id == f"launch-recovered-{session.name}"
    assert bridge.complete_event_claim(recovered_claim, disposition="published")
    _publish_terminal_event(session)

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert argv[1] in {"logs", "rm"}
        return subprocess.CompletedProcess(argv, 0, "output", "")

    retired = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=run, now=lambda: 2000.0
    )

    assert retired["retired"] == 1
    assert lifecycle.read_status(session)["state"] == "retired"


def test_fresh_foreign_claim_defers_without_disturbing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("claimed", tmp_path)
    claim = session / "lifecycle-claim"
    claim.mkdir()
    (claim / "owner.json").write_text('{"pid": 1}', encoding="utf-8")
    now = time.time()

    with lifecycle._claim(session, lambda: now) as acquired:
        assert acquired is False

    assert claim.is_dir()
    assert (claim / "owner.json").read_text(encoding="utf-8") == '{"pid": 1}'


def test_stale_claim_is_taken_over_and_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("stale-claim", tmp_path)
    claim = session / "lifecycle-claim"
    claim.mkdir()
    (claim / "owner.json").write_text('{"pid": 1}', encoding="utf-8")
    later = time.time() + lifecycle.CLAIM_STALE_SECONDS + 1

    with lifecycle._claim(session, lambda: later) as acquired:
        assert acquired is True
        owner = json.loads((claim / "owner.json").read_text(encoding="utf-8"))
        assert owner == {"pid": os.getpid(), "claimed_at": later}

    assert not claim.exists()


def test_claim_is_released_when_the_guarded_block_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("raising", tmp_path)
    claim = session / "lifecycle-claim"

    with (
        pytest.raises(RuntimeError, match="snapshot exploded"),
        lifecycle._claim(session, time.time) as acquired,
    ):
        assert acquired is True
        assert claim.is_dir()
        raise RuntimeError("snapshot exploded")

    assert not claim.exists()
    with lifecycle._claim(session, time.time) as acquired:
        assert acquired is True


def test_failed_launch_is_pruned_only_after_event_delivery_and_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    session = bridge.create_session("never-launched", tmp_path)
    lifecycle.mark_launch_uncertain(session, "nonzero launch", now=1000.0)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    after_window = 1000.0 + lifecycle.LAUNCH_RECONCILIATION_WINDOW_SECONDS + 1
    lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "[]", ""),
        now=lambda: after_window,
    )
    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "failed"
    failed_at = metadata["launch_failed_at"]
    assert failed_at == after_window
    after_retention = failed_at + lifecycle.RETIRED_STATE_RETENTION_SECONDS + 1
    fail_run = lambda *args, **kwargs: pytest.fail("failed launch ran commands")  # noqa: E731

    undelivered = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=fail_run, now=lambda: after_retention
    )

    assert undelivered["observed"] == 1
    assert undelivered["pruned"] == 0
    assert session.exists()
    pending = bridge.list_pending_events()
    assert [event["event_id"] for _session, event in pending] == [
        f"launch-failed-{session.name}"
    ]

    claim = bridge.claim_next_event(session, consumer="dispatcher")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="published") is True

    retained = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=fail_run, now=lambda: after_retention - 2
    )

    assert retained["pruned"] == 0
    assert session.exists()

    pruned = lifecycle.reconcile_terminal_sessions(
        "/mock/claude", run=fail_run, now=lambda: after_retention
    )

    assert pruned["pruned"] == 1
    assert not session.exists()


def test_uncertain_launch_reports_native_id_ownership_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    owner = bridge.create_session("owner", tmp_path)
    bridge.set_native_id(owner, "a1b2c3d4")
    binding = bridge.native_binding_path("a1b2c3d4")
    binding_before = binding.read_bytes()
    session = bridge.create_session("collide", tmp_path)
    lifecycle.mark_launch_uncertain(session, "launch timeout", now=1000.0)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["created_at"] = 1000.0
    bridge._write_json_atomic(session / "metadata.json", metadata)
    rows = [
        {
            "id": "a1b2c3d4",
            "name": metadata["launch_name"],
            "cwd": str(tmp_path),
            "sessionId": "a1b2c3d4-0000-4000-8000-000000000000",
            "startedAt": 1000_001,
            "state": "working",
        }
    ]
    run = lambda argv, **kwargs: subprocess.CompletedProcess(  # noqa: E731
        argv, 0, json.dumps(rows), ""
    )
    collision_detail = "native session id is already bound to another managed session"

    early = lifecycle.reconcile_uncertain_launches(
        "/mock/claude", run=run, now=lambda: 1001.0
    )

    assert early == {"observed": 1, "recovered": 0, "ambiguous": 1}
    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "ambiguous"
    assert metadata["native_id"] is None
    assert metadata["launch_detail"] == collision_detail
    assert binding.read_bytes() == binding_before
    assert bridge.find_session("a1b2c3d4") == owner

    expired = lifecycle.reconcile_uncertain_launches(
        "/mock/claude",
        run=run,
        now=lambda: 1000.0 + lifecycle.LAUNCH_RECONCILIATION_WINDOW_SECONDS + 1,
    )

    assert expired == {"observed": 1, "recovered": 0, "ambiguous": 0}
    metadata = json.loads((session / "metadata.json").read_text())
    assert metadata["launch_state"] == "failed"
    assert metadata["launch_detail"] == collision_detail
    assert binding.read_bytes() == binding_before
    assert bridge.find_session("a1b2c3d4") == owner


def _bound_working_session(tmp_path: Path, name: str, native_id: str) -> Path:
    session = bridge.create_session(name, tmp_path)
    state_path = tmp_path / "jobs" / native_id / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"state": "working"}), encoding="utf-8")
    bridge.set_native_id(session, native_id, native_state_path=state_path)
    return session


def test_reconcile_pass_fetches_roster_once_for_all_bound_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bridge, "state_root", lambda: tmp_path / "managed")
    native_ids = ["a1b2c3d4", "b2c3d4e5", "c3d4e5f6"]
    for index, native_id in enumerate(native_ids):
        _bound_working_session(tmp_path, f"bound-{index}", native_id)
    rows = [{"id": native_id, "state": "working"} for native_id in native_ids]
    roster_calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["agents", "--json", "--all"]:
            roster_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

    lifecycle.reconcile_terminal_sessions("/mock/claude", run=run, now=lambda: 1.0)
    assert len(roster_calls) == 1

    lifecycle.reconcile_terminal_sessions("/mock/claude", run=run, now=lambda: 2.0)
    assert len(roster_calls) == 2, "memo must not outlive a single pass"


def test_memoized_roster_is_invalidated_by_mutating_commands() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    memoized = lifecycle._memoize_roster(run)
    roster = ["/mock/claude", "agents", "--json", "--all"]
    memoized(roster)
    memoized(roster)
    assert len(calls) == 1
    memoized(["/mock/claude", "rm", "a1b2c3d4"])
    memoized(roster)
    assert [argv[1] for argv in calls] == ["agents", "rm", "agents"]


def test_memoized_roster_does_not_retry_a_failed_listing_within_a_pass() -> None:
    attempts = 0

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        raise subprocess.TimeoutExpired(argv, 20)

    memoized = lifecycle._memoize_roster(run)
    roster = ["/mock/claude", "agents", "--json", "--all"]
    for _ in range(3):
        with pytest.raises(subprocess.TimeoutExpired):
            memoized(roster)
    assert attempts == 1
