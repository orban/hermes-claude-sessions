"""Explicitly opt-in live probe for one disposable acceptEdits worktree."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    CharacterizationError,
    collect_disposable_worktree_edit_characterization,
    validate_prerequisites,
)


def _live_capability_or_skip() -> object:
    try:
        capability = validate_prerequisites(os.environ)
        if os.environ.get("CLAUDE_BIN") != EXACT_CLAUDE_BIN:
            raise CharacterizationError(
                f"CLAUDE_BIN must be exactly {EXACT_CLAUDE_BIN} for worktree probing"
            )
        return capability
    except Exception as error:
        pytest.skip(f"live disposable worktree characterization is opt-in: {error}")


def test_disposable_accept_edits_worktree_characterization(tmp_path: Path) -> None:
    capability = _live_capability_or_skip()
    artifact = tmp_path / "disposable-worktree-edit.json"
    result = collect_disposable_worktree_edit_characterization(capability, artifact)

    assert artifact.exists()
    assert result["fully_characterized"] is False
    assert result["estimated_test_owned_session_count"] == 1
    assert result["actual_test_owned_session_count"] == 1
    assert result["launch_token_reconciled"] is True
    assert result["cleanup"]["confirmed"] is True
    assert result["unrelated_agents"]["unchanged"] is True
    assert result["worktree"]["primary_unchanged"] is True
    assert result["worktree"]["probe_status"] in {
        "observed",
        "not_observed",
        "probe_inconclusive",
    }
    assert '"rm"' not in artifact.read_text(encoding="utf-8")
