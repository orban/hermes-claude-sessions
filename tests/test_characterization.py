from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    CharacterizationError,
    CommandCapture,
    atomic_write_contract,
    contract_fingerprint,
    extract_required_contract,
    run_command,
    sanitize_contract,
    sanitize_text,
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


def test_run_command_captures_timeout_and_os_error() -> None:
    timed_out = run_command(
        [
            sys.executable,
            "-c",
            "import time; print('begin', flush=True); time.sleep(2)",
        ],
        timeout_seconds=0.2,
    )
    assert timed_out.timed_out is True
    assert timed_out.returncode is None
    assert "begin" in timed_out.stdout

    missing = run_command(["/definitely/not/a/command"], timeout_seconds=1)
    assert missing.returncode is None
    assert missing.error
    assert "definitely/not/a/command" in missing.error


def test_run_command_binds_output_and_uses_argv() -> None:
    capture = run_command(
        [sys.executable, "-c", "print('x' * 500)"],
        timeout_seconds=1,
        output_limit=20,
    )
    assert capture.returncode == 0
    assert len(capture.stdout) <= 20
    assert capture.argv[0] == sys.executable


def test_sanitization_removes_user_home_temp_namespace_and_prompt() -> None:
    raw = (
        "ryo at /Users/ryo/workspace and /var/folders/ab/cd/T/job-123; "
        "namespace=ns_01JABCDEF1234567890ABCDEF; prompt=secret prompt"
    )
    clean = sanitize_text(raw, username="ryo", sensitive_values=["secret prompt"])
    contract = sanitize_contract(
        {"prompt": "unique prompt", "namespace": "private-namespace"}, username="ryo"
    )
    assert "ryo" not in clean
    assert "/Users/ryo" not in clean
    assert "/var/folders/ab/cd/T/job-123" not in clean
    assert "secret prompt" not in clean
    assert "<HOME>" in clean
    assert "<TEMP_PATH>" in clean
    assert "<NAMESPACE>" in clean
    assert contract["prompt"] == "<REDACTED_PROMPT>"
    assert contract["namespace"] == "<NAMESPACE>"


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
