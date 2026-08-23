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
from pathlib import Path

import pytest

# Task 0B imports are intentionally separate: they specify the bounded,
# opt-in observer-hook path without widening Task 0A's public command runner.
from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    MAX_OUTPUT_BYTES,
    OBSERVER_EVENT_MATCHERS,
    REQUIRED_AGENT_OBJECT_KEYS,
    AcknowledgedClaudeBinary,
    CharacterizationError,
    CommandCapture,
    _create_observer_plugin,
    _run_command,
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
        "from hermes_claude_sessions.characterization import _run_command\n"
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
        "from hermes_claude_sessions.characterization import _run_command\n"
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

    monkeypatch.setattr("hermes_claude_sessions.characterization.run_command", fake_run)
    contract = collect_live_contract(capability)
    serialized = json.dumps(contract)
    assert "raw prompt" not in serialized
    assert "tenant-red" not in serialized
    assert "tenant-green" not in serialized


def test_sanitize_text_handles_empty_user_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.getpass.getuser", lambda: ""
    )
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


def test_unattended_probe_uses_only_generated_owned_session_and_sanitizes_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "unattended-hooks.json"
    calls: list[tuple[str, ...]] = []
    evidence = tmp_path / "fake-evidence"
    evidence.mkdir()
    (evidence / "SessionStart.json").write_text('{"event":"SessionStart"}')

    def fake_capability(capability: object) -> Path:
        assert capability is marker
        return Path(EXACT_CLAUDE_BIN)

    def fake_owned_run(
        binary: Path, argv: tuple[str, ...], cwd: Path
    ) -> CommandCapture:
        assert binary == Path(EXACT_CLAUDE_BIN)
        assert cwd.name == "repository"
        calls.append(argv)
        if argv[1:3] == ("agents", "--json"):
            return CommandCapture(argv, 0, "[]", "", False, None)
        return CommandCapture(argv, 0, "", "", False, None)

    marker = object()
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._require_exact_observer_capability",
        fake_capability,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_observer_command",
        fake_owned_run,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._create_probe_evidence_directory",
        lambda _: evidence,
    )

    result = collect_unattended_observer_hook_characterization(marker, artifact)

    assert artifact.exists()
    assert result["context"] == "unattended_bg"
    assert result["fully_characterized"] is False
    assert result["estimated_test_owned_session_count"] == 1
    assert result["actual_test_owned_session_count"] == 1
    assert result["cleanup"]["confirmed"] is True
    assert any("--bg" in argv for argv in calls)
    stop = next(argv for argv in calls if argv[1] == "stop")
    assert stop[2].startswith("observer-")
    assert all(
        entry["status"] in {"observed", "not_observed", "probe_inconclusive"}
        for entry in result["events"]
    )
    assert (
        next(entry for entry in result["events"] if entry["event"] == "SessionStart")[
            "status"
        ]
        == "observed"
    )
    written = artifact.read_text()
    assert str(tmp_path) not in written
    assert 'fully_characterized": true' not in written


def test_unattended_fixture_is_schema_only_not_synthetic_live_evidence() -> None:
    fixture = (
        Path(__file__).parent
        / "characterization"
        / "unattended_observer_hooks.fixture.json"
    )
    payload = json.loads(fixture.read_text())
    assert payload["source"] == "deterministic_schema_fixture_no_live_probe"
    assert payload["fully_characterized"] is False
    assert payload["raw_evidence_retained"] is False
    assert {entry["status"] for entry in payload["events"]} == {"probe_inconclusive"}
