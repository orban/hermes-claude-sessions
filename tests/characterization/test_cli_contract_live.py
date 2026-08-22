"""Opt-in characterization of explicitly safe Claude CLI surface area."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    REQUIRED_COMMANDS,
    REQUIRED_FLAGS,
    atomic_write_contract,
    collect_live_contract,
    validate_prerequisites,
)


def _live_binary_or_skip() -> Path:
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

    serialized = (tmp_path / "contract.json").read_text()
    assert "/Users/ryo" not in serialized
    assert os.environ.get("USER", "") not in serialized
