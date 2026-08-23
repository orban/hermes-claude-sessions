from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    CharacterizationError,
    CommandCapture,
    _worktree_launch_argv,
    _WorktreeProbe,
    collect_disposable_worktree_edit_characterization,
)


def _runner(
    rows: dict[str, dict[str, str]],
    calls: list[tuple[str, ...]],
    duplicate: bool = False,
):
    def run(
        binary: Path,
        probe: _WorktreeProbe,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        del binary, cwd, timeout
        calls.append(argv)
        if argv[1] == "--bg":
            rows[probe.session_id] = {
                "id": probe.session_id[:8],
                "sessionId": probe.session_id,
                "name": probe.display_name,
                "state": "completed",
            }
            return CommandCapture(
                argv,
                0,
                f"backgrounded · {probe.session_id[:8]} · {probe.display_name}\n",
                "",
                False,
                None,
            )
        if argv[1] == "stop":
            rows[probe.session_id]["state"] = "stopped"
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1:3] == ("agents", "--json"):
            items = list(rows.values())
            if duplicate and probe.session_id in rows:
                items.append(dict(rows[probe.session_id]))
            return CommandCapture(argv, 0, json.dumps(items), "", False, None)
        raise AssertionError(argv)

    return run


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, dict[str, str]],
    calls: list[tuple[str, ...]],
    duplicate: bool = False,
) -> None:
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_worktree_command",
        _runner(rows, calls, duplicate),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._worktree_snapshot",
        lambda *args: "worktree /private/tmp/main\n",
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._initialize_seed_repository",
        lambda repository, deadline: (repository / "seed.txt").write_text("seed\n"),
    )


def test_exact_argv_accept_edits_and_prompt_scope() -> None:
    probe = _WorktreeProbe("worktree-" + str(uuid.uuid4()), str(uuid.uuid4()))
    argv = _worktree_launch_argv(Path(EXACT_CLAUDE_BIN), probe, Path("/plugin"))
    assert argv[:7] == (
        EXACT_CLAUDE_BIN,
        "--bg",
        "--name",
        probe.display_name,
        "--permission-mode",
        "acceptEdits",
        "--plugin-dir",
    )
    assert len(argv) == 9 and "--session-id" not in argv
    assert "seed.txt" in argv[-1] and "no action elsewhere" in argv[-1].lower()


def test_identity_finally_cleanup_sanitization_and_unrelated_preservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = "aaaaaaaa-1111-4111-8111-111111111111"
    rows = {
        unrelated: {
            "id": "aaaaaaaa",
            "sessionId": unrelated,
            "name": "unrelated",
            "state": "blocked",
        }
    }
    calls: list[tuple[str, ...]] = []
    _configure(monkeypatch, rows, calls)
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._observe_worktree_probe",
        lambda *args: {
            "effective_cwd": "/private/tmp/worktree",
            "effective_cwd_source": "hook",
            "worktree_path": "/private/tmp/worktree",
            "git_branch": "claude/probe",
            "requested_edit_landed": True,
            "primary_unchanged": True,
            "diff": "+CHARACTERIZATION_EDIT\n",
        },
    )
    artifact = tmp_path / "artifact.json"
    result = collect_disposable_worktree_edit_characterization(object(), artifact)
    written = artifact.read_text()
    assert result["cleanup"]["confirmed"] and result["unrelated_agents"]["unchanged"]
    assert rows[unrelated]["state"] == "blocked"
    assert "/private/tmp" not in written and unrelated not in written
    assert [call[1] for call in calls if call[1] in {"--bg", "stop"}] == [
        "--bg",
        "stop",
    ]


@pytest.mark.parametrize("fault", ["identity", "observation"])
def test_finally_stops_exact_token_after_timeout_or_observation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    _configure(monkeypatch, rows, calls)
    if fault == "identity":
        monkeypatch.setattr(
            "hermes_claude_sessions.characterization._discover_worktree_identity",
            lambda *args: None,
        )
    else:
        monkeypatch.setattr(
            "hermes_claude_sessions.characterization._observe_worktree_probe",
            lambda *args: (_ for _ in ()).throw(TimeoutError()),
        )
    with pytest.raises(CharacterizationError):
        collect_disposable_worktree_edit_characterization(
            object(), tmp_path / "failure.json"
        )
    stops = [call for call in calls if call[1] == "stop"]
    assert len(stops) == 1 and len(stops[0][2]) == 8


def test_no_token_and_duplicate_identity_fail_without_broad_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    _configure(monkeypatch, rows, calls, duplicate=True)
    with pytest.raises(CharacterizationError, match="ambiguous"):
        collect_disposable_worktree_edit_characterization(
            object(), tmp_path / "duplicate.json"
        )
    assert len([call for call in calls if call[1] == "stop"]) == 1
