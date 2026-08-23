from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    WORKTREE_DIFF_LIMIT,
    CharacterizationError,
    CommandCapture,
    _discover_worktree_identity,
    _mint_worktree_probe,
    _observe_worktree_probe,
    _worktree_launch_argv,
    _WorktreeProbe,
    collect_disposable_worktree_edit_characterization,
    contract_fingerprint,
)


def _runner(
    rows: dict[str, dict[str, str]],
    calls: list[tuple[str, ...]],
    duplicate: bool = False,
):
    native_session_id = str(uuid.uuid4())

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
            rows[native_session_id] = {
                "id": native_session_id[:8],
                "sessionId": native_session_id,
                "name": probe.display_name,
                "state": "completed",
            }
            return CommandCapture(
                argv,
                0,
                f"backgrounded · {native_session_id[:8]} · {probe.display_name}\n",
                "",
                False,
                None,
            )
        if argv[1] == "stop":
            rows[native_session_id]["state"] = "stopped"
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1:3] == ("agents", "--json"):
            items = list(rows.values())
            if duplicate and native_session_id in rows:
                items.append(dict(rows[native_session_id]))
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


def test_minted_probe_has_no_guessed_native_session_id() -> None:
    probe = _mint_worktree_probe()
    assert probe.session_id is None


def test_identity_reconciliation_requires_launch_returned_short_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    full = str(uuid.uuid4())
    probe = _WorktreeProbe("worktree-exact-name", None, "deadbeef")

    def mismatched_listing(*args) -> CommandCapture:
        argv = args[2]
        return CommandCapture(
            argv,
            0,
            json.dumps(
                [
                    {
                        "id": full[:8],
                        "sessionId": full,
                        "name": probe.display_name,
                        "state": "running",
                    }
                ]
            ),
            "",
            False,
            None,
        )

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_worktree_command",
        mismatched_listing,
    )
    discovered = _discover_worktree_identity(
        Path(EXACT_CLAUDE_BIN),
        probe,
        tmp_path,
        {},
        time.monotonic() + 0.02,
    )
    assert discovered is None


def test_observation_polls_until_exact_native_session_is_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    full = str(uuid.uuid4())
    probe = _WorktreeProbe("worktree-poll", full, full[:8])
    states = iter(("running", "completed"))
    calls = 0

    def listing(*args) -> CommandCapture:
        nonlocal calls
        calls += 1
        argv = args[2]
        state = next(states)
        return CommandCapture(
            argv,
            0,
            json.dumps(
                [
                    {
                        "id": full[:8],
                        "sessionId": full,
                        "name": probe.display_name,
                        "state": state,
                    }
                ]
            ),
            "",
            False,
            None,
        )

    (tmp_path / "seed.txt").write_text("seed\n")
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_worktree_command", listing
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._worktree_snapshot",
        lambda *args: f"worktree {tmp_path}\n",
    )
    result = _observe_worktree_probe(
        Path(EXACT_CLAUDE_BIN),
        probe,
        tmp_path,
        f"worktree {tmp_path}\n",
        tmp_path / "evidence",
        time.monotonic() + 1,
    )
    assert calls == 2
    assert result["native_terminal_observed"] is True
    assert result["probe_status"] == "not_observed"


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


def test_post_stop_artifact_preservation_is_observed_not_inferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    _configure(monkeypatch, rows, calls)
    snapshots = iter(
        (
            "worktree /private/tmp/main\n",
            "worktree /private/tmp/main\n"
            "worktree /private/tmp/owned\n"
            "branch refs/heads/probe\n",
            "worktree /private/tmp/main\n",
        )
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._worktree_snapshot",
        lambda *args: next(snapshots),
    )
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "seed.txt").write_text("seed\nCHARACTERIZATION_EDIT\n")
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._worktree_paths",
        lambda porcelain: (
            {str(tmp_path / "main"): None, str(owned): "refs/heads/probe"}
            if "owned" in porcelain
            else {str(tmp_path / "main"): None}
        ),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_git",
        lambda argv, *args: CommandCapture(
            argv, 0, "+CHARACTERIZATION_EDIT\n", "", False, None
        ),
    )

    result = collect_disposable_worktree_edit_characterization(
        object(), tmp_path / "artifact.json"
    )

    assert result["worktree"]["separate_worktree_created"] is True
    assert result["stop_preserved_artifact"] is False


def test_observed_diff_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = str(uuid.uuid4())
    probe = _WorktreeProbe("worktree-diff", full, full[:8])
    owned = tmp_path / "owned"
    owned.mkdir()
    (tmp_path / "seed.txt").write_text("seed\n")
    (owned / "seed.txt").write_text("seed\nCHARACTERIZATION_EDIT\n")

    def listing(*args) -> CommandCapture:
        argv = args[2]
        return CommandCapture(
            argv,
            0,
            json.dumps(
                [
                    {
                        "id": full[:8],
                        "sessionId": full,
                        "name": probe.display_name,
                        "state": "completed",
                    }
                ]
            ),
            "",
            False,
            None,
        )

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_worktree_command", listing
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._worktree_snapshot",
        lambda *args: (
            f"worktree {tmp_path}\nworktree {owned}\nbranch refs/heads/probe\n"
        ),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_git",
        lambda argv, *args: CommandCapture(
            argv, 0, "x" * (WORKTREE_DIFF_LIMIT + 100), "", False, None
        ),
    )
    result = _observe_worktree_probe(
        Path(EXACT_CLAUDE_BIN),
        probe,
        tmp_path,
        f"worktree {tmp_path}\n",
        tmp_path / "evidence",
        time.monotonic() + 1,
    )
    assert len(result["diff"]) == WORKTREE_DIFF_LIMIT


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


def test_missing_launch_token_fails_without_guessing_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    runner = _runner(rows, calls)
    _configure(monkeypatch, rows, calls)

    def no_token(*args) -> CommandCapture:
        capture = runner(*args)
        if args[2][1] == "--bg":
            return CommandCapture(capture.argv, 0, "", "", False, None)
        return capture

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_worktree_command",
        no_token,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._preserve_worktree_diagnostics",
        lambda root: tmp_path / "preserved",
    )
    with pytest.raises(CharacterizationError, match="omitted exact cleanup token"):
        collect_disposable_worktree_edit_characterization(
            object(), tmp_path / "missing-token.json"
        )
    assert not [call for call in calls if call[1] == "stop"]


def test_live_fixture_is_fingerprinted_conservative_and_identity_free() -> None:
    fixture = (
        Path(__file__).parent
        / "characterization"
        / "disposable_worktree_edit.live.fixture.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    fingerprint = payload.pop("fingerprint")
    serialized = json.dumps(payload, sort_keys=True)
    assert fingerprint == contract_fingerprint(payload)
    assert payload["worktree"]["probe_status"] == "probe_inconclusive"
    assert payload["worktree"]["blocked_before_cleanup"] is True
    assert payload["worktree"]["permission_notification_observed"] is False
    assert not re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", serialized)
    assert not re.search(r'"(?:owned_display_id|owned_session_id)"', serialized)
    assert "/Users/" not in serialized and "/private/var/" not in serialized
