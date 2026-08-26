from __future__ import annotations

import json
import subprocess
import threading
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from hermes_claude_sessions import bridge, permission_hook, tools


def decode(value: str) -> dict[str, Any]:
    return json.loads(value)


def prepare_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, native_id: str = "a1b2c3d4"
) -> Path:
    root = tmp_path / "state"
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    monkeypatch.setattr(bridge, "state_root", lambda: root)
    session = bridge.create_session("managed-test", repository)
    bridge.set_native_id(session, native_id)
    return session


def test_managed_launch_uses_auto_classifier_and_permission_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    root = tmp_path / "state"
    monkeypatch.setattr(bridge, "state_root", lambda: root)
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            "backgrounded · a1b2c3d4 · managed-test\n",
            "",
        )

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    result = decode(
        tools.claude_session_start(
            {"name": "managed-test", "cwd": str(repository), "prompt": "work"}
        )
    )

    assert result["ok"] is True
    assert result["mode"] == "auto"
    assert result["id"] == "a1b2c3d4"
    argv, kwargs = calls[0]
    assert argv[:6] == [
        "/mock/claude",
        "--bg",
        "--name",
        "managed-test",
        "--permission-mode",
        "auto",
    ]
    plugin_index = argv.index("--plugin-dir")
    assert Path(argv[plugin_index + 1]).is_dir()
    assert argv[-2:] == ["--", "work"]
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    session_dir = Path(str(environment[bridge.SESSION_DIR_ENV]))
    metadata = json.loads((session_dir / "metadata.json").read_text())
    assert metadata["native_id"] == "a1b2c3d4"
    assert metadata["name"] == "managed-test"
    assert "work" not in json.dumps(metadata)


def test_permission_hook_round_trip_allows_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = prepare_session(monkeypatch, tmp_path)
    hook_input = {
        "session_id": "native-uuid",
        "cwd": str(tmp_path / "repo"),
        "permission_mode": "auto",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "git diff --check"},
    }
    output = StringIO()
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            permission_hook.run(
                stdin=StringIO(json.dumps(hook_input)),
                stdout=output,
                environ={
                    bridge.SESSION_DIR_ENV: str(session),
                    bridge.TIMEOUT_ENV: "2",
                },
            )
        except BaseException as error:  # pragma: no cover - assertion aid
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    request = bridge.wait_for_pending(timeout_seconds=1)
    assert request is not None
    assert request["native_id"] == "a1b2c3d4"
    assert request["tool_name"] == "Bash"
    assert request["tool_input"] == {"command": "git diff --check"}

    result = bridge.decide(request["request_id"], "allow", "within delegated scope")
    assert result["decision"] == "allow"
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert errors == []
    assert json.loads(output.getvalue()) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    assert bridge.list_pending() == []


def test_permission_hook_denies_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = prepare_session(monkeypatch, tmp_path)
    output = StringIO()

    permission_hook.run(
        stdin=StringIO(
            json.dumps(
                {
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Bash",
                    "tool_input": {"command": "curl example.com"},
                }
            )
        ),
        stdout=output,
        environ={
            bridge.SESSION_DIR_ENV: str(session),
            bridge.TIMEOUT_ENV: "0.01",
        },
    )

    decision = json.loads(output.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert "timed out" in decision["message"].lower()
    assert decision["interrupt"] is False


def test_permission_decision_is_single_use_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = prepare_session(monkeypatch, tmp_path)
    request = bridge.enqueue_request(
        session,
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Write",
            "tool_input": {},
        },
    )

    assert bridge.decide(request["request_id"], "deny", "outside scope")["ok"] is True
    second = bridge.decide(request["request_id"], "allow", "changed mind")
    assert second == {"ok": False, "error": "permission request is no longer pending"}
    assert bridge.decide("not-an-id", "allow", "bad")["ok"] is False
    assert bridge.decide("f" * 32, "maybe", "bad")["ok"] is False


def test_logs_and_stop_are_restricted_to_managed_owned_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepare_session(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        if argv[1] == "agents":
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    logs = decode(tools.claude_session_logs({"id": "a1b2c3d4"}))
    stopped = decode(tools.claude_session_stop({"id": "a1b2c3d4"}))
    unowned = decode(tools.claude_session_stop({"id": "deadbeef"}))

    assert logs["ok"] is True
    assert stopped == {"ok": True, "id": "a1b2c3d4", "confirmed": True}
    assert unowned == {"ok": False, "error": "session is not managed by Hermes"}
    assert ["/mock/claude", "logs", "a1b2c3d4"] in calls
    assert ["/mock/claude", "stop", "a1b2c3d4"] in calls
    assert ["/mock/claude", "stop", "deadbeef"] not in calls


def test_permission_tool_handlers_return_bounded_controlled_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = prepare_session(monkeypatch, tmp_path)
    request = bridge.enqueue_request(
        session,
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
        },
    )

    pending = decode(tools.claude_permission_list({}))
    assert pending["ok"] is True
    assert pending["requests"][0]["request_id"] == request["request_id"]

    decided = decode(
        tools.claude_permission_decide(
            {
                "request_id": request["request_id"],
                "decision": "allow",
                "reason": "test command is within scope",
            }
        )
    )
    assert decided["ok"] is True
    assert len(json.dumps(decided)) <= tools.MAX_OUTPUT_CHARS


def test_bridge_rejects_invalid_paths_metadata_and_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "state"
    monkeypatch.setattr(bridge, "state_root", lambda: root)

    with pytest.raises(ValueError, match="existing directory"):
        bridge.create_session("missing", tmp_path / "missing")
    assert bridge.find_session("not-an-id") is None
    assert bridge.find_session("a1b2c3d4") is None

    outside = tmp_path / "outside"
    outside.mkdir()
    bridge.remove_session(outside)
    assert outside.exists()

    repository = tmp_path / "repo"
    repository.mkdir()
    session = bridge.create_session("managed", repository)
    with pytest.raises(ValueError, match="eight lowercase"):
        bridge.set_native_id(session, "BAD")

    (session / "metadata.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="metadata"):
        bridge.set_native_id(session, "a1b2c3d4")
    with pytest.raises(ValueError, match="metadata"):
        bridge.enqueue_request(session, {})


def test_bridge_bounds_requests_and_validates_one_shot_decisions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = prepare_session(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="size limit"):
        bridge.enqueue_request(session, {"tool_input": "x" * bridge.MAX_REQUEST_BYTES})

    assert not bridge.decide("bad", "allow", "reason")["ok"]
    request = bridge.enqueue_request(
        session,
        {"tool_name": "Bash", "tool_input": {"command": "pytest"}},
    )
    request_id = str(request["request_id"])
    assert not bridge.decide(request_id, "maybe", "reason")["ok"]
    assert not bridge.decide(request_id, "allow", "")["ok"]
    assert not bridge.decide(request_id, "allow", "x" * (bridge.MAX_REASON_CHARS + 1))[
        "ok"
    ]
    assert bridge.decide(request_id, "allow", "task-scoped")["ok"]
    assert not bridge.decide(request_id, "deny", "duplicate")["ok"]

    exclusive = tmp_path / "exclusive.json"
    assert bridge._write_json_exclusive(exclusive, {"value": 1})
    assert not bridge._write_json_exclusive(exclusive, {"value": 2})


def test_pending_list_ignores_invalid_and_already_answered_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = prepare_session(monkeypatch, tmp_path)
    stray = bridge.state_root() / "stray"
    stray.mkdir()
    (session / "requests" / "bad.json").write_text("[1]", encoding="utf-8")
    request = bridge.enqueue_request(session, {"tool_name": "Bash", "tool_input": {}})

    assert bridge.wait_for_pending(0.1) == request
    assert bridge.decide(str(request["request_id"]), "deny", "out of scope")["ok"]
    assert bridge.list_pending() == []


@pytest.mark.parametrize(
    ("payload", "environment"),
    [
        ([], {}),
        ({"hook_event_name": "Other"}, {}),
        ({"hook_event_name": "PermissionRequest"}, {}),
        (
            {"hook_event_name": "PermissionRequest"},
            {bridge.SESSION_DIR_ENV: "/missing"},
        ),
    ],
)
def test_permission_hook_denies_malformed_or_unbound_input(
    payload: object, environment: dict[str, str]
) -> None:
    stdout = StringIO()

    assert (
        permission_hook.run(
            stdin=StringIO(json.dumps(payload)), stdout=stdout, environ=environment
        )
        == 0
    )
    decision = json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert "failed closed" in decision["message"]


def test_permission_hook_validates_timeout_and_invalid_responses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert permission_hook._timeout({bridge.TIMEOUT_ENV: "bad"}) == pytest.approx(
        permission_hook.DEFAULT_TIMEOUT_SECONDS
    )
    assert permission_hook._timeout({bridge.TIMEOUT_ENV: "9999"}) == pytest.approx(
        permission_hook.MAX_TIMEOUT_SECONDS
    )

    session = prepare_session(monkeypatch, tmp_path)
    stdout = StringIO()
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
    }
    thread = threading.Thread(
        target=permission_hook.run,
        kwargs={
            "stdin": StringIO(json.dumps(payload)),
            "stdout": stdout,
            "environ": {
                bridge.SESSION_DIR_ENV: str(session),
                bridge.TIMEOUT_ENV: "1",
            },
        },
    )
    thread.start()
    pending = bridge.wait_for_pending(1)
    assert pending is not None
    request_id = str(pending["request_id"])
    bridge._write_json_atomic(
        session / "responses" / f"{request_id}.json",
        {"decision": "invalid", "reason": 42},
    )
    thread.join(timeout=2)

    decision = json.loads(stdout.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert "invalid permission decision" in decision["message"]


def test_permission_hook_resolves_internal_failure_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = prepare_session(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bridge,
        "read_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sensitive")),
    )
    stdout = StringIO()

    permission_hook.run(
        stdin=StringIO(
            json.dumps(
                {
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest"},
                }
            )
        ),
        stdout=stdout,
        environ={bridge.SESSION_DIR_ENV: str(session)},
    )

    assert "failed closed" in stdout.getvalue()
    resolved = list((session / "resolved").glob("*.json"))
    assert len(resolved) == 1
    assert json.loads(resolved[0].read_text())["decision"] == "deny"


def test_managed_tool_handlers_fail_closed_on_invalid_or_unowned_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        bridge, "list_pending", lambda: (_ for _ in ()).throw(RuntimeError("secret"))
    )
    assert not decode(tools.claude_permission_list({}))["ok"]
    assert not decode(tools.claude_permission_decide(None))["ok"]
    assert not decode(tools.claude_permission_decide({"request_id": 1}))["ok"]
    assert not decode(
        tools.claude_permission_decide(
            {"request_id": "a" * 32, "decision": 1, "reason": "x"}
        )
    )["ok"]
    assert not decode(
        tools.claude_permission_decide(
            {"request_id": "a" * 32, "decision": "allow", "reason": 1}
        )
    )["ok"]
    monkeypatch.setattr(
        bridge, "decide", lambda *args: (_ for _ in ()).throw(RuntimeError("secret"))
    )
    assert not decode(
        tools.claude_permission_decide(
            {"request_id": "a" * 32, "decision": "allow", "reason": "x"}
        )
    )["ok"]

    assert not decode(tools.claude_session_logs(None))["ok"]
    assert not decode(tools.claude_session_logs({"id": "BAD"}))["ok"]
    assert not decode(tools.claude_session_logs({"id": "a1b2c3d4"}))["ok"]

    prepare_session(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: None)
    assert not decode(tools.claude_session_logs({"id": "a1b2c3d4"}))["ok"]


def test_owned_logs_and_stop_surface_native_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepare_session(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")

    monkeypatch.setattr(
        tools,
        "_run_claude",
        lambda argv: subprocess.CompletedProcess(argv, 2, "out", "err"),
    )
    logs = decode(tools.claude_session_logs({"id": "a1b2c3d4"}))
    stopped = decode(tools.claude_session_stop({"id": "a1b2c3d4"}))
    assert logs["returncode"] == stopped["returncode"] == 2

    monkeypatch.setattr(tools, "_run_claude", lambda argv: '{"ok": false}')
    assert not decode(tools.claude_session_logs({"id": "a1b2c3d4"}))["ok"]
    assert not decode(tools.claude_session_stop({"id": "a1b2c3d4"}))["ok"]


def test_owned_logs_return_bounded_recent_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepare_session(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools,
        "_run_claude",
        lambda argv: subprocess.CompletedProcess(
            argv,
            0,
            "old" + "\x00" * tools.MAX_OUTPUT_CHARS + "recent-stdout",
            "old" + "\x01" * tools.MAX_OUTPUT_CHARS + "recent-stderr",
        ),
    )

    result_text = tools.claude_session_logs({"id": "a1b2c3d4"})
    result = decode(result_text)

    assert result["ok"] is True
    assert result["stdout"].startswith("[truncated]\n")
    assert result["stdout"].endswith("recent-stdout")
    assert result["stderr"].endswith("recent-stderr")
    assert len(result_text) <= tools.MAX_OUTPUT_CHARS


def test_stop_requires_readback_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepare_session(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools,
        "_run_claude",
        lambda argv: subprocess.CompletedProcess(argv, 0, "stopped", ""),
    )
    monkeypatch.setattr(
        tools,
        "_native_sessions",
        lambda binary: [{"id": "a1b2c3d4", "state": "working"}],
    )
    monkeypatch.setattr(tools.time, "sleep", lambda seconds: None)

    result = decode(tools.claude_session_stop({"id": "a1b2c3d4"}))

    assert not result["ok"]
    assert "could not be confirmed" in result["error"]
