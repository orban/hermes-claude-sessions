"""Explicitly opt-in live probe for unattended native lifecycle behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    CharacterizationError,
    collect_unattended_lifecycle_characterization,
    validate_prerequisites,
)


def _live_capability_or_skip() -> object:
    try:
        capability = validate_prerequisites(os.environ)
        if os.environ.get("CLAUDE_BIN") != EXACT_CLAUDE_BIN:
            raise CharacterizationError(
                f"CLAUDE_BIN must be exactly {EXACT_CLAUDE_BIN} for lifecycle probing"
            )
        return capability
    except Exception as error:
        pytest.skip(f"live lifecycle characterization is opt-in: {error}")


def test_unattended_lifecycle_characterization(tmp_path: Path) -> None:
    capability = _live_capability_or_skip()
    artifact = tmp_path / "lifecycle.json"
    result = collect_unattended_lifecycle_characterization(capability, artifact)

    assert artifact.exists()
    assert result["attended"] is False
    assert result["fully_characterized"] is False
    assert result["cleanup"]["confirmed"] is True
    assert result["unrelated_agents"]["unchanged"] is True
    for command in ("logs", "stop", "respawn"):
        assert set(result["identifier_acceptance"][command]) == {"short", "full_uuid"}
    assert result["lifecycle"]["stop_transition"] in {
        "observed",
        "not_observed",
        "probe_inconclusive",
    }
