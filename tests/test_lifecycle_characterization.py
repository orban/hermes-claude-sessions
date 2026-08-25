from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

import pytest

from hermes_claude_sessions.characterization import (
    EXACT_CLAUDE_BIN,
    CharacterizationError,
    CommandCapture,
    _classify_daemon,
    _classify_lifecycle_roster,
    _lifecycle_cleanup,
    _lifecycle_launch_argv,
    _lifecycle_roster_snapshot,
    _LifecycleProbe,
    _recover_lifecycle_identity_by_name,
    _run_owned_lifecycle_command,
    collect_unattended_lifecycle_characterization,
    contract_fingerprint,
)


def test_lifecycle_launch_argv_is_exact_manual_no_write_scope() -> None:
    probe = _LifecycleProbe("lifecycle-" + str(uuid.uuid4()))
    argv = _lifecycle_launch_argv(Path(EXACT_CLAUDE_BIN), probe)
    assert argv[:6] == (
        EXACT_CLAUDE_BIN,
        "--bg",
        "--name",
        probe.display_name,
        "--permission-mode",
        "manual",
    )
    assert "--session-id" not in argv and "--all" not in argv
    assert "read only" in argv[-1].lower()


def test_lifecycle_owned_runner_rejects_arbitrary_targets(tmp_path: Path) -> None:
    probe = _LifecycleProbe(
        "lifecycle-" + str(uuid.uuid4()), "deadbeef", str(uuid.uuid4())
    )
    for argv in (
        (EXACT_CLAUDE_BIN, "stop", "other-id"),
        (EXACT_CLAUDE_BIN, "respawn", "other-id"),
        (EXACT_CLAUDE_BIN, "logs", "other-id"),
        (EXACT_CLAUDE_BIN, "stop", "--all"),
    ):
        with pytest.raises(CharacterizationError, match="probe-owned"):
            _run_owned_lifecycle_command(
                Path(EXACT_CLAUDE_BIN), probe, argv, tmp_path, 1
            )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Daemon is running", "running"),
        ("daemon not running", "not_running"),
        ("", "unparseable"),
    ],
)
def test_daemon_classifier_is_conservative(output: str, expected: str) -> None:
    capture = CommandCapture(
        (EXACT_CLAUDE_BIN, "daemon", "status"), 0, output, "", False, None
    )
    assert _classify_daemon(capture) == expected


def test_daemon_classifier_recognizes_explicit_not_running_on_nonzero_exit() -> None:
    capture = CommandCapture(
        (EXACT_CLAUDE_BIN, "daemon", "status"),
        1,
        "Daemon is not running",
        "",
        False,
        None,
    )
    assert _classify_daemon(capture) == "not_running"


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], "stale"),
        ([{"state": "running"}], "active"),
        ([{"state": "blocked"}], "active"),
        ([{"state": "done"}], "recoverable"),
        ([{"state": "stopped"}], "recoverable"),
        ([{"state": "failed"}], "recoverable"),
        ([{"state": "running"}, {"state": "stopped"}], "ambiguous"),
        ([{"state": 123}], "ambiguous"),
    ],
)
def test_lifecycle_roster_classifier_is_evidence_bounded(
    rows: list[dict[str, object]], expected: str
) -> None:
    assert _classify_lifecycle_roster(rows) == expected


@pytest.mark.parametrize(
    "payload", ["null", "{}", '"text"', '[{"state":"running"}, 1]']
)
def test_lifecycle_roster_snapshot_rejects_malformed_json_structures(
    payload: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _LifecycleProbe("lifecycle-test", "deadbeef")
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._lifecycle_listing",
        lambda *args: CommandCapture(
            (EXACT_CLAUDE_BIN, "agents", "--json", "--all"),
            0,
            payload,
            "",
            False,
            None,
        ),
    )
    assert (
        _lifecycle_roster_snapshot(
            Path(EXACT_CLAUDE_BIN), probe, tmp_path, time.monotonic() + 1
        )
        == "ambiguous"
    )


@pytest.mark.parametrize(
    "payload",
    ["null", "{}", '"text"', '[{"name":"lifecycle-test"}, 1]', "["],
)
def test_tokenless_recovery_rejects_malformed_roster_structures(
    payload: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _LifecycleProbe("lifecycle-test", "deadbeef")
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._lifecycle_listing",
        lambda *args: CommandCapture(
            (EXACT_CLAUDE_BIN, "agents", "--json", "--all"),
            0,
            payload,
            "",
            False,
            None,
        ),
    )

    with pytest.raises(CharacterizationError, match="malformed lifecycle roster"):
        _recover_lifecycle_identity_by_name(
            Path(EXACT_CLAUDE_BIN),
            probe,
            tmp_path,
            set(),
            time.monotonic() + 0.1,
        )


def test_lifecycle_probe_reconciles_exact_identity_exercises_identifiers_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = "aaaaaaaa-1111-4111-8111-111111111111"
    rows = {
        unrelated: {
            "id": "aaaaaaaa",
            "sessionId": unrelated,
            "name": "unrelated",
            "state": "running",
        }
    }
    calls: list[tuple[str, ...]] = []
    generations = iter(
        (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
            "33333333-3333-4333-8333-333333333333",
        )
    )

    def run(
        binary: Path,
        probe: _LifecycleProbe,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        del binary, cwd, timeout
        calls.append(argv)
        if argv[1] == "--bg":
            full = next(generations)
            rows[full] = {
                "id": full[:8],
                "sessionId": full,
                "name": probe.display_name,
                "state": "running",
            }
            return CommandCapture(
                argv,
                0,
                f"backgrounded · {full[:8]} · {probe.display_name}\n",
                "",
                False,
                None,
            )
        if argv[1] == "agents":
            return CommandCapture(
                argv, 0, json.dumps(list(rows.values())), "", False, None
            )
        if argv[1] == "daemon":
            return CommandCapture(argv, 0, "Daemon is running", "", False, None)
        if argv[1] == "--version":
            return CommandCapture(argv, 0, "2.1.239 (Claude Code)", "", False, None)
        target = argv[2]
        row = next(
            (
                item
                for item in rows.values()
                if target in {item["id"], item["sessionId"]}
            ),
            None,
        )
        if row is None:
            return CommandCapture(argv, 1, "", "not found", False, None)
        if argv[1] == "stop":
            row["state"] = "stopped"
        elif argv[1] == "respawn":
            rows.pop(row["sessionId"])
            full = next(generations)
            rows[full] = {
                "id": full[:8],
                "sessionId": full,
                "name": probe.display_name,
                "state": "running",
            }
            return CommandCapture(
                argv,
                0,
                f"backgrounded · {full[:8]} · {probe.display_name}\n",
                "",
                False,
                None,
            )
        return CommandCapture(argv, 0, "", "", False, None)

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_lifecycle_command", run
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.LIFECYCLE_POLL_SECONDS", 0.0
    )
    artifact = tmp_path / "lifecycle.json"
    result = collect_unattended_lifecycle_characterization(object(), artifact)

    assert result["cleanup"]["confirmed"] is True
    assert result["unrelated_agents"]["unchanged"] is True
    assert result["identifier_acceptance"] == {
        "logs": {"short": "accepted", "full_uuid": "accepted"},
        "stop": {"short": "accepted", "full_uuid": "accepted"},
        "respawn": {"short": "accepted", "full_uuid": "accepted"},
    }
    assert result["lifecycle"]["stop_transition"] == "observed"
    assert result["lifecycle"]["respawn_identity"] == "replaced"
    assert all("--all" not in call for call in calls if call[1] in {"stop", "respawn"})
    serialized = artifact.read_text()
    assert (
        unrelated not in serialized
        and "11111111-" not in serialized
        and str(tmp_path) not in serialized
    )
    payload = json.loads(serialized)
    fingerprint = payload.pop("fingerprint")
    assert fingerprint == contract_fingerprint(payload)


def test_lifecycle_archive_precedes_finally_cleanup_and_cleanup_runs_after_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    probe = _LifecycleProbe(
        "lifecycle-test", "deadbeef", "deadbeef-1111-4111-8111-111111111111"
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._initialize_seed_repository",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._lifecycle_listing",
        lambda *args: CommandCapture(
            (EXACT_CLAUDE_BIN, "agents", "--json", "--all"), 0, "[]", "", False, None
        ),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._mint_lifecycle_probe", lambda: probe
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_lifecycle_command",
        lambda *args: CommandCapture(
            (EXACT_CLAUDE_BIN, "--bg"), 1, "", "", False, None
        ),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.atomic_write_contract",
        lambda *args: events.append("archive"),
    )
    with pytest.raises(CharacterizationError, match="background launch failed"):
        collect_unattended_lifecycle_characterization(object(), tmp_path / "out.json")
    assert events == ["archive", "archive", "archive"]


def test_each_exploratory_mutation_is_archived_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    full = "deadbeef-1111-4111-8111-111111111111"
    probe = _LifecycleProbe("lifecycle-test", "deadbeef", full)

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._initialize_seed_repository",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._mint_lifecycle_probe", lambda: probe
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._reconcile_lifecycle_identity",
        lambda *args: probe,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._transition_to_terminal",
        lambda *args: "observed",
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._lifecycle_cleanup",
        lambda *args: True,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization.atomic_write_contract",
        lambda _path, payload: events.append(
            f"archive:{payload.get('pending_mutation', 'final')}"
        ),
    )

    def run(*args: object) -> CommandCapture:
        argv = args[2]
        assert isinstance(argv, tuple)
        if argv[1] == "--bg":
            events.append("run:launch:bg")
        if argv[1] in {"stop", "respawn"}:
            events.append(
                f"run:{argv[1]}:{'short' if argv[2] == 'deadbeef' else 'full_uuid'}"
            )
        if argv[1] == "agents":
            stdout = json.dumps(
                [
                    {
                        "id": "deadbeef",
                        "sessionId": full,
                        "name": probe.display_name,
                        "state": "running",
                    }
                ]
            )
        elif argv[1] in {"--bg", "respawn"}:
            stdout = f"backgrounded · deadbeef · {probe.display_name}\n"
        elif argv[1] == "--version":
            stdout = "2.1.241 (Claude Code)"
        else:
            stdout = ""
        return CommandCapture(argv, 0, stdout, "", False, None)

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_lifecycle_command", run
    )
    collect_unattended_lifecycle_characterization(object(), tmp_path / "out.json")

    for mutation in (
        "launch:bg",
        "stop:short",
        "respawn:short",
        "stop:full_uuid",
        "respawn:full_uuid",
    ):
        assert events.index(f"archive:{mutation}") < events.index(f"run:{mutation}")


@pytest.mark.parametrize("accepted_spelling", ["short", "full_uuid"])
def test_missing_respawn_token_recovers_exact_replacement_for_cleanup(
    accepted_spelling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_full = "11111111-1111-4111-8111-111111111111"
    new_full = "22222222-2222-4222-8222-222222222222"
    minted = _LifecycleProbe("lifecycle-test")
    current = {
        "id": "11111111",
        "sessionId": old_full,
        "name": minted.display_name,
        "state": "running",
    }
    launched = False
    replacement_created = False
    stop_targets: list[str] = []

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._initialize_seed_repository",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._mint_lifecycle_probe", lambda: minted
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._transition_to_terminal",
        lambda *args: "observed",
    )

    def run(*args: object) -> CommandCapture:
        nonlocal launched, replacement_created, current
        argv = args[2]
        assert isinstance(argv, tuple)
        if argv[1] == "--version":
            return CommandCapture(argv, 0, "2.1.241 (Claude Code)", "", False, None)
        if argv[1] == "daemon":
            return CommandCapture(argv, 0, "Daemon is running", "", False, None)
        if argv[1] == "--bg":
            launched = True
            return CommandCapture(
                argv,
                0,
                f"backgrounded · 11111111 · {minted.display_name}\n",
                "",
                False,
                None,
            )
        if argv[1] == "agents":
            rows = [current] if launched else []
            return CommandCapture(argv, 0, json.dumps(rows), "", False, None)
        if argv[1] == "logs":
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1] == "stop":
            stop_targets.append(argv[2])
            if argv[2] in {current["id"], current["sessionId"]}:
                current["state"] = "stopped"
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1] == "respawn":
            spelling = "short" if argv[2] == current["id"] else "full_uuid"
            if replacement_created or spelling != accepted_spelling:
                return CommandCapture(argv, 1, "", "", False, None)
            replacement_created = True
            current = {
                "id": "22222222",
                "sessionId": new_full,
                "name": minted.display_name,
                "state": "running",
            }
            return CommandCapture(
                argv, 0, "respawn accepted without token\n", "", False, None
            )
        raise AssertionError(argv)

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_lifecycle_command", run
    )
    result = collect_unattended_lifecycle_characterization(
        object(), tmp_path / "out.json"
    )
    assert result["cleanup"]["confirmed"] is True
    assert replacement_created
    assert "22222222" in stop_targets


def test_new_respawn_token_is_the_final_cleanup_capability_on_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_full = "11111111-1111-4111-8111-111111111111"
    new_full = "22222222-2222-4222-8222-222222222222"
    probe = _LifecycleProbe("lifecycle-test")
    stop_targets: list[str] = []
    launched = False
    respawned = False

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._require_exact_observer_capability",
        lambda _: Path(EXACT_CLAUDE_BIN),
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._initialize_seed_repository",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._mint_lifecycle_probe", lambda: probe
    )
    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._transition_to_terminal",
        lambda *args: "observed",
    )

    def run(*args: object) -> CommandCapture:
        nonlocal launched, respawned
        argv = args[2]
        assert isinstance(argv, tuple)
        if argv[1] == "--bg":
            launched = True
            return CommandCapture(
                argv,
                0,
                f"backgrounded · 11111111 · {probe.display_name}\n",
                "",
                False,
                None,
            )
        if argv[1] == "--version":
            return CommandCapture(argv, 0, "2.1.241 (Claude Code)", "", False, None)
        if argv[1] == "daemon":
            return CommandCapture(argv, 0, "Daemon is running", "", False, None)
        if argv[1] == "respawn" and argv[2] == "11111111":
            respawned = True
            return CommandCapture(
                argv,
                0,
                f"backgrounded · 22222222 · {probe.display_name}\n",
                "",
                False,
                None,
            )
        if argv[1] == "stop":
            stop_targets.append(argv[2])
            return CommandCapture(argv, 0, "", "", False, None)
        if argv[1] == "agents":
            if not launched:
                rows = []
            elif not respawned:
                rows = [
                    {
                        "id": "11111111",
                        "sessionId": old_full,
                        "name": probe.display_name,
                        "state": "running",
                    }
                ]
            else:
                rows = [
                    {
                        "id": "22222222",
                        "sessionId": new_full,
                        "name": probe.display_name,
                        "state": "running",
                    },
                    {
                        "id": "22222222",
                        "sessionId": new_full,
                        "name": probe.display_name,
                        "state": "running",
                    },
                ]
            return CommandCapture(argv, 0, json.dumps(rows), "", False, None)
        return CommandCapture(argv, 0, "", "", False, None)

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_lifecycle_command", run
    )
    with pytest.raises(
        CharacterizationError, match="ambiguous lifecycle native identity"
    ):
        collect_unattended_lifecycle_characterization(object(), tmp_path / "out.json")
    assert "22222222" in stop_targets


@pytest.mark.parametrize(
    "owned_rows",
    [
        [
            {
                "id": "deadbeef",
                "sessionId": "not-a-canonical-uuid",
                "name": "lifecycle-unique-name",
                "state": "stopped",
            }
        ],
        [
            {
                "id": "deadbeef",
                "sessionId": "deadbeef-1111-4111-8111-111111111111",
                "state": "stopped",
            }
        ],
        [
            {
                "id": "changed1",
                "sessionId": "deadbeef-1111-4111-8111-111111111111",
                "name": "lifecycle-unique-name",
                "state": "stopped",
            }
        ],
        [
            {
                "id": "deadbeef",
                "sessionId": "deadbeef-1111-4111-8111-111111111111",
                "name": "lifecycle-unique-name",
                "state": "stopped",
            },
            {
                "id": "deadbeef",
                "sessionId": "deadbeef-1111-4111-8111-111111111111",
                "name": "lifecycle-unique-name",
                "state": "stopped",
            },
        ],
    ],
)
def test_lifecycle_cleanup_rejects_malformed_or_duplicate_owned_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owned_rows: list[dict[str, object]],
) -> None:
    probe = _LifecycleProbe(
        "lifecycle-unique-name",
        "deadbeef",
        "deadbeef-1111-4111-8111-111111111111",
    )

    def run(
        binary: Path,
        candidate: _LifecycleProbe,
        argv: tuple[str, ...],
        cwd: Path,
        timeout: float,
    ) -> CommandCapture:
        del binary, candidate, cwd, timeout
        if argv[1] == "stop":
            return CommandCapture(argv, 0, "", "", False, None)
        return CommandCapture(argv, 0, json.dumps(owned_rows), "", False, None)

    monkeypatch.setattr(
        "hermes_claude_sessions.characterization._run_owned_lifecycle_command", run
    )
    assert not _lifecycle_cleanup(
        Path(EXACT_CLAUDE_BIN), probe, tmp_path, time.monotonic() + 1
    )


def test_live_lifecycle_fixture_is_sanitized_fingerprinted_and_conservative() -> None:
    fixture = (
        Path(__file__).parent
        / "characterization"
        / "unattended_lifecycle.live.fixture.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    fingerprint = payload.pop("fingerprint")
    serialized = json.dumps(payload, sort_keys=True)
    assert fingerprint == contract_fingerprint(payload)
    assert payload["fully_characterized"] is False
    assert payload["attended"] is False
    assert payload["cleanup"]["confirmed"] is True
    assert payload["actual_test_owned_session_count"] == 1
    assert payload["estimated_test_owned_session_count"] == 1
    assert payload["launch_token_reconciled"] is True
    assert payload["live_evidence"] is True
    assert payload["artifact_phase"] == "post_cleanup"
    assert payload["probe_status"] == "observed"
    assert payload["unrelated_agents"]["unchanged"] is True
    assert set(payload["roster"]) == {
        "before_launch",
        "after_launch",
        "after_stop",
        "after_respawn",
        "after_final_stop",
    }
    assert set(payload["roster"].values()) <= {
        "active",
        "recoverable",
        "stale",
        "ambiguous",
    }
    assert not any(
        token in serialized for token in ("/Users/", "/private/", "backgrounded ·")
    )
    assert not re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", serialized)
