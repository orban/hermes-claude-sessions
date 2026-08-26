from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_claude_sessions import cli, hooks, plugin, schemas  # noqa: E402


class RecordingContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, object]] = []
        self.skills: list[tuple[str, Path, str]] = []
        self.cli_commands: list[dict[str, object]] = []

    def register_tool(self, **kwargs: object) -> None:
        self.tools.append(dict(kwargs))

    def register_hook(self, name: str, callback: object) -> None:
        self.hooks.append((name, callback))

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        self.skills.append((name, path, description))

    def register_cli_command(self, **kwargs: object) -> None:
        self.cli_commands.append(dict(kwargs))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    cli.setup_argparse(result)
    return result


def test_argparse_requires_a_subcommand_and_attach_id() -> None:
    with pytest.raises(SystemExit):
        parser().parse_args([])
    with pytest.raises(SystemExit):
        parser().parse_args(["attach"])


def test_argparse_parses_agents_and_attach() -> None:
    assert vars(parser().parse_args(["agents"])) == {"claude_command": "agents"}
    assert vars(parser().parse_args(["attach", "native-id"])) == {
        "claude_command": "attach",
        "identifier": "native-id",
    }


@pytest.mark.parametrize("identifier", ["--help", "-x"])
def test_argparse_rejects_option_looking_attach_identifiers(identifier: str) -> None:
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["attach", "--", identifier])
    assert error.value.code == 2


def test_argparse_help_and_unknown_command_exit_conventionally() -> None:
    with pytest.raises(SystemExit) as help_exit:
        parser().parse_args(["--help"])
    assert help_exit.value.code == 0

    with pytest.raises(SystemExit) as unknown_exit:
        parser().parse_args(["unknown"])
    assert unknown_exit.value.code == 2


@pytest.mark.parametrize(
    ("args", "expected_argv"),
    [
        (argparse.Namespace(claude_command="agents"), ["/mock/claude", "agents"]),
        (
            argparse.Namespace(claude_command="attach", identifier="native-id"),
            ["/mock/claude", "attach", "native-id"],
        ),
    ],
)
def test_handler_uses_native_inherited_terminal_argv(
    monkeypatch: pytest.MonkeyPatch,
    args: argparse.Namespace,
    expected_argv: list[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(
        *run_args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append({"args": run_args, "kwargs": kwargs})
        return subprocess.CompletedProcess(run_args[0], 23)

    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.handle_cli(args) == 23
    assert calls == [
        {
            "args": (expected_argv,),
            "kwargs": {"shell": False, "check": False},
        }
    ]


def test_handler_returns_controlled_code_when_claude_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: None)

    assert cli.handle_cli(argparse.Namespace(claude_command="agents")) == 127
    assert capsys.readouterr().err == (
        "Claude CLI is unavailable; set CLAUDE_BIN or install an executable "
        "claude binary\n"
    )


def test_handler_returns_controlled_code_when_subprocess_cannot_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )

    assert cli.handle_cli(argparse.Namespace(claude_command="agents")) == 127
    assert capsys.readouterr().err == (
        "Claude CLI could not be started; check CLAUDE_BIN or the claude installation\n"
    )


def test_handler_returns_controlled_code_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    assert cli.handle_cli(argparse.Namespace(claude_command="agents")) == 130


def test_handler_translates_child_signal_to_shell_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], -15),
    )

    assert cli.handle_cli(argparse.Namespace(claude_command="agents")) == 143


def test_registers_managed_tools_one_hook_and_one_claude_cli_command() -> None:
    context = RecordingContext()

    plugin.register(context)

    assert [item["name"] for item in context.tools] == [
        "claude_session_start",
        "claude_session_list",
        "claude_permission_list",
        "claude_permission_decide",
        "claude_session_logs",
        "claude_session_stop",
    ]
    assert {item["toolset"] for item in context.tools} == {"claude_sessions"}
    assert [item["schema"] for item in context.tools] == [
        schemas.CLAUDE_SESSION_START,
        schemas.CLAUDE_SESSION_LIST,
        schemas.CLAUDE_PERMISSION_LIST,
        schemas.CLAUDE_PERMISSION_DECIDE,
        schemas.CLAUDE_SESSION_LOGS,
        schemas.CLAUDE_SESSION_STOP,
    ]
    assert context.hooks == [("pre_tool_call", hooks.pre_tool_call)]
    assert context.cli_commands == [
        {
            "name": "claude",
            "help": "Open native Claude agent views and attachments",
            "setup_fn": cli.setup_argparse,
            "handler_fn": cli.handle_cli,
        }
    ]
