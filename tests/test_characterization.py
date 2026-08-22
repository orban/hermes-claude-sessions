from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    MAX_OUTPUT_BYTES,
    REQUIRED_AGENT_OBJECT_KEYS,
    CharacterizationError,
    CommandCapture,
    _run_command,
    atomic_write_contract,
    collect_live_contract,
    contract_fingerprint,
    extract_required_contract,
    run_command,
    sanitize_contract,
    sanitize_text,
    validate_agents_json_payload,
    validate_prerequisites,
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


def test_run_command_rejects_mutating_suffixes_before_subprocess_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_started(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess creation must not be reached")

    monkeypatch.setattr(subprocess, "Popen", fail_if_started)
    for suffix in (
        ("stop", "session"),
        ("respawn", "session"),
        ("rm", "session"),
        ("launch",),
    ):
        with pytest.raises(CharacterizationError, match="not an allowlisted"):
            run_command([sys.executable, *suffix])


def test_run_command_allows_only_exact_safe_suffixes() -> None:
    capture = run_command([sys.executable, "--version"], timeout_seconds=1)
    assert capture.returncode == 0
    assert capture.argv == (sys.executable, "--version")


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
    binary = tmp_path / "claude"
    binary.write_text("binary")

    def fake_run(argv: list[str]) -> CommandCapture:
        return CommandCapture(
            tuple(argv),
            0,
            json.dumps(
                {"agents": [{"prompt": "raw prompt", "namespace": "tenant-red"}]}
            ),
            "prompt=another raw prompt\nnamespace=tenant-green",
            False,
            None,
        )

    monkeypatch.setattr("hermes_claude_sessions.characterization.run_command", fake_run)
    contract = collect_live_contract(binary)
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
