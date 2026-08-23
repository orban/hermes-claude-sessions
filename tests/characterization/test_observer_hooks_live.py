"""Explicitly opt-in live probe for the bounded unattended observer-hook path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    OBSERVER_EVENT_MATCHERS,
    CharacterizationError,
    collect_unattended_observer_hook_characterization,
    validate_prerequisites,
)


def _live_capability_or_skip() -> object:
    try:
        capability = validate_prerequisites(os.environ)
        if os.environ.get("CLAUDE_BIN") != EXACT_CLAUDE_BIN:
            raise CharacterizationError(
                f"CLAUDE_BIN must be exactly {EXACT_CLAUDE_BIN} for observer probing"
            )
        return capability
    except Exception as error:
        pytest.skip(f"live unattended observer characterization is opt-in: {error}")


def test_unattended_observer_hook_characterization(tmp_path: Path) -> None:
    """Probe one exact-owned ``--bg`` session; lack of an event remains inconclusive."""
    capability = _live_capability_or_skip()
    artifact = tmp_path / "unattended-observer-hooks.json"
    result = collect_unattended_observer_hook_characterization(capability, artifact)

    assert artifact.exists()
    assert result["context"] == "unattended_bg"
    assert result["estimated_test_owned_session_count"] == 1
    assert result["cleanup"]["confirmed"] is True
    assert result["fully_characterized"] is False
    assert [entry["event"] for entry in result["events"]] == list(
        OBSERVER_EVENT_MATCHERS
    )
    assert all(
        entry["status"] in {"observed", "not_observed", "probe_inconclusive"}
        for entry in result["events"]
    )
