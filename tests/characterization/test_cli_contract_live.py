"""Opt-in characterization of explicitly safe Claude CLI surface area."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

import pytest

from tests.characterization.support import (
    REQUIRED_COMMANDS,
    REQUIRED_FLAGS,
    AcknowledgedClaudeBinary,
    atomic_write_contract,
    collect_live_contract,
    validate_agents_json_payload,
    validate_prerequisites,
)


def _live_binary_or_skip() -> AcknowledgedClaudeBinary:
    try:
        return validate_prerequisites(os.environ)
    except Exception as error:
        pytest.skip(f"live Claude characterization is opt-in: {error}")


def test_non_mutating_cli_contract(tmp_path: Path) -> None:
    """Capture only help/status/list commands; never perform session mutation."""
    binary = _live_binary_or_skip()
    contract = collect_live_contract(binary)
    atomic_write_contract(tmp_path / "contract.json", contract)

    required = contract["required_contract"]
    assert set(REQUIRED_FLAGS) <= set(required["flags"])
    assert set(REQUIRED_COMMANDS) <= set(required["commands"])
    assert "composition_proven" not in contract
    for capture in contract["commands"]:
        if capture["argv"][1:] != ["daemon", "status"]:
            assert capture["returncode"] == 0
            assert capture["timed_out"] is False
            assert capture["error"] is None

    agents_capture = next(
        capture
        for capture in contract["commands"]
        if capture["argv"][1:] == ["agents", "--json", "--all"]
    )
    assert agents_capture["returncode"] == 0
    assert agents_capture["timed_out"] is False
    assert agents_capture["error"] is None
    agents_payload = json.loads(agents_capture["stdout"])
    validate_agents_json_payload(agents_payload)

    serialized = (tmp_path / "contract.json").read_text()
    assert "/Users/ryo" not in serialized
    username = getpass.getuser()
    if username:
        assert username not in serialized
