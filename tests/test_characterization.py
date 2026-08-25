from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

# Task 0B imports are intentionally separate: they specify the bounded,
# opt-in observer-hook path without widening Task 0A's public command runner.
from tests.characterization.support import (
    EXACT_CLAUDE_BIN,
    MAX_OUTPUT_BYTES,
    OBSERVER_EVENT_MATCHERS,
    OBSERVER_TIMEOUT_SECONDS,
    REQUIRED_AGENT_OBJECT_KEYS,
    AcknowledgedClaudeBinary,
    CharacterizationError,
    CommandCapture,
    _cleanup_owned,
    _create_observer_plugin,
    _discover_native_identity,
    _launch_display_id,
    _mint_owned_probe,
    _owned_rows,
    _owned_session_id,
    _OwnedProbe,
    _run_command,
    _run_owned_observer_command,
    atomic_write_contract,
    collect_live_contract,
    collect_unattended_observer_hook_characterization,
    contract_fingerprint,
    extract_required_contract,
    run_command,
    sanitize_contract,
    sanitize_text,
    validate_agents_json_payload,
    validate_prerequisites,
)


def _validated_capability(binary: Path) -> AcknowledgedClaudeBinary:
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return validate_prerequisites(
        {
            "RUN_CLAUDE_CHARACTERIZATION": "1",
            "CLAUDE_CHARACTERIZATION_ACK_COST": "1",
            "CLAUDE_BIN": str(binary),
        }
    )


def test_prerequisites_require_two_explicit_acknowledgements(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(CharacterizationError, match="RUN_CLAUDE_CHARACTERIZATION"):
        validate_prerequisites({"CLAUDE_BIN": str(binary)})

    with pytest.raises(CharacterizationError, match="ACK_COST"):
        validate_prerequisites(
            {
                "RUN_CLAUDE_CHARACTERIZATION": "1",
                "CLAUDE_BIN": str(binary),
            }
        )

    capability = _validated_capability(binary)
    assert isinstance(capability, AcknowledgedClaudeBinary)
    with pytest.raises(TypeError, match="issued by validation only"):
        AcknowledgedClaudeBinary(binary, object())


def test_prerequisites_reject_relative_or_nonexecutable_binary(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("not executable")
    env = {
        "RUN_CLAUDE_CHARACTERIZATION": "1",
        "CLAUDE_CHARACTERIZATION_ACK_COST": "1",
        "CLAUDE_BIN": "claude",
    }
    with pytest.raises(CharacterizationError, match="absolute"):
        validate_prerequisites(env)

    env["CLAUDE_BIN"] = str(binary)
    with pytest.raises(CharacterizationError, match="executable"):
        validate_prerequisites(env)


def test_private_runner_captures_timeout_and_os_error() -> None:
    timed_out = _run_command(
        [
            sys.executable,
            "-c",
            "import os, time; print(os.getpid(), flush=True); time.sleep(2)",
        ],
        timeout_seconds=0.2,
    )
    assert timed_out.timed_out is True
    assert timed_out.returncode is None
    child_pid = int(timed_out.stdout.strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)

    missing = _run_command(["/definitely/not/a/command"], timeout_seconds=1)
    assert missing.returncode is None
    assert missing.error
    assert "definitely/not/a/command" in missing.error


def test_private_runner_allows_success_through_full_execution_timeout() -> None:
    timeout_seconds = 2.0
    started = time.monotonic()
    capture = _run_command(
        [sys.executable, "-c", "import time; time.sleep(1.2); print('done')"],
        timeout_seconds=timeout_seconds,
    )
    elapsed = time.monotonic() - started

    assert capture.timed_out is False
    assert capture.returncode == 0
    assert capture.stdout.strip() == "done"
    assert 1.0 <= elapsed < timeout_seconds


@pytest.mark.skipif(os.name != "posix", reason="signal behavior is POSIX-specific")
def test_private_runner_explicit_overall_timeout_is_a_hard_wall_clock_budget() -> None:
    timeout_seconds = 0.05
    overall_timeout_seconds = 0.1
    started = time.monotonic()
    capture = _run_command(
        [
            sys.executable,
            "-c",
            (
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(30)"
            ),
        ],
        timeout_seconds=timeout_seconds,
        overall_timeout_seconds=overall_timeout_seconds,
    )
    elapsed = time.monotonic() - started

    assert capture.timed_out is True
    assert elapsed < 0.4


def test_private_runner_rejects_overall_timeout_shorter_than_execution() -> None:
    with pytest.raises(ValueError, match="must cover timeout_seconds"):
        _run_command(
            [sys.executable, "-c", "pass"],
            timeout_seconds=1,
            overall_timeout_seconds=0.5,
        )


@pytest.mark.skipif(
    os.name != "posix", reason="process-group containment is POSIX-specific"
)
def test_private_runner_timeout_terminates_descendant_holding_pipes(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "descendant-ready"
    child_code = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "os.close(os.open(sys.argv[1], os.O_CREAT)); time.sleep(30)"
    )
    parent_code = (
        "import os, subprocess, sys, time\n"
        "ready = sys.argv[1]\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}, ready], "
        "stdout=sys.stdout, stderr=sys.stderr)\n"
        "while not os.path.exists(ready):\n"
        "    time.sleep(0.01)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)"
    )
    started = time.monotonic()
    capture = _run_command(
        [sys.executable, "-c", parent_code, str(ready)], timeout_seconds=0.5
    )
    elapsed = time.monotonic() - started

    assert capture.timed_out is True
    assert capture.returncode is None
    assert elapsed < 1.0
    descendant_pid = int(capture.stdout.strip())
    cleanup_deadline = time.monotonic() + 1
    while time.monotonic() < cleanup_deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("contained descendant was not cleaned up")


@pytest.mark.skipif(
    os.name != "posix", reason="process-group containment is POSIX-specific"
)
def test_private_runner_deadline_includes_inherited_pipe_eof(tmp_path: Path) -> None:
    ready = tmp_path / "descendant-ready"
    pid_file = tmp_path / "descendant-pid"
    descendant_code = (
        "import os, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "open(sys.argv[2], 'w').close(); time.sleep(30)"
    )
    parent_code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}, "
        "sys.argv[1], sys.argv[2]], stdout=sys.stdout, stderr=sys.stderr)\n"
    )
    worker_code = (
        "import json, sys, threading\n"
        "from tests.characterization.support import _run_command\n"
        f"capture = _run_command([sys.executable, '-c', {parent_code!r}, "
        "sys.argv[1], sys.argv[2]], timeout_seconds=0.3)\n"
        "print(json.dumps({'timed_out': capture.timed_out, "
        "'returncode': capture.returncode, "
        "'drain_threads': sum(thread.name.startswith('command-') "
        "for thread in threading.enumerate())}))\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", worker_code, str(pid_file), str(ready)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result == {"timed_out": True, "returncode": 0, "drain_threads": 0}
        assert ready.exists()
        descendant_pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.skipif(
    os.name != "posix", reason="escaped-session containment is POSIX-specific"
)
def test_private_runner_deadline_survives_escaped_pipe_holder(tmp_path: Path) -> None:
    pid_file = tmp_path / "escaped-pid"
    ready = tmp_path / "escaped-ready"
    result_file = tmp_path / "runner-result.json"
    descendant_code = (
        "import os, signal, sys, time; "
        "os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(os.getpid())); "
        "open(sys.argv[2], 'w').close(); time.sleep(30)"
    )
    parent_code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}, "
        "sys.argv[1], sys.argv[2]], stdout=sys.stdout, stderr=sys.stderr)\n"
    )
    worker_code = (
        "import json, sys, threading\n"
        "from tests.characterization.support import _run_command\n"
        f"capture = _run_command([sys.executable, '-c', {parent_code!r}, "
        "sys.argv[1], sys.argv[2]], timeout_seconds=0.3)\n"
        "open(sys.argv[3], 'w').write(json.dumps({"
        "'timed_out': capture.timed_out, 'returncode': capture.returncode, "
        "'drain_threads': sum(thread.name.startswith('command-') "
        "for thread in threading.enumerate())}))\n"
    )
    worker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            worker_code,
            str(pid_file),
            str(ready),
            str(result_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 2
        while worker.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.poll() == 0, "runner exceeded strict outer deadline"
        assert ready.exists()
        assert json.loads(result_file.read_text()) == {
            "timed_out": True,
            "returncode": 0,
            "drain_threads": 0,
        }
    finally:
        if worker.poll() is None:
            worker.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            worker.wait(timeout=1)
        if pid_file.exists():
            escaped_pid = int(pid_file.read_text())
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped_pid, signal.SIGKILL)
            cleanup_deadline = time.monotonic() + 1
            while time.monotonic() < cleanup_deadline:
                try:
                    os.kill(escaped_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("test-owned escaped descendant was not cleaned up")


def test_private_runner_bounds_noisy_stdout_and_stderr_during_execution() -> None:
    capture = _run_command(
        [
            sys.executable,
            "-c",
            "import sys; "
            "[print('o' * 8192) for _ in range(1024)]; "
            "[print('e' * 8192, file=sys.stderr) for _ in range(1024)]",
        ],
        timeout_seconds=10,
    )
    assert capture.returncode == 0
    assert len(capture.stdout.encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert len(capture.stderr.encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_run_command_rejects_non_capabilities_and_mutating_suffixes_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_started(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess creation must not be reached")

    monkeypatch.setattr(subprocess, "Popen", fail_if_started)
    for binary in (str(tmp_path / "claude"), tmp_path / "claude"):
        with pytest.raises(CharacterizationError, match="validated Claude capability"):
            run_command(binary, ("--version",))
        with pytest.raises(CharacterizationError, match="validated Claude capability"):
            collect_live_contract(binary)

    capability = _validated_capability(tmp_path / "claude")
    for suffix in (
        ("stop", "session"),
        ("respawn", "session"),
        ("rm", "session"),
        ("launch",),
    ):
        with pytest.raises(CharacterizationError, match="not an allowlisted"):
            run_command(capability, suffix)


def test_run_command_allows_only_exact_safe_suffixes(tmp_path: Path) -> None:
    capability = _validated_capability(tmp_path / "claude")
    capture = run_command(capability, ("--version",), timeout_seconds=5)
    assert capture.returncode == 0
    assert capture.argv == (str(tmp_path / "claude"), "--version")


def test_agents_json_payload_requires_a_list_of_identified_agent_objects() -> None:
    assert frozenset(("id", "name", "state")) == REQUIRED_AGENT_OBJECT_KEYS
    validate_agents_json_payload([])
    validate_agents_json_payload(
        [{"id": "agent-1", "name": "worker", "state": "running"}]
    )

    with pytest.raises(TypeError, match="top-level JSON array"):
        validate_agents_json_payload({"agents": []})
    with pytest.raises(TypeError, match="an object"):
        validate_agents_json_payload(["agent-1"])
    with pytest.raises(ValueError, match="name, state"):
        validate_agents_json_payload([{"id": "agent-1"}])


def test_sanitization_removes_user_home_temp_namespace_and_prompt() -> None:
    raw = (
        "ryo at /Users/ryo/workspace and /var/folders/ab/cd/T/job-123; "
        "/private/var/folders/ab/cd/T/job-456; "
        "namespace=ns_01JABCDEF1234567890ABCDEF; prompt=secret prompt"
    )
    clean = sanitize_text(raw, username="ryo", sensitive_values=["secret prompt"])
    contract = sanitize_contract(
        {"prompt": "unique prompt", "namespace": "private-namespace"}, username="ryo"
    )
    assert "ryo" not in clean
    assert "/Users/ryo" not in clean
    assert "/var/folders/ab/cd/T/job-123" not in clean
    assert "/private/var/folders/ab/cd/T/job-456" not in clean
    assert "secret prompt" not in clean
    assert "<HOME>" in clean
    assert "<TEMP_PATH>" in clean
    assert "<NAMESPACE>" in clean
    assert contract["prompt"] == "<REDACTED_PROMPT>"
    assert contract["namespace"] == "<NAMESPACE>"
    current_temp = f"{tempfile.gettempdir()}/characterization-output"
    assert current_temp not in sanitize_text(current_temp, username="not-present")


def test_sanitize_text_redacts_arbitrary_prompt_and_namespace_from_json_and_text() -> (
    None
):
    json_capture = json.dumps(
        {"agents": [{"prompt": "arbitrary secret", "namespace": "tenant-blue"}]}
    )
    clean_json = sanitize_text(json_capture, username="not-present")
    clean_text = sanitize_text(
        "prompt=arbitrary secret with spaces\nnamespace=tenant-blue",
        username="not-present",
    )
    assert "arbitrary secret" not in clean_json
    assert "tenant-blue" not in clean_json
    assert json.loads(clean_json) == {
        "agents": [{"namespace": "<NAMESPACE>", "prompt": "<REDACTED_PROMPT>"}]
    }
    assert "arbitrary secret" not in clean_text
    assert "tenant-blue" not in clean_text
    assert "<REDACTED_PROMPT>" in clean_text
    assert "<NAMESPACE>" in clean_text


def test_collect_live_contract_sanitizes_every_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = _validated_capability(tmp_path / "claude")

    def fake_run(capability: object, suffix: tuple[str, ...]) -> CommandCapture:
        return CommandCapture(
            ("validated-binary", *suffix),
            0,
            json.dumps(
                {"agents": [{"prompt": "raw prompt", "namespace": "tenant-red"}]}
            ),
            "prompt=another raw prompt\nnamespace=tenant-green",
            False,
            None,
        )

    monkeypatch.setattr("tests.characterization.support.run_command", fake_run)
    contract = collect_live_contract(capability)
    serialized = json.dumps(contract)
    assert "raw prompt" not in serialized
    assert "tenant-red" not in serialized
    assert "tenant-green" not in serialized


def test_sanitize_text_handles_empty_user_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tests.characterization.support.getpass.getuser", lambda: "")
    assert sanitize_text("safe output") == "safe output"


def test_fingerprint_is_deterministic_and_ignores_key_order() -> None:
    left = {"commands": [{"argv": ["claude", "--help"]}], "version": "1"}
    right = {"version": "1", "commands": [{"argv": ["claude", "--help"]}]}
    assert contract_fingerprint(left) == contract_fingerprint(right)
    assert len(contract_fingerprint(left)) == 64


def test_extract_required_contract_finds_advertised_flags_and_commands() -> None:
    help_text = """
    Commands:
      attach logs stop respawn rm agents
    Options:
      --bg --session-id ID --name NAME --permission-mode MODE
      --plugin-dir PATH --model MODEL --effort LEVEL
    """
    required = extract_required_contract(help_text)
    assert required["commands"] == ["agents", "attach", "logs", "respawn", "rm", "stop"]
    assert required["flags"] == [
        "--bg",
        "--effort",
        "--model",
        "--name",
        "--permission-mode",
        "--plugin-dir",
        "--session-id",
    ]


def test_atomic_write_contract_redacts_colon_labeled_captures(tmp_path: Path) -> None:
    destination = tmp_path / "contract.json"
    contract = {
        "capture": CommandCapture(
            ("claude", "--help"),
            0,
            "PrOmPt: attack phrase\nsafe stdout line",
            "nAmEsPaCe: tenant-xyz\nsafe stderr line",
            False,
            None,
        )
    }

    atomic_write_contract(destination, contract)

    serialized = destination.read_text()
    written = json.loads(serialized)
    assert "attack phrase" not in serialized
    assert "tenant-xyz" not in serialized
    assert written["capture"]["stdout"] == "PrOmPt: <REDACTED_PROMPT>\nsafe stdout line"
    assert written["capture"]["stderr"] == "nAmEsPaCe: <NAMESPACE>\nsafe stderr line"


def test_atomic_contract_output_is_sanitized_and_complete(tmp_path: Path) -> None:
    destination = tmp_path / "contract.json"
    contract = {
        "binary": "/Users/ryo/bin/claude",
        "capture": CommandCapture(("claude", "--help"), 0, "ryo", "", False, None),
        "prompt": "unique prompt",
    }
    atomic_write_contract(
        destination,
        sanitize_contract(contract, username="ryo", sensitive_values=["unique prompt"]),
    )
    written = json.loads(destination.read_text())
    assert written["fingerprint"] == contract_fingerprint(
        {k: v for k, v in written.items() if k != "fingerprint"}
    )
    serialized = destination.read_text()
    assert "/Users/ryo" not in serialized
    assert "ryo" not in serialized
    assert "unique prompt" not in serialized
    assert not list(tmp_path.glob(".contract.json.*.tmp"))


def test_observer_plugin_is_non_deciding_and_observer_never_emits_output(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    evidence = tmp_path / "evidence"
    _create_observer_plugin(plugin, evidence)

    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {"SessionStart", "SessionEnd", "Notification"}
    assert OBSERVER_EVENT_MATCHERS == (
        "SessionStart",
        "SessionEnd",
        "permission_prompt",
        "idle_prompt",
        "agent_needs_input",
        "agent_completed",
    )
    serialized = json.dumps(hooks)
    for forbidden in (
        "PermissionRequest",
        "PermissionDenied",
        "PreToolUse",
        "Stop",
        "SubagentStop",
        "WorktreeCreate",
        "WorktreeRemove",
        "decision",
        "continue",
    ):
        assert forbidden not in serialized

    observer = plugin / "hooks" / "observer.py"
    completed = subprocess.run(
        [sys.executable, str(observer), "SessionStart"],
        input='{"prompt":"must not persist"}',
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CLAUDE_OBSERVER_EVIDENCE_DIR": str(evidence)},
    )
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    stored = json.loads(next(evidence.glob("*.json")).read_text())
    assert stored["event"] == "SessionStart"
    assert stored["input_bytes"] > 0
    assert "must not persist" not in json.dumps(stored)

    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("occupied")
    unavailable_run = subprocess.run(
        [sys.executable, str(observer), "SessionEnd"],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CLAUDE_OBSERVER_EVIDENCE_DIR": str(unavailable)},
    )
    assert unavailable_run.returncode == 0
    assert unavailable_run.stdout == ""
    assert unavailable_run.stderr == ""


def test_unattended_probe_isolates_exact_uuid_cases_and_waits_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "unattended-hooks.json"
    calls: list[tuple[str, ...]] = []
    active: dict[str, str] = {}

    def fake_capability(capability: object) -> Path:
        assert capability is marker
        return Path(EXACT_CLAUDE_BIN)

    def fake_owned_run(
        binary: Path,
        owned: object,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        assert binary == Path(EXACT_CLAUDE_BIN)
        assert cwd.name == "repository"
        assert timeout >= 0
        calls.append(argv)
        if argv[1] == "--bg":
            name = argv[3]
            session_id = f"{uuid.uuid4()}"
            active[session_id] = f"completed:{name}"
            output = (
                "Starting background service…\n"
                f"backgrounded · {session_id[:8]} · {name}\n"
            )
            return CommandCapture(argv, 0, output, "", False, None)
        elif argv[1] == "stop":
            session_id = next(key for key in active if key.startswith(f"{argv[2]}-"))
            active.pop(session_id)
        if argv[1:3] == ("agents", "--json"):
            rows = [
                {
                    "id": session_id[:8],
                    "sessionId": session_id,
                    "name": state.split(":", 1)[1],
                    "state": state.split(":", 1)[0],
                }
                for session_id, state in active.items()
            ]
            return CommandCapture(argv, 0, json.dumps(rows), "", False, None)
        return CommandCapture(argv, 0, "", "", False, None)

    marker = object()
    monkeypatch.setattr(
        "tests.characterization.support._require_exact_observer_capability",
        fake_capability,
    )
    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        fake_owned_run,
    )
    monkeypatch.setattr(
        "tests.characterization.support._observe_exact_owned",
        lambda binary, owned, cwd, evidence, deadline: (False, False, set()),
    )

    result = collect_unattended_observer_hook_characterization(marker, artifact)

    launches = [argv for argv in calls if argv[1] == "--bg"]
    stops = [argv for argv in calls if argv[1] == "stop"]
    assert len(launches) == len(OBSERVER_EVENT_MATCHERS[2:])
    assert len(stops) == len(launches)
    assert result["estimated_test_owned_session_count"] == len(launches)
    assert result["actual_test_owned_session_count"] == len(launches)
    assert result["cleanup"]["confirmed"] is True
    assert {probe["matcher"] for probe in result["probes"]} == set(
        OBSERVER_EVENT_MATCHERS[2:]
    )
    assert all("owned_display_id" not in probe for probe in result["probes"])
    assert all("owned_session_id" not in probe for probe in result["probes"])
    assert all(probe["launch_token_reconciled"] is True for probe in result["probes"])
    for launch, stop in zip(launches, stops, strict=True):
        launch_index = next(i for i, call in enumerate(calls) if call is launch)
        stop_index = next(i for i, call in enumerate(calls) if call is stop)
        assert launch[3].startswith("observer-")
        assert len(stop[2]) == 8
        assert "-p" not in launch
        assert launch_index < stop_index
        assert any(
            call[1:3] == ("agents", "--json") and launch_index < index < stop_index
            for index, call in enumerate(calls)
        )
    written = artifact.read_text()
    assert str(tmp_path) not in written
    assert 'fully_characterized": true' not in written


def test_owned_session_identity_is_a_uuid_separate_from_display_name() -> None:
    session_id = _owned_session_id()
    assert str(uuid.UUID(session_id)) == session_id
    assert not session_id.startswith("observer-")


def test_launch_display_id_requires_exact_owned_background_response() -> None:
    owned = _mint_owned_probe("idle_prompt")
    output = (
        "Starting background service…\n"
        f"backgrounded · 11cc4577 · {owned.display_name}\n"
        "  claude stop 11cc4577      stop this session"
    )
    assert _launch_display_id(output, owned) == "11cc4577"
    assert _launch_display_id(output.replace("11cc4577", "not-an-id"), owned) is None
    assert (
        _launch_display_id(output.replace(owned.display_name, "unrelated"), owned)
        is None
    )


@pytest.mark.parametrize(
    "failure", ["identity_timeout", "identity_ambiguity", "observer"]
)
def test_every_post_launch_failure_still_stops_exact_launch_tokens(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    unrelated_id = "aaaaaaaa-1111-4111-8111-111111111111"
    rows: dict[str, dict[str, str]] = {
        unrelated_id: {
            "id": "aaaaaaaa",
            "sessionId": unrelated_id,
            "name": "unrelated",
            "state": "blocked",
        }
    }

    def fake_run(
        binary: Path,
        owned: object,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        calls.append(argv)
        if argv[1] == "--bg":
            session_id = str(uuid.uuid4())
            rows[session_id] = {
                "id": session_id[:8],
                "sessionId": session_id,
                "name": argv[3],
                "state": "blocked",
            }
            output = f"backgrounded · {session_id[:8]} · {argv[3]}\n"
            return CommandCapture(argv, 0, output, "", False, None)
        if argv[1] == "stop":
            match = next(row for row in rows.values() if row["id"] == argv[2])
            match["state"] = "stopped"
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1:3] == ("agents", "--json"):
            return CommandCapture(
                argv, 0, json.dumps(list(rows.values())), "", False, None
            )
        raise AssertionError(argv)

    monkeypatch.setattr(
        "tests.characterization.support._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        fake_run,
    )
    if failure == "identity_timeout":
        monkeypatch.setattr(
            "tests.characterization.support._discover_native_identity",
            lambda *args: None,
        )
    elif failure == "identity_ambiguity":

        def ambiguous(*args: object) -> None:
            raise CharacterizationError("ambiguous native identity")

        monkeypatch.setattr(
            "tests.characterization.support._discover_native_identity",
            ambiguous,
        )
    else:

        def observer_failure(*args: object) -> None:
            raise OSError("test-owned evidence unavailable")

        monkeypatch.setattr(
            "tests.characterization.support._observe_exact_owned",
            observer_failure,
        )

    with pytest.raises(CharacterizationError):
        collect_unattended_observer_hook_characterization(
            object(), tmp_path / f"{failure}.json"
        )

    launches = [argv for argv in calls if argv[1] == "--bg"]
    stops = [argv for argv in calls if argv[1] == "stop"]
    assert len(launches) == len(OBSERVER_EVENT_MATCHERS[2:])
    assert len(stops) == len(launches)
    assert {argv[2] for argv in stops} == {
        row["id"] for row in rows.values() if row["name"].startswith("observer-")
    }
    assert rows[unrelated_id]["state"] == "blocked"
    assert all(
        row["state"] == "stopped"
        for row in rows.values()
        if row["name"].startswith("observer-")
    )


def test_probe_fails_without_identity_readback_and_never_guesses_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(
        binary: Path,
        owned: object,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        calls.append(argv)
        if argv[1:3] == ("agents", "--json"):
            return CommandCapture(argv, 0, "[]", "", False, None)
        return CommandCapture(argv, 0, "", "", False, None)

    monkeypatch.setattr(
        "tests.characterization.support._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        fake_run,
    )
    monkeypatch.setattr(
        "tests.characterization.support.OBSERVER_SUITE_TIMEOUT_SECONDS",
        0.5,
    )
    started = time.monotonic()
    with pytest.raises(CharacterizationError, match="failed identity read-back"):
        collect_unattended_observer_hook_characterization(
            object(), tmp_path / "out.json"
        )
    assert time.monotonic() - started < 1
    assert not [argv for argv in calls if argv[1] == "stop"]


def test_observer_plugin_quotes_paths_with_shell_metacharacters(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin with spaces;$(nope)"
    evidence = tmp_path / "evidence with spaces;$(nope)"
    _create_observer_plugin(plugin, evidence)
    command = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"][
        "SessionStart"
    ][0]["hooks"][0]["command"]
    completed = subprocess.run(
        command, shell=True, input="{}", capture_output=True, text=True
    )
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert list(evidence.glob("*.json"))


def test_owned_runner_rejects_arbitrary_observer_prefix_stop(tmp_path: Path) -> None:
    owned = _mint_owned_probe("idle_prompt")
    with pytest.raises(CharacterizationError, match="probe-owned"):
        _run_owned_observer_command(
            Path(EXACT_CLAUDE_BIN),
            owned,
            (EXACT_CLAUDE_BIN, "stop", "observer-not-owned"),
            tmp_path,
            1,
        )


def test_duplicate_owned_name_is_ambiguous() -> None:
    owned = _mint_owned_probe("idle_prompt")
    rows = [
        {"id": "11111111", "sessionId": str(uuid.uuid4()), "name": owned.display_name},
        {"id": "22222222", "sessionId": str(uuid.uuid4()), "name": owned.display_name},
    ]
    assert len(_owned_rows(rows, owned)) == 2


@pytest.mark.parametrize(
    "session_id",
    [
        "deadbeef-not-a-uuid",
        "cafebabe-1111-4111-8111-111111111111",
        None,
    ],
)
def test_identity_readback_rejects_noncanonical_or_missing_full_uuid(
    session_id: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = _mint_owned_probe("idle_prompt")
    owned = type(owned)(
        matcher=owned.matcher,
        display_name=owned.display_name,
        display_id="deadbeef",
    )
    row = {
        "id": "deadbeef",
        "sessionId": session_id,
        "name": owned.display_name,
        "state": "running",
    }

    monkeypatch.setattr(
        "tests.characterization.support._listing",
        lambda *args: CommandCapture(
            (EXACT_CLAUDE_BIN, "agents", "--json", "--all"),
            0,
            json.dumps([row]),
            "",
            False,
            None,
        ),
    )
    monkeypatch.setattr("tests.characterization.support.time.sleep", lambda _: None)

    assert (
        _discover_native_identity(
            Path(EXACT_CLAUDE_BIN),
            owned,
            tmp_path,
            {},
            time.monotonic() + 0.01,
        )
        is None
    )


def test_cleanup_rejects_duplicate_exact_nonterminal_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "deadbeef-1111-4111-8111-111111111111"
    owned = _mint_owned_probe("idle_prompt")
    owned = type(owned)(
        matcher=owned.matcher,
        display_name=owned.display_name,
        session_id=session_id,
        display_id="deadbeef",
    )
    duplicate = {
        "id": owned.display_id,
        "sessionId": owned.session_id,
        "name": owned.display_name,
        "state": "blocked",
    }

    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        lambda binary, owned, argv, cwd, timeout: CommandCapture(
            argv, 0, "", "", False, None
        ),
    )
    monkeypatch.setattr(
        "tests.characterization.support._listing",
        lambda *args: CommandCapture(
            (EXACT_CLAUDE_BIN, "agents", "--json", "--all"),
            0,
            json.dumps([duplicate, duplicate]),
            "",
            False,
            None,
        ),
    )
    monkeypatch.setattr("tests.characterization.support.time.sleep", lambda _: None)

    confirmed, _, _ = _cleanup_owned(
        Path(EXACT_CLAUDE_BIN), owned, tmp_path, time.monotonic() + 0.01
    )

    assert confirmed is False


def test_cleanup_accepts_exact_done_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "deadbeef-1111-4111-8111-111111111111"
    owned = _OwnedProbe(
        "observer-done-terminal",
        "idle_prompt",
        session_id,
        "deadbeef",
    )
    terminal = {
        "id": owned.display_id,
        "sessionId": owned.session_id,
        "name": owned.display_name,
        "state": "done",
    }
    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        lambda binary, owned, argv, cwd, timeout: CommandCapture(
            argv, 0, "", "", False, None
        ),
    )
    monkeypatch.setattr(
        "tests.characterization.support._listing",
        lambda *args: CommandCapture(
            (EXACT_CLAUDE_BIN, "agents", "--json", "--all"),
            0,
            json.dumps([terminal]),
            "",
            False,
            None,
        ),
    )

    confirmed, _, _ = _cleanup_owned(
        Path(EXACT_CLAUDE_BIN), owned, tmp_path, time.monotonic() + 1
    )

    assert confirmed is True


def test_probe_fails_when_cleanup_listing_has_duplicate_exact_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[str, dict[str, str]] = {}
    stopped: set[str] = set()
    calls: list[tuple[str, ...]] = []

    def fake_run(
        binary: Path,
        owned: object,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        calls.append(argv)
        if argv[1] == "--bg":
            session_id = str(uuid.uuid4())
            rows[session_id] = {
                "id": session_id[:8],
                "sessionId": session_id,
                "name": argv[3],
                "state": "blocked",
            }
            return CommandCapture(
                argv,
                0,
                f"backgrounded · {session_id[:8]} · {argv[3]}\n",
                "",
                False,
                None,
            )
        if argv[1] == "stop":
            stopped.add(argv[2])
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1:3] == ("agents", "--json"):
            listed = [
                row
                for row in rows.values()
                for _ in range(2 if row["id"] in stopped else 1)
            ]
            return CommandCapture(argv, 0, json.dumps(listed), "", False, None)
        raise AssertionError(argv)

    monkeypatch.setattr(
        "tests.characterization.support._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        fake_run,
    )
    monkeypatch.setattr(
        "tests.characterization.support._observe_exact_owned",
        lambda binary, owned, cwd, evidence, deadline: (True, False, set()),
    )
    monkeypatch.setattr("tests.characterization.support.OBSERVER_CLEANUP_SECONDS", 0.01)
    monkeypatch.setattr("tests.characterization.support.time.sleep", lambda _: None)

    with pytest.raises(CharacterizationError, match="cleanup could not be confirmed"):
        collect_unattended_observer_hook_characterization(
            object(), tmp_path / "duplicates.json"
        )

    stops = [argv for argv in calls if argv[1] == "stop"]
    assert len(stops) == len(OBSERVER_EVENT_MATCHERS[2:])
    assert {argv[2] for argv in stops} == {row["id"] for row in rows.values()}


def test_post_launch_identity_timeout_preserves_cleanup_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 0.0
    calls: list[tuple[tuple[str, ...], float, float]] = []
    stopped: list[str] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        assert seconds >= 0
        now += seconds

    def fake_run(
        binary: Path,
        owned: object,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        assert timeout >= 0
        calls.append((argv, timeout, now))
        if argv[1] == "--bg":
            return CommandCapture(
                argv,
                0,
                f"backgrounded · deadbeef · {argv[3]}\n",
                "",
                False,
                None,
            )
        if argv[1] == "stop":
            stopped.append(argv[2])
        if argv[1:3] == ("agents", "--json"):
            return CommandCapture(argv, 0, "[]", "", False, None)
        return CommandCapture(argv, 0, "", "", False, None)

    monkeypatch.setattr(
        "tests.characterization.support._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        fake_run,
    )
    monkeypatch.setattr(
        "tests.characterization.support._run_command_within_total_budget",
        lambda argv, total_timeout_seconds, cwd=None: CommandCapture(
            tuple(argv), 0, "", "", False, None
        ),
    )
    monkeypatch.setattr(
        "tests.characterization.support.OBSERVER_SUITE_TIMEOUT_SECONDS", 13.0
    )
    monkeypatch.setattr("tests.characterization.support.time.monotonic", monotonic)
    monkeypatch.setattr("tests.characterization.support.time.sleep", sleep)

    with pytest.raises(CharacterizationError, match="failed identity read-back"):
        collect_unattended_observer_hook_characterization(
            object(), tmp_path / "timeout.json"
        )

    launches = [argv for argv, _, _ in calls if argv[1] == "--bg"]
    assert len(launches) == len(OBSERVER_EVENT_MATCHERS[2:])
    assert stopped == ["deadbeef"] * len(launches)
    assert now <= 13.0
    assert all(
        0 <= timeout <= min(OBSERVER_TIMEOUT_SECONDS, 13.0 - called_at)
        for _, timeout, called_at in calls
    )


@pytest.mark.skipif(os.name != "posix", reason="signal behavior is POSIX-specific")
def test_hard_command_deadlines_leave_time_for_all_exact_observer_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens: dict[str, str] = {}
    stopped: list[str] = []

    def token_for(name: str) -> str:
        if name not in tokens:
            tokens[name] = f"deadbee{len(tokens)}"
        return tokens[name]

    def fake_run(
        binary: Path,
        owned: _OwnedProbe,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        del binary, cwd, timeout
        display_name = owned.display_name
        display_id = owned.display_id
        session_id = owned.session_id
        if argv[1] == "--bg":
            token = token_for(display_name)
            return CommandCapture(
                argv,
                0,
                f"backgrounded · {token} · {display_name}\n",
                "",
                False,
                None,
            )
        if argv[1] == "stop":
            stopped.append(argv[2])
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1:3] == ("agents", "--json"):
            rows = []
            if display_id is not None and session_id is None:
                rows = [
                    {
                        "id": display_id,
                        "sessionId": f"{display_id}-0000-4000-8000-000000000000",
                        "name": display_name,
                        "state": "running",
                    }
                ]
            return CommandCapture(argv, 0, json.dumps(rows), "", False, None)
        raise AssertionError(argv)

    def slow_observation(*_: object) -> tuple[bool, bool, set[str]]:
        capture = _run_command(
            [
                sys.executable,
                "-c",
                (
                    "import signal, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(30)"
                ),
            ],
            timeout_seconds=0.05,
            overall_timeout_seconds=0.1,
        )
        assert capture.timed_out is True
        return True, False, set()

    monkeypatch.setattr(
        "tests.characterization.support._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "tests.characterization.support._run_owned_observer_command",
        fake_run,
    )
    monkeypatch.setattr(
        "tests.characterization.support._observe_exact_owned",
        slow_observation,
    )
    monkeypatch.setattr(
        "tests.characterization.support.OBSERVER_SUITE_TIMEOUT_SECONDS", 2.0
    )
    monkeypatch.setattr("tests.characterization.support.OBSERVER_CLEANUP_SECONDS", 0.2)

    started = time.monotonic()
    result = collect_unattended_observer_hook_characterization(
        object(), tmp_path / "hard-deadline.json"
    )
    elapsed = time.monotonic() - started

    assert result["actual_test_owned_session_count"] == 4
    assert result["cleanup"]["confirmed"] is True
    assert stopped == list(tokens.values())
    assert elapsed < 2.0


def test_live_fixture_contains_no_raw_launch_output() -> None:
    fixture = (
        Path(__file__).parent
        / "characterization"
        / "unattended_observer_hooks.live.fixture.json"
    )
    payload = json.loads(fixture.read_text())
    assert payload["actual_test_owned_session_count"] == 4
    assert payload["cleanup"]["confirmed"] is True
    assert payload["unrelated_agents"]["unchanged"] is True
    statuses = {entry["event"]: entry["status"] for entry in payload["events"]}
    assert statuses["SessionStart"] == "observed"
    assert statuses["SessionEnd"] == "observed"
    assert {statuses[event] for event in OBSERVER_EVENT_MATCHERS[2:]} == {
        "probe_inconclusive"
    }
    assert all("launch_stderr" not in probe for probe in payload["probes"])
    assert all("launch_stdout" not in probe for probe in payload["probes"])
    assert all("owned_display_id" not in probe for probe in payload["probes"])
    assert all("owned_session_id" not in probe for probe in payload["probes"])
    assert all(probe["launch_token_reconciled"] is True for probe in payload["probes"])


def test_unattended_fixture_is_schema_only_not_synthetic_live_evidence() -> None:
    fixture = (
        Path(__file__).parent
        / "characterization"
        / "unattended_observer_hooks.fixture.json"
    )
    payload = json.loads(fixture.read_text())
    assert payload["source"] == "deterministic_schema_fixture_no_live_probe"
    assert payload["estimated_test_owned_session_count"] == len(
        OBSERVER_EVENT_MATCHERS[2:]
    )
    assert [entry["event"] for entry in payload["events"]] == list(
        OBSERVER_EVENT_MATCHERS
    )
    assert payload["fully_characterized"] is False
    assert payload["raw_evidence_retained"] is False
    assert {entry["status"] for entry in payload["events"]} == {"probe_inconclusive"}
