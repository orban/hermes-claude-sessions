from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    WORKTREE_CLEANUP_SECONDS,
    WORKTREE_DIFF_LIMIT,
    WORKTREE_SUITE_TIMEOUT_SECONDS,
    CharacterizationError,
    CommandCapture,
    _discover_worktree_identity,
    _mint_worktree_probe,
    _observe_worktree_probe,
    _worktree_launch_argv,
    _worktree_paths,
    _WorktreeProbe,
    collect_disposable_worktree_edit_characterization,
    contract_fingerprint,
    sanitize_contract,
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


@pytest.mark.parametrize(
    "porcelain",
    [
        (
            "worktree /Volumes/private-project/owned\n"
            "branch refs/heads/first\n\n"
            "worktree /Volumes/private-project/owned\n"
            "branch refs/heads/second\n"
        ),
        "branch refs/heads/orphan\n",
        "worktree /opt/owned\nbranch refs/heads/first\nbranch refs/heads/second\n",
        "worktree /opt/first\nworktree /opt/second\n",
        "worktree /opt/owned\nbranch \n",
        "worktree /opt/owned\nnot-a-git-record\n",
    ],
)
def test_worktree_porcelain_rejects_duplicate_and_malformed_records(
    porcelain: str,
) -> None:
    with pytest.raises(CharacterizationError, match="worktree porcelain"):
        _worktree_paths(porcelain)


def test_worktree_porcelain_accepts_documented_record_attributes() -> None:
    oid = "a" * 40
    porcelain = (
        f"worktree /opt/normal\nHEAD {oid}\nbranch refs/heads/main\n"
        "locked maintenance\nprunable stale metadata\n\n"
        f"worktree /opt/detached\nHEAD {oid}\ndetached\n\n"
        "worktree /opt/bare\nbare\n"
    )

    assert _worktree_paths(porcelain) == {
        "/opt/normal": "refs/heads/main",
        "/opt/detached": None,
        "/opt/bare": None,
    }


@pytest.mark.parametrize("duplicate_stage", ["before", "after"])
def test_observation_rejects_duplicate_paths_in_either_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate_stage: str
) -> None:
    full = str(uuid.uuid4())
    probe = _WorktreeProbe("worktree-duplicate-path", full, full[:8])
    valid = f"worktree {tmp_path}\n"
    duplicate = valid + f"\nworktree {tmp_path}\n"

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
        lambda *args: duplicate if duplicate_stage == "after" else valid,
    )
    with pytest.raises(CharacterizationError, match="worktree porcelain"):
        _observe_worktree_probe(
            Path(EXACT_CLAUDE_BIN),
            probe,
            tmp_path,
            duplicate if duplicate_stage == "before" else valid,
            tmp_path / "evidence",
            time.monotonic() + 1,
        )


def test_contract_sanitizer_redacts_arbitrary_absolute_paths() -> None:
    sanitized = sanitize_contract(
        {
            "origin_cwd": "/Volumes/private-project/repository",
            "worktree": {
                "worktree_path": "/opt/private-project/worktree",
                "effective_cwd": "/etc/private-project",
                "diff": (
                    "--- /Volumes/private-project/seed.txt\n"
                    "+++ /opt/private-project/seed.txt\n"
                    "metadata: /srv/non-home-root/repository\n"
                ),
            },
        }
    )
    serialized = json.dumps(sanitized, sort_keys=True)
    for leaked in ("/Volumes/", "/opt/", "/etc/", "/srv/"):
        assert leaked not in serialized
    assert sanitized["origin_cwd"] == "<LOCAL_PATH>"


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


def test_sanitized_observation_is_archived_before_cleanup_even_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    events: list[str] = []
    writes: list[dict[str, object]] = []
    _configure(monkeypatch, rows, calls)
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._observe_worktree_probe",
        lambda *args: {
            "probe_status": "probe_inconclusive",
            "worktree_path": "/private/tmp/owned",
            "primary_unchanged": True,
            "diff": "+CHARACTERIZATION_EDIT\n",
        },
    )

    def archive(destination: Path, contract: dict[str, object]) -> None:
        del destination
        events.append("archive")
        writes.append(contract)

    def fail_cleanup(*args) -> bool:
        events.append("cleanup")
        return False

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.atomic_write_contract", archive
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._worktree_cleanup", fail_cleanup
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._preserve_worktree_diagnostics",
        lambda root: tmp_path / "preserved",
    )

    with pytest.raises(CharacterizationError, match="cleanup could not be confirmed"):
        collect_disposable_worktree_edit_characterization(
            object(), tmp_path / "failure.json"
        )

    assert events[:2] == ["archive", "cleanup"]
    assert writes[0]["artifact_phase"] == "pre_cleanup"
    archived_worktree = writes[0]["worktree"]
    assert isinstance(archived_worktree, dict)
    assert archived_worktree["probe_status"] == "probe_inconclusive"
    assert archived_worktree["worktree_path"] == "/private/tmp/owned"
    assert archived_worktree["primary_unchanged"] is True
    assert archived_worktree["diff"] == "+CHARACTERIZATION_EDIT\n"
    assert archived_worktree["primary_unchanged_after_stop"] is None


def test_observation_error_is_archived_before_exact_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    events: list[str] = []
    writes: list[dict[str, object]] = []
    _configure(monkeypatch, rows, calls)
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._observe_worktree_probe",
        lambda *args: (_ for _ in ()).throw(TimeoutError()),
    )

    def archive(destination: Path, contract: dict[str, object]) -> None:
        del destination
        events.append("archive")
        writes.append(contract)

    def confirm_cleanup(*args) -> bool:
        events.append("cleanup")
        return True

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.atomic_write_contract", archive
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._worktree_cleanup", confirm_cleanup
    )

    with pytest.raises(CharacterizationError, match="observation failed: TimeoutError"):
        collect_disposable_worktree_edit_characterization(
            object(), tmp_path / "failure.json"
        )

    assert events[:2] == ["archive", "cleanup"]
    assert writes[0]["artifact_phase"] == "pre_cleanup"
    assert writes[0]["probe_error"] == "TimeoutError"


def test_setup_cannot_consume_cleanup_reserve_or_begin_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    now = [100.0]
    _configure(monkeypatch, rows, calls)
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.time.monotonic", lambda: now[0]
    )

    def consume_work_budget(repository: Path, deadline: float) -> None:
        (repository / "seed.txt").write_text("seed\n")
        assert deadline == pytest.approx(
            100.0 + WORKTREE_SUITE_TIMEOUT_SECONDS - WORKTREE_CLEANUP_SECONDS
        )
        now[0] = deadline

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._initialize_seed_repository",
        consume_work_budget,
    )

    with pytest.raises(CharacterizationError, match="deadline exhausted before"):
        collect_disposable_worktree_edit_characterization(
            object(), tmp_path / "reserve.json"
        )

    assert not [call for call in calls if call[1] == "--bg"]


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


@pytest.mark.parametrize("failure", ["cleanup", "snapshot"])
def test_post_stop_integrity_is_unknown_when_not_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    rows: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, ...]] = []
    writes: list[dict[str, object]] = []
    _configure(monkeypatch, rows, calls)
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._observe_worktree_probe",
        lambda *args: {
            "probe_status": "probe_inconclusive",
            "worktree_path": "/private/tmp/owned",
            "primary_unchanged": True,
            "diff": "",
        },
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.atomic_write_contract",
        lambda destination, contract: writes.append(contract),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._preserve_worktree_diagnostics",
        lambda root: tmp_path / "preserved",
    )
    if failure == "cleanup":
        monkeypatch.setattr(
            "hermes_claude_sessions.characterization._worktree_cleanup",
            lambda *args: False,
        )
    else:
        snapshots = 0

        def fail_post_snapshot(*args):
            nonlocal snapshots
            snapshots += 1
            if snapshots >= 2:
                raise OSError("post-stop snapshot unavailable")
            return "worktree /private/tmp/main\n"

        monkeypatch.setattr(
            "hermes_claude_sessions.characterization._worktree_snapshot",
            fail_post_snapshot,
        )

    with pytest.raises(CharacterizationError):
        collect_disposable_worktree_edit_characterization(
            object(), tmp_path / f"{failure}.json"
        )

    result = writes[-1]
    assert result["stop_preserved_artifact"] is None
    assert result["stop_preserved_worktree"] is None
    worktree = result["worktree"]
    assert isinstance(worktree, dict)
    assert worktree["primary_unchanged_after_stop"] is None


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
            f"worktree {tmp_path}\n\nworktree {owned}\nbranch refs/heads/probe\n"
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
    assert not re.search(r'(?<![A-Za-z0-9:/])/(?!/)[^\s"\'<>]+', serialized)
