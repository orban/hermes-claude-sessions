from __future__ import annotations

import io
import json
import os
import select
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from hermes_claude_sessions import bridge, event_hook, events, tools


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _session(tmp_path: Path, *, event_socket: str = "") -> Path:
    return bridge.create_session(
        "event-test",
        tmp_path,
        route={
            "session_key": "agent:main:telegram:dm:123",
            "parent_session_id": "parent-1",
        },
        event_socket=str(event_socket),
    )


def test_enqueue_event_is_durable_and_terminal_events_coalesce(tmp_path):
    session = _session(tmp_path)

    first = bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude finished",
        event_id="terminal-session",
    )
    replay = bridge.enqueue_event(
        session,
        kind="session_ended",
        message="Claude session ended",
        event_id="terminal-session",
    )

    assert replay == first
    assert first["event_id"] == "terminal-session"
    assert first["route"]["parent_session_id"] == "parent-1"
    assert len(bridge.list_pending_events()) == 1

    assert bridge.mark_event_published(session, "terminal-session") is True
    assert bridge.list_pending_events() == []
    assert (session / "events" / "published" / "terminal-session.json").is_file()


def test_signal_dispatcher_uses_launch_owned_unix_socket(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        return
    socket_path = Path("/tmp") / f"hermes-claude-test-{uuid.uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(str(socket_path))
    listener.settimeout(1)
    try:
        session = _session(tmp_path, event_socket=socket_path)
        assert bridge.signal_dispatcher(session) is True
        assert listener.recv(32) == b"event"
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


def test_permission_hook_event_is_published_before_waiting(tmp_path):
    session = _session(tmp_path)
    payload = {
        "hook_event_name": "PermissionRequest",
        "session_id": "claude-session",
        "cwd": str(tmp_path),
        "permission_mode": "auto",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
    }
    stdout = io.StringIO()

    from hermes_claude_sessions import permission_hook

    assert (
        permission_hook.run(
            stdin=io.StringIO(json.dumps(payload)),
            stdout=stdout,
            environ={
                bridge.SESSION_DIR_ENV: str(session),
                bridge.TIMEOUT_ENV: "0.01",
            },
        )
        == 0
    )

    events_found = bridge.list_pending_events()
    assert len(events_found) == 1
    assert events_found[0][1]["kind"] == "permission_required"
    assert "Bash" not in events_found[0][1]["message"]
    decision = json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"


@pytest.mark.parametrize(
    "notification_type", ["permission_prompt", "idle_prompt", "agent_needs_input"]
)
def test_observer_hook_treats_attention_notifications_as_reconciliation_hints(
    monkeypatch, tmp_path, notification_type
):
    session = _session(tmp_path)
    signals = []
    monkeypatch.setattr(bridge, "signal_dispatcher", signals.append)
    payload = {
        "hook_event_name": "Notification",
        "notification_type": notification_type,
        "session_id": "claude-session",
        "message": "SECRET=provider-token",
        "last_assistant_message": "untrusted model output",
    }

    assert (
        event_hook.run(
            stdin=io.StringIO(json.dumps(payload)),
            stdout=io.StringIO(),
            environ={bridge.SESSION_DIR_ENV: str(session)},
        )
        == 0
    )

    assert bridge.list_pending_events() == []
    assert signals == [session]


@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "Notification", "notification_type": "agent_completed"},
        {"hook_event_name": "StopFailure", "error": "SECRET=provider-token"},
        {"hook_event_name": "SessionEnd", "reason": "SECRET=session-end-token"},
    ],
)
def test_terminal_hook_hints_only_signal_authoritative_state_reconciliation(
    monkeypatch, tmp_path, payload
):
    session = _session(tmp_path)
    signals = []
    monkeypatch.setattr(
        bridge,
        "signal_dispatcher",
        lambda value: signals.append(value),
    )

    assert (
        event_hook.run(
            stdin=io.StringIO(json.dumps(payload)),
            stdout=io.StringIO(),
            environ={bridge.SESSION_DIR_ENV: str(session)},
        )
        == 0
    )
    assert bridge.list_pending_events() == []
    assert signals == [session]


def test_executable_event_hook_treats_attention_as_reconciliation_hint(tmp_path):
    session = _session(tmp_path)
    executable = tools._CLAUDE_PLUGIN_DIR / "hooks" / "event_bridge.py"
    completed = subprocess.run(
        [tools.sys.executable, str(executable), str(session)],
        input=json.dumps(
            {
                "hook_event_name": "Notification",
                "notification_type": "idle_prompt",
                "session_id": "claude-session",
                "message": "waiting",
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert bridge.list_pending_events() == []


def test_native_blocked_state_is_reconciled_without_hook_delivery(tmp_path):
    session = _session(tmp_path)
    state_path = tmp_path / "claude" / "jobs" / "a1b2c3d4" / "state.json"
    state_path.parent.mkdir(parents=True)
    bridge.set_native_id(session, "a1b2c3d4", native_state_path=state_path)
    state_path.write_text(json.dumps({"state": "blocked"}), encoding="utf-8")
    os.utime(state_path, ns=(1_000, 1_000))

    assert bridge.enqueue_native_state_events() == 1
    event = bridge.list_pending_events()[0][1]
    assert event["kind"] == "needs_input"
    assert event["payload"] == {"native_state": "blocked"}
    assert event["event_id"] == f"blocked-{session.name}-1"

    claim = bridge.claim_next_event(session, consumer="wait")
    assert claim is not None
    assert bridge.complete_event_claim(claim, disposition="consumed") is True
    assert bridge.enqueue_native_state_events() == 0

    state_path.write_text(
        json.dumps({"state": "blocked", "tokens": 123}), encoding="utf-8"
    )
    os.utime(state_path, ns=(2_000, 2_000))
    assert bridge.enqueue_native_state_events() == 0

    state_path.write_text(json.dumps({"state": "running"}), encoding="utf-8")
    os.utime(state_path, ns=(3_000, 3_000))
    assert bridge.enqueue_native_state_events() == 0
    state_path.write_text(json.dumps({"state": "blocked"}), encoding="utf-8")
    os.utime(state_path, ns=(4_000, 4_000))

    assert bridge.enqueue_native_state_events() == 1
    assert bridge.list_pending_events()[0][1]["event_id"] == (
        f"blocked-{session.name}-2"
    )


def test_attention_hook_and_blocked_state_produce_one_event(tmp_path):
    session = _session(tmp_path)
    state_path = tmp_path / "claude" / "jobs" / "a1b2c3d4" / "state.json"
    state_path.parent.mkdir(parents=True)
    bridge.set_native_id(session, "a1b2c3d4", native_state_path=state_path)
    state_path.write_text(json.dumps({"state": "blocked"}), encoding="utf-8")

    assert (
        event_hook.run(
            stdin=io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "Notification",
                        "notification_type": "permission_prompt",
                    }
                )
            ),
            stdout=io.StringIO(),
            environ={bridge.SESSION_DIR_ENV: str(session)},
        )
        == 0
    )
    assert bridge.list_pending_events() == []
    assert bridge.enqueue_native_state_events() == 1
    pending = bridge.list_pending_events()
    assert len(pending) == 1
    assert pending[0][1]["event_id"] == f"blocked-{session.name}-1"


def test_permission_request_suppresses_duplicate_blocked_state_event(tmp_path):
    session = _session(tmp_path)
    state_path = tmp_path / "claude" / "jobs" / "a1b2c3d4" / "state.json"
    state_path.parent.mkdir(parents=True)
    bridge.set_native_id(session, "a1b2c3d4", native_state_path=state_path)
    request = bridge.enqueue_request(
        session,
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
    )
    bridge.enqueue_event(
        session,
        kind="permission_required",
        message="Managed Claude session is waiting for permission.",
        event_id=f"permission-{request['request_id']}",
        payload={"request_id": request["request_id"]},
    )
    state_path.write_text(json.dumps({"state": "blocked"}), encoding="utf-8")

    assert bridge.enqueue_native_state_events() == 0
    pending = bridge.list_pending_events()
    assert len(pending) == 1
    assert pending[0][1]["kind"] == "permission_required"
    assert pending[0][1]["event_id"] == f"permission-{request['request_id']}"


def test_legacy_managed_session_reconstructs_native_state_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    session = _session(tmp_path)
    metadata = json.loads((session / "metadata.json").read_text())
    metadata["native_id"] = "a1b2c3d4"
    metadata.pop("native_state_path", None)
    bridge._write_json_atomic(session / "metadata.json", metadata)
    state_path = bridge.native_job_state_path("a1b2c3d4")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"state": "done"}), encoding="utf-8")

    assert state_path.parent in bridge.native_state_watch_directories()
    assert bridge.enqueue_native_state_events() == 0
    assert bridge.list_pending_events() == []
    assert bridge._read_json(session / "native-observation.json")["state"] == "done"


def test_native_terminal_state_is_a_stable_reconciliation_hint(tmp_path, monkeypatch):
    session = _session(tmp_path)
    state_path = tmp_path / "claude" / "jobs" / "a1b2c3d4" / "state.json"
    state_path.parent.mkdir(parents=True)
    bridge.set_native_id(session, "a1b2c3d4", native_state_path=state_path)
    state_path.write_text(json.dumps({"state": "running"}), encoding="utf-8")

    assert bridge.enqueue_native_terminal_events() == 0

    state_path.write_text(
        json.dumps({"state": "done", "output": "untrusted model output"}),
        encoding="utf-8",
    )
    assert bridge.enqueue_native_terminal_events() == 0
    assert bridge.list_pending_events() == []
    observation = bridge._read_json(session / "native-observation.json")
    assert observation is not None
    assert observation["state"] == "done"
    assert "output" not in observation
    monkeypatch.setattr(
        bridge,
        "_write_json_atomic",
        lambda *args, **kwargs: pytest.fail("stable terminal state was rewritten"),
    )
    assert bridge.enqueue_native_terminal_events() == 0
    assert bridge._read_json(session / "native-observation.json") == observation
    assert bridge.list_pending_events() == []


def test_dispatcher_schedules_lifecycle_after_terminal_event_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bridge,
        "enqueue_native_terminal_events",
        lambda: calls.append("terminal-events") or 0,
    )
    dispatcher = events.EventDispatcher(
        reconcile_lifecycle=lambda: calls.append("lifecycle"),
    )

    dispatcher._reconcile_native_state()
    assert calls == ["terminal-events"]
    dispatcher._run_lifecycle_once()

    assert calls == ["terminal-events", "lifecycle"]


def test_dispatcher_contains_lifecycle_reconciliation_failures(monkeypatch):
    monkeypatch.setattr(bridge, "enqueue_native_terminal_events", lambda: 0)
    dispatcher = events.EventDispatcher(
        reconcile_lifecycle=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    dispatcher._reconcile_native_state()
    dispatcher._run_lifecycle_once()


def test_watch_session_never_runs_lifecycle_inline(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "enqueue_native_terminal_events", lambda: 0)
    entered = threading.Event()
    release = threading.Event()

    def reconcile() -> None:
        entered.set()
        release.wait(timeout=1)

    dispatcher = events.EventDispatcher(
        capture_route=lambda **kwargs: {},
        publish=lambda **kwargs: {"ok": True},
        reconcile_lifecycle=reconcile,
    )
    dispatcher.start()
    assert entered.wait(timeout=1)

    started = time.monotonic()
    try:
        dispatcher.watch_session(tmp_path / "session")
        assert time.monotonic() - started < 0.1
    finally:
        release.set()
        dispatcher.stop()


@pytest.mark.parametrize(
    ("native_state", "expected_kind"),
    [
        ("failed", "failed"),
        ("stopped", "session_ended"),
        ("terminated", "session_ended"),
        ("completed", "completed"),
    ],
)
def test_native_terminal_states_preserve_failure_semantics(
    tmp_path, native_state, expected_kind
):
    session = _session(tmp_path)
    state_path = tmp_path / "claude" / "jobs" / "c3d4e5f6" / "state.json"
    state_path.parent.mkdir(parents=True)
    bridge.set_native_id(session, "c3d4e5f6", native_state_path=state_path)
    state_path.write_text(json.dumps({"state": native_state}), encoding="utf-8")

    assert bridge.enqueue_terminal_event(session, native_state) is True
    event = bridge.list_pending_events()[0][1]
    assert event["kind"] == expected_kind
    assert event["payload"] == {"native_state": native_state}


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="requires kqueue")
def test_kqueue_state_change_publishes_without_polling(tmp_path):
    published: list[dict[str, object]] = []
    published_event = threading.Event()

    def publish(**event):
        published.append(event)
        published_event.set()
        return {"ok": True, "status": "published"}

    session: Path | None = None

    def reconcile_lifecycle() -> None:
        assert session is not None
        bridge.enqueue_terminal_event(session, "done")

    dispatcher = events.EventDispatcher(
        reconcile_lifecycle=reconcile_lifecycle,
        reconcile_seconds=30,
    )
    dispatcher.configure(capture_route=lambda **_kwargs: {}, publish=publish)
    socket_path = dispatcher.start()
    session = _session(tmp_path, event_socket=socket_path)
    state_path = tmp_path / "claude" / "jobs" / "b2c3d4e5" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"state": "running"}), encoding="utf-8")
    bridge.set_native_id(session, "b2c3d4e5", native_state_path=state_path)

    try:
        dispatcher.watch_session(session)
        deadline = time.monotonic() + 1
        while dispatcher.state_watch_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert dispatcher.state_watch_count() == 1

        replacement = state_path.with_suffix(".next")
        replacement.write_text(json.dumps({"state": "done"}), encoding="utf-8")
        os.replace(replacement, state_path)

        assert published_event.wait(timeout=2)
        assert len(published) == 1
        assert published[0]["kind"] == "completed"
        assert published[0]["event_id"] == f"terminal-{session.name}"
    finally:
        dispatcher.stop()


def test_dispatcher_publishes_then_moves_spool_record(tmp_path):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude finished",
        event_id="event-dispatch",
    )
    published: list[dict[str, object]] = []

    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
        reconcile_seconds=60,
    )

    assert dispatcher.drain_once() == 1
    assert published == [
        {
            "event_id": "event-dispatch",
            "kind": "completed",
            "message": "Claude finished",
            "route": {
                "session_key": "agent:main:telegram:dm:123",
                "parent_session_id": "parent-1",
            },
            "producer_id": "",
            "payload": {},
        }
    ]
    assert bridge.list_pending_events() == []


def _routeless_session(tmp_path: Path) -> Path:
    return bridge.create_session(
        "event-test-routeless", tmp_path, route={}, event_socket=""
    )


def test_dispatcher_dead_letters_route_less_event_without_publishing(tmp_path):
    session = _routeless_session(tmp_path)
    bridge.enqueue_event(
        session, kind="completed", message="done", event_id="event-noroute"
    )
    published: list[dict[str, object]] = []
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
        reconcile_seconds=60,
    )

    assert dispatcher.drain_once() == 0
    assert dispatcher.drain_once() == 0  # no retry churn
    assert published == []
    assert bridge.list_pending_events() == []
    record = bridge._read_json(
        session / "events" / "undeliverable" / "event-noroute.json"
    )
    assert record is not None
    assert record["undeliverable_reason"] == "route has no routable session identity"
    assert isinstance(record["undeliverable_at"], float)
    assert bridge._existing_event(session, "event-noroute") is not None
    # A dead-lettered terminal record still coalesces later terminal hints.
    assert bridge.enqueue_terminal_event(session, "done") is True
    assert dispatcher.drain_once() == 0
    assert (
        session / "events" / "undeliverable" / f"terminal-{session.name}.json"
    ).is_file()
    assert bridge.enqueue_terminal_event(session, "done") is False


def test_dispatcher_dead_letters_after_bounded_publish_failures(tmp_path):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session, kind="needs_input", message="x", event_id="event-flaky"
    )
    calls: list[int] = []
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **_kwargs: (
            calls.append(1) or {"ok": False, "error": "temporary"}
        ),
        reconcile_seconds=60,
    )

    for attempt in range(1, bridge.MAX_PUBLISH_ATTEMPTS):
        assert dispatcher.drain_once() == 0
        pending = bridge.list_pending_events()
        assert pending[0][1]["publish_attempts"] == attempt
        assert pending[0][1]["publish_last_error"] == "temporary"
    assert dispatcher.drain_once() == 0
    assert bridge.list_pending_events() == []
    assert len(calls) == bridge.MAX_PUBLISH_ATTEMPTS
    record = bridge._read_json(
        session / "events" / "undeliverable" / "event-flaky.json"
    )
    assert record is not None
    assert record["publish_attempts"] == bridge.MAX_PUBLISH_ATTEMPTS
    assert record["undeliverable_reason"].startswith("publish failed 5 times")
    assert dispatcher.drain_once() == 0  # nothing left to retry


def test_dispatcher_dead_letters_when_publish_raises_repeatedly(tmp_path):
    session = _session(tmp_path)
    bridge.enqueue_event(session, kind="completed", message="x", event_id="event-raise")

    def _boom(**_kwargs):
        raise ValueError("route has no routable session identity")

    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {}, publish=_boom, reconcile_seconds=60
    )
    for _ in range(bridge.MAX_PUBLISH_ATTEMPTS):
        assert dispatcher.drain_once() == 0
    record = bridge._read_json(
        session / "events" / "undeliverable" / "event-raise.json"
    )
    assert record is not None
    assert (
        "ValueError: route has no routable session identity"
        in record["publish_last_error"]
    )


def test_wait_still_consumes_a_dead_lettered_event(tmp_path):
    session = _routeless_session(tmp_path)
    bridge.enqueue_event(
        session, kind="completed", message="done", event_id="event-late"
    )
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **_kwargs: {"ok": True},
        reconcile_seconds=60,
    )
    assert dispatcher.drain_once() == 0
    assert (session / "events" / "undeliverable" / "event-late.json").is_file()

    event = dispatcher.wait_for_event(session, timeout_seconds=0.2)

    assert event is not None
    assert event["event_id"] == "event-late"
    assert event["undeliverable_reason"] == "route has no routable session identity"
    assert (session / "events" / "consumed" / "event-late.json").is_file()
    assert not (session / "events" / "undeliverable" / "event-late.json").exists()
    assert dispatcher.wait_for_event(session, timeout_seconds=0.05) is None


def test_active_waiter_keeps_route_less_event_pending(tmp_path):
    session = _routeless_session(tmp_path)
    bridge.enqueue_event(
        session, kind="completed", message="done", event_id="event-held"
    )
    marker = bridge.register_event_waiter(session)
    try:
        dispatcher = events.EventDispatcher(
            capture_route=lambda **_kwargs: {},
            publish=lambda **_kwargs: {"ok": True},
            reconcile_seconds=60,
        )
        assert dispatcher.drain_once() == 0
        assert bridge.list_pending_events()[0][1]["event_id"] == "event-held"
    finally:
        bridge.unregister_event_waiter(marker)


def test_dispatcher_keeps_event_pending_when_host_rejects_it(tmp_path):
    _session(tmp_path)
    session = next(bridge.state_root().iterdir())
    bridge.enqueue_event(
        session,
        kind="needs_input",
        message="Claude needs input",
        event_id="event-retry",
    )
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **_kwargs: {"ok": False, "error": "temporary"},
        reconcile_seconds=60,
    )

    assert dispatcher.drain_once() == 0
    assert bridge.list_pending_events()[0][1]["event_id"] == "event-retry"


def test_socket_hint_drains_spool_without_polling(tmp_path):
    delivered = threading.Event()
    calls: list[dict[str, object]] = []

    def publish(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        delivered.set()
        return {"ok": True, "status": "published"}

    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=publish,
        reconcile_seconds=30,
    )
    try:
        socket_path = dispatcher.start()
        assert socket_path
        session = _session(tmp_path, event_socket=socket_path)
        bridge.enqueue_event(
            session,
            kind="completed",
            message="Claude completed.",
            event_id="event-socket",
        )

        assert bridge.signal_dispatcher(session) is True
        assert delivered.wait(timeout=2)
        assert calls[0]["event_id"] == "event-socket"
        deadline = time.monotonic() + 2
        while bridge.list_pending_events() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bridge.list_pending_events() == []
    finally:
        dispatcher.stop()


def test_waiter_claims_existing_event_without_publishing_a_fresh_turn(tmp_path):
    session = _session(tmp_path)
    bridge.set_native_id(session, "a1b2c3d4")
    bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude completed.",
        event_id="event-before-wait",
        payload={"notification_type": "agent_completed"},
    )
    published: list[dict[str, object]] = []
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
        reconcile_seconds=30,
    )

    event = dispatcher.wait_for_event(session, timeout_seconds=0.1)

    assert event is not None
    assert event["event_id"] == "event-before-wait"
    assert dispatcher.drain_once() == 0
    assert published == []
    assert bridge.list_pending_events() == []
    assert (session / "events" / "consumed" / "event-before-wait.json").is_file()


def test_socket_hint_delivers_to_active_waiter_without_fresh_turn(tmp_path):
    published: list[dict[str, object]] = []
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
        reconcile_seconds=30,
    )
    result: list[dict[str, object] | None] = []
    try:
        socket_path = dispatcher.start()
        session = _session(tmp_path, event_socket=socket_path)
        bridge.set_native_id(session, "a1b2c3d4")
        waiter = threading.Thread(
            target=lambda: result.append(
                dispatcher.wait_for_event(session, timeout_seconds=2)
            )
        )
        waiter.start()
        deadline = time.monotonic() + 1
        while not dispatcher.waiter_count(session) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert dispatcher.waiter_count(session) == 1

        bridge.enqueue_event(
            session,
            kind="needs_input",
            message="Claude needs input.",
            event_id="event-during-wait",
        )
        competing = events.EventDispatcher(
            capture_route=lambda **_kwargs: {},
            publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
            reconcile_seconds=30,
        )
        assert competing.drain_once() == 0
        assert bridge.signal_dispatcher(session) is True
        waiter.join(timeout=2)

        assert not waiter.is_alive()
        assert result[0] is not None
        assert result[0]["event_id"] == "event-during-wait"
        assert published == []
    finally:
        dispatcher.stop()


def test_dispatcher_claim_prevents_waiter_duplicate(tmp_path):
    session = _session(tmp_path)
    bridge.set_native_id(session, "a1b2c3d4")
    bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude completed.",
        event_id="event-dispatch-wins",
    )
    published: list[dict[str, object]] = []
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
        reconcile_seconds=30,
    )

    assert dispatcher.drain_once() == 1
    assert dispatcher.wait_for_event(session, timeout_seconds=0.01) is None
    assert [event["event_id"] for event in published] == ["event-dispatch-wins"]


def test_abandoned_claim_replays_after_dispatcher_restart(tmp_path):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude completed.",
        event_id="event-abandoned",
    )
    claim = bridge.claim_next_event(session, consumer="dead-dispatcher")
    assert claim is not None
    abandoned = claim.path.with_name(f"claim-999999-{uuid.uuid4().hex}.json")
    os.replace(claim.path, abandoned)
    published: list[dict[str, object]] = []
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
        reconcile_seconds=30,
    )

    assert dispatcher.drain_once() == 1
    assert [event["event_id"] for event in published] == ["event-abandoned"]


def test_stale_current_process_claim_replays_after_dispatcher_reload(tmp_path):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude completed.",
        event_id="event-stale-current-process",
    )
    claim = bridge.claim_next_event(session, consumer="old-dispatcher")
    assert claim is not None
    stale = time.time() - bridge.EVENT_CLAIM_STALE_SECONDS - 1
    os.utime(claim.path, (stale, stale))

    assert bridge.recover_abandoned_event_claims() == 1
    replay = bridge.claim_next_event(session, consumer="new-dispatcher")
    assert replay is not None
    assert replay.event["event_id"] == "event-stale-current-process"


def test_dispatcher_rechecks_waiter_after_claim(tmp_path, monkeypatch):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude completed.",
        event_id="event-waiter-race",
    )
    published: list[dict[str, object]] = []
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=lambda **kwargs: published.append(dict(kwargs)) or {"ok": True},
        reconcile_seconds=30,
    )
    original_claim = bridge.claim_event
    marker: list[Path] = []

    def claim_after_waiter_registration(*args, **kwargs):
        if not marker:
            marker.append(bridge.register_event_waiter(session))
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(bridge, "claim_event", claim_after_waiter_registration)

    assert dispatcher.drain_once() == 0
    assert published == []
    assert len(bridge.list_pending_events()) == 1

    bridge.unregister_event_waiter(marker[0])
    assert dispatcher.drain_once() == 1
    assert [event["event_id"] for event in published] == ["event-waiter-race"]


def test_claude_session_wait_returns_bounded_event_and_timeout(tmp_path, monkeypatch):
    session = _session(tmp_path)
    bridge.set_native_id(session, "a1b2c3d4")
    bridge.enqueue_event(
        session,
        kind="completed",
        message="Claude completed.",
        event_id="event-tool-wait",
        payload={"notification_type": "agent_completed"},
    )
    pending_event = bridge.list_pending_events()[0][1]
    monkeypatch.setattr(
        events,
        "wait_for_event",
        lambda *_args, **_kwargs: pending_event,
    )

    result = json.loads(
        tools.claude_session_wait({"id": "a1b2c3d4", "timeout_seconds": 1})
    )

    assert result == {
        "ok": True,
        "id": "a1b2c3d4",
        "status": "event",
        "event": {
            "event_id": "event-tool-wait",
            "kind": "completed",
            "message": "Claude completed.",
            "payload": {"notification_type": "agent_completed"},
            "created_at": result["event"]["created_at"],
        },
    }

    monkeypatch.setattr(events, "wait_for_event", lambda *_args, **_kwargs: None)
    assert json.loads(
        tools.claude_session_wait({"id": "a1b2c3d4", "timeout_seconds": 1})
    ) == {
        "ok": True,
        "id": "a1b2c3d4",
        "status": "timeout",
        "event": None,
    }


@pytest.mark.parametrize("timeout", [True, 0, 601, "1"])
def test_claude_session_wait_rejects_invalid_timeouts(tmp_path, timeout):
    session = _session(tmp_path)
    bridge.set_native_id(session, "a1b2c3d4")

    result = json.loads(
        tools.claude_session_wait({"id": "a1b2c3d4", "timeout_seconds": timeout})
    )

    assert result["ok"] is False


@pytest.mark.parametrize(
    ("payload", "expected_kind"),
    [
        (
            {
                "hook_event_name": "Notification",
                "notification_type": "agent_needs_input",
            },
            None,
        ),
        (
            {
                "hook_event_name": "Notification",
                "notification_type": "agent_completed",
            },
            None,
        ),
        (
            {"hook_event_name": "Notification", "notification_type": "unknown"},
            None,
        ),
        ({"hook_event_name": "Stop"}, "state_changed"),
        ({"hook_event_name": "Unknown"}, None),
    ],
)
def test_observer_hook_maps_only_supported_semantic_events(
    tmp_path, payload, expected_kind
):
    session = _session(tmp_path)

    mapped = event_hook._event_for(payload, session)

    if expected_kind is None:
        assert mapped is None
        return
    kind, message, event_id, safe_payload = mapped
    assert kind == expected_kind
    assert message.startswith("Managed Claude session")
    assert "last_assistant_message" not in safe_payload
    if expected_kind == "completed":
        assert event_id == f"terminal-{session.name}"


@pytest.mark.parametrize(
    ("raw", "environ"),
    [
        ("x" * (event_hook.MAX_HOOK_INPUT_CHARS + 1), {}),
        ("[]", {}),
        (json.dumps({"hook_event_name": "Stop"}), {}),
        (
            json.dumps({"hook_event_name": "Stop"}),
            {bridge.SESSION_DIR_ENV: "/missing/session"},
        ),
        (
            json.dumps({"hook_event_name": "Unknown"}),
            {bridge.SESSION_DIR_ENV: "/missing/session"},
        ),
        ("{", {}),
    ],
)
def test_observer_hook_fails_open_for_untrusted_or_unrouted_input(
    tmp_path, raw, environ
):
    if environ.get(bridge.SESSION_DIR_ENV) == "/missing/session":
        missing = tmp_path / "missing-session"
        environ = {bridge.SESSION_DIR_ENV: str(missing)}

    assert (
        event_hook.run(
            stdin=io.StringIO(raw),
            stdout=io.StringIO(),
            environ=environ,
        )
        == 0
    )
    assert bridge.list_pending_events() == []


def test_observer_hook_main_binds_explicit_session_directory(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr(
        event_hook.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop"})),
    )
    monkeypatch.setattr(event_hook.sys, "stdout", io.StringIO())

    assert event_hook.main(session_dir=str(session)) == 0
    assert bridge.list_pending_events()[0][1]["kind"] == "state_changed"


def test_hook_plugin_rejects_malformed_or_untrusted_templates(tmp_path):
    baseline = json.loads(
        (tools._CLAUDE_PLUGIN_DIR / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    cases: list[tuple[str, object]] = [
        ("invalid_json", None),
        ("hooks_not_mapping", {"hooks": []}),
        ("groups_not_list", {"hooks": {"Stop": {}}}),
        ("group_not_mapping", {"hooks": {"Stop": ["bad"]}}),
        ("handlers_not_list", {"hooks": {"Stop": [{"hooks": {}}]}}),
        (
            "handler_not_command",
            {"hooks": {"Stop": [{"hooks": [{"type": "prompt"}]}]}},
        ),
        (
            "unknown_script",
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/hooks/evil.py",
                                }
                            ]
                        }
                    ]
                }
            },
        ),
        ("missing_known_script", baseline),
    ]

    for index, (case, hooks) in enumerate(cases):
        source = tmp_path / f"source-{index}"
        shutil.copytree(tools._CLAUDE_PLUGIN_DIR, source)
        hooks_path = source / "hooks" / "hooks.json"
        if case == "invalid_json":
            hooks_path.write_text("{", encoding="utf-8")
        else:
            hooks_path.write_text(json.dumps(hooks), encoding="utf-8")
        if case == "missing_known_script":
            (source / "hooks" / "event_bridge.py").unlink()
        session = _session(tmp_path)

        with pytest.raises(ValueError, match="template|entry point"):
            bridge.create_hook_plugin(
                session,
                source,
                python_executable=tools.sys.executable,
            )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "completed", "message": "ok", "event_id": "bad id"},
        {"kind": "", "message": "ok"},
        {"kind": "x" * 65, "message": "ok"},
        {"kind": "completed", "message": ""},
        {
            "kind": "completed",
            "message": "ok",
            "payload": {"value": "x" * bridge.MAX_EVENT_PAYLOAD_BYTES},
        },
    ],
)
def test_enqueue_event_rejects_unbounded_or_malformed_records(tmp_path, kwargs):
    session = _session(tmp_path)

    with pytest.raises(ValueError):
        bridge.enqueue_event(session, **kwargs)


def test_enqueue_event_rejects_missing_metadata_and_failed_exclusive_write(
    tmp_path, monkeypatch
):
    session = _session(tmp_path)
    (session / "metadata.json").unlink()
    with pytest.raises(ValueError, match="metadata"):
        bridge.enqueue_event(session, kind="completed", message="ok")

    session = _session(tmp_path)
    monkeypatch.setattr(
        bridge, "_write_json_exclusive", lambda *_args, **_kwargs: False
    )
    with pytest.raises(RuntimeError, match="could not be persisted"):
        bridge.enqueue_event(
            session,
            kind="completed",
            message="ok",
            event_id="write-race",
        )


def test_claimed_event_remains_idempotent_and_corrupt_pending_is_discarded(tmp_path):
    session = _session(tmp_path)
    original = bridge.enqueue_event(
        session,
        kind="completed",
        message="ok",
        event_id="claimed-event",
    )
    claim = bridge.claim_event(session, "claimed-event", consumer="test")
    assert claim is not None

    assert (
        bridge.enqueue_event(
            session,
            kind="completed",
            message="different",
            event_id="claimed-event",
        )
        == original
    )
    assert bridge.claim_event(session, "bad id", consumer="test") is None

    corrupt = session / "events" / "pending" / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    assert bridge.claim_event(session, "corrupt", consumer="test") is None
    assert not corrupt.exists()


def test_claim_completion_and_release_handle_duplicate_or_failed_moves(
    tmp_path, monkeypatch
):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session,
        kind="completed",
        message="ok",
        event_id="duplicate-destination",
    )
    claim = bridge.claim_event(session, "duplicate-destination", consumer="test")
    assert claim is not None
    destination = session / "events" / "published" / "duplicate-destination.json"
    destination.write_text(json.dumps(claim.event), encoding="utf-8")
    assert bridge.complete_event_claim(claim, disposition="published") is True
    assert not claim.path.exists()

    bridge.enqueue_event(
        session,
        kind="completed",
        message="ok",
        event_id="failed-move",
    )
    claim = bridge.claim_event(session, "failed-move", consumer="test")
    assert claim is not None
    real_replace = bridge.os.replace

    def fail_claim_move(source, target):
        if source == claim.path:
            raise OSError("simulated move failure")
        return real_replace(source, target)

    monkeypatch.setattr(bridge.os, "replace", fail_claim_move)
    assert bridge.complete_event_claim(claim, disposition="published") is False
    assert bridge.release_event_claim(claim) is False


def test_release_claim_handles_missing_and_duplicate_pending_records(tmp_path):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session,
        kind="completed",
        message="ok",
        event_id="release-cases",
    )
    claim = bridge.claim_event(session, "release-cases", consumer="test")
    assert claim is not None
    pending = session / "events" / "pending" / "release-cases.json"
    pending.write_text(json.dumps(claim.event), encoding="utf-8")
    assert bridge.release_event_claim(claim) is True
    assert not claim.path.exists()

    missing_claim = bridge.EventClaim(
        session=session,
        event_id="missing-claim",
        consumer="test",
        path=session / "events" / "claimed" / "missing.json",
        event={},
    )
    assert bridge.release_event_claim(missing_claim) is False


def test_claim_recovery_cleans_corrupt_records_and_preserves_live_claims(
    tmp_path, monkeypatch
):
    session = _session(tmp_path)
    claimed = session / "events" / "claimed"
    corrupt = claimed / "claim-999999-corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    invalid = claimed / "claim-999999-invalid.json"
    invalid.write_text(json.dumps({"event_id": "bad id"}), encoding="utf-8")
    live = claimed / "claim-123-live.json"
    live.write_text(json.dumps({"event_id": "live-event"}), encoding="utf-8")
    monkeypatch.setattr(bridge, "_pid_is_alive", lambda pid: pid == 123)

    assert bridge.recover_abandoned_event_claims() == 0
    assert not corrupt.exists()
    assert not invalid.exists()
    assert live.exists()


def test_waiter_cleanup_and_event_marking_fail_closed(tmp_path, monkeypatch):
    session = _session(tmp_path)
    assert bridge.has_active_event_waiter(session) is False
    stale = session / "events" / "waiters" / "wait-999999-stale.json"
    stale.write_text("{}", encoding="utf-8")
    assert bridge.has_active_event_waiter(session) is False
    assert not stale.exists()

    assert bridge.mark_event_published(session, "bad id") is False
    assert bridge.mark_event_published(session, "missing-event") is False
    published = session / "events" / "published" / "already.json"
    published.write_text("{}", encoding="utf-8")
    assert bridge.mark_event_published(session, "already") is True

    monkeypatch.setattr(
        bridge, "_write_json_exclusive", lambda *_args, **_kwargs: False
    )
    with pytest.raises(OSError, match="waiter marker"):
        bridge.register_event_waiter(session)


@pytest.mark.parametrize(
    "publisher",
    [
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        lambda **_kwargs: "not-a-mapping",
    ],
)
def test_dispatcher_releases_claim_when_host_publish_fails(tmp_path, publisher):
    session = _session(tmp_path)
    bridge.enqueue_event(
        session,
        kind="completed",
        message="ok",
        event_id="publisher-failure",
    )
    dispatcher = events.EventDispatcher(
        capture_route=lambda **_kwargs: {},
        publish=publisher,
    )

    assert dispatcher.drain_once() == 0
    assert bridge.list_pending_events()[0][1]["event_id"] == "publisher-failure"


def test_dispatcher_route_capture_and_optional_context_fail_closed(tmp_path):
    dispatcher = events.EventDispatcher()
    assert dispatcher.capture_route(parent_session_id="parent") == {}
    assert dispatcher.start() == ""

    dispatcher.configure(
        capture_route=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bad")),
        publish=lambda **_kwargs: {"ok": True},
    )
    assert dispatcher.capture_route(parent_session_id="parent") == {}
    dispatcher.configure(
        capture_route=lambda **_kwargs: "not-a-mapping",
        publish=lambda **_kwargs: {"ok": True},
    )
    assert dispatcher.capture_route(parent_session_id="parent") == {}

    assert events.configure_context(object()) is False
    assert events.wait_for_event("ffffffff", timeout_seconds=0) is None


def test_configure_context_registers_cleanup_and_starts_once(tmp_path):
    cleanups = []

    class Context:
        def capture_background_event_route(self, **_kwargs):
            return {}

        def publish_background_event(self, **_kwargs):
            return {"ok": True}

        def on_unload(self, callback):
            cleanups.append(callback)

    events._dispatcher.stop()
    try:
        assert events.configure_context(Context()) is True
        first_socket = events.socket_path()
        assert first_socket
        assert events.configure_context(Context()) is True
        assert events.socket_path() == first_socket
        assert len(cleanups) == 2
    finally:
        events._dispatcher.stop()
