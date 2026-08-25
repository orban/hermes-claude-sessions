from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_claude_sessions import cli, hooks, plugin, schemas, tools  # noqa: E402


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


def decode(result: str) -> dict[str, Any]:
    return json.loads(result)


def test_schemas_describe_only_native_background_sessions() -> None:
    start = schemas.CLAUDE_SESSION_START
    listing = schemas.CLAUDE_SESSION_LIST

    assert start["name"] == "claude_session_start"
    assert set(start["parameters"]["required"]) == {"name", "cwd", "prompt"}
    assert start["parameters"]["properties"]["permission_mode"] == {
        "type": "string",
        "enum": ["manual", "acceptEdits", "plan"],
        "default": "manual",
    }
    assert "native Claude background sessions" in start["description"]
    assert listing["name"] == "claude_session_list"
    assert listing["parameters"] == {"type": "object", "properties": {}}
    assert "read-only" in listing["description"].lower()


def test_start_uses_exact_shell_free_argv_and_default_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args[0], 0, "started", "")

    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    result = decode(
        tools.claude_session_start(
            {"name": "review", "cwd": str(tmp_path), "prompt": "secret prompt"}
        )
    )

    assert result == {
        "ok": True,
        "returncode": 0,
        "name": "review",
        "cwd": str(tmp_path),
        "mode": "manual",
        "stdout": "started",
        "stderr": "",
    }
    assert calls == [
        {
            "args": (
                [
                    "/mock/claude",
                    "--bg",
                    "--name",
                    "review",
                    "--permission-mode",
                    "manual",
                    "--",
                    "secret prompt",
                ],
            ),
            "kwargs": {
                "cwd": str(tmp_path),
                "capture_output": True,
                "text": True,
                "timeout": 20,
                "shell": False,
                "check": False,
            },
        }
    ]


@pytest.mark.parametrize("mode", ["manual", "acceptEdits", "plan"])
def test_start_accepts_each_allowed_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    captured: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        captured.extend(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    assert (
        decode(
            tools.claude_session_start(
                {
                    "name": "job",
                    "cwd": str(tmp_path),
                    "prompt": "work",
                    "permission_mode": mode,
                }
            )
        )["ok"]
        is True
    )
    assert captured[-4:] == ["--permission-mode", mode, "--", "work"]


@pytest.mark.parametrize(
    "args,error",
    [
        (None, "arguments must be an object"),
        ({"name": "", "cwd": "/", "prompt": "work"}, "name must be a non-empty string"),
        (
            {"name": "job", "cwd": "/", "prompt": ""},
            "prompt must be a non-empty string",
        ),
        (
            {"name": "job", "cwd": 42, "prompt": "work"},
            "cwd must be an absolute existing directory",
        ),
        (
            {"name": "job", "cwd": "relative", "prompt": "work"},
            "cwd must be an absolute existing directory",
        ),
        (
            {"name": "job", "cwd": "/definitely/missing", "prompt": "work"},
            "cwd must be an absolute existing directory",
        ),
        (
            {"name": "job", "cwd": "/", "prompt": "work", "permission_mode": "yolo"},
            "permission_mode must be one of: manual, acceptEdits, plan",
        ),
    ],
)
def test_start_validation_errors_are_json(
    monkeypatch: pytest.MonkeyPatch, args: object, error: str
) -> None:
    monkeypatch.setattr(
        tools, "resolve_claude_bin", lambda: pytest.fail("should not resolve")
    )

    result = decode(tools.claude_session_start(args))

    assert result == {"ok": False, "error": error}


def test_start_never_returns_prompt_even_when_cli_echoes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = "super secret task text"
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, prompt, prompt),
    )

    result = tools.claude_session_start(
        {"name": "job", "cwd": str(tmp_path), "prompt": prompt}
    )

    assert prompt not in result
    assert decode(result)["stdout"] == ""
    assert decode(result)["stderr"] == ""


def test_prompt_redaction_sentinel_cannot_alias_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = "<redacted>"
    args = {"name": prompt, "cwd": str(tmp_path), "prompt": prompt}
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *run_args, **kwargs: subprocess.CompletedProcess(
            run_args[0], 0, prompt, prompt
        ),
    )

    result = tools.claude_session_start(args)
    approval = hooks.pre_tool_call("claude_session_start", args)

    assert prompt not in result
    assert approval is not None
    assert prompt not in approval["message"]


def test_start_result_and_approval_have_total_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = {
        "name": "n" * (tools.MAX_OUTPUT_CHARS + 2_000),
        "cwd": str(tmp_path),
        "prompt": "private work",
        "permission_mode": "manual",
    }
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *run_args, **kwargs: subprocess.CompletedProcess(
            run_args[0], 0, "o" * tools.MAX_OUTPUT_CHARS, "e" * tools.MAX_OUTPUT_CHARS
        ),
    )

    result_text = tools.claude_session_start(args)
    result = decode(result_text)
    approval = hooks.pre_tool_call("claude_session_start", args)

    assert len(result_text) <= tools.MAX_OUTPUT_CHARS
    assert result == {
        "ok": True,
        "returncode": 0,
        "output_omitted": True,
    }
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *run_args, **kwargs: subprocess.CompletedProcess(
            run_args[0], 9, "o" * tools.MAX_OUTPUT_CHARS, "e" * tools.MAX_OUTPUT_CHARS
        ),
    )
    failure_text = tools.claude_session_start(args)
    assert len(failure_text) <= tools.MAX_OUTPUT_CHARS
    assert decode(failure_text) == {
        "ok": False,
        "returncode": 9,
        "output_omitted": True,
        "error": "Claude command exited with a non-zero status",
    }
    assert approval is not None
    assert len(approval["message"]) <= tools.MAX_OUTPUT_CHARS
    assert "name=" in approval["message"]
    assert "cwd=" in approval["message"]
    assert "permission_mode=" in approval["message"]


def test_resolver_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "override-claude"
    fallback = tmp_path / "fallback-claude"
    override.touch()
    fallback.touch()
    override.chmod(0o755)
    fallback.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(override))
    monkeypatch.setattr(tools.shutil, "which", lambda _: str(fallback))

    assert tools.resolve_claude_bin() == str(override)

    monkeypatch.delenv("CLAUDE_BIN")
    assert tools.resolve_claude_bin() == str(fallback)

    monkeypatch.setattr(tools.shutil, "which", lambda _: None)
    local_binary = tmp_path / ".local" / "bin" / "claude"
    local_binary.parent.mkdir(parents=True)
    local_binary.touch()
    local_binary.chmod(0o755)
    monkeypatch.setattr(tools.Path, "home", classmethod(lambda cls: tmp_path))
    assert tools.resolve_claude_bin() == str(local_binary)


def test_resolver_returns_none_when_no_executable_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "missing"))
    monkeypatch.setattr(tools.shutil, "which", lambda _: None)
    monkeypatch.setattr(tools.Path, "home", classmethod(lambda cls: tmp_path))

    assert tools.resolve_claude_bin() is None


def test_start_unavailable_and_oserror_are_controlled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = {"name": "job", "cwd": str(tmp_path), "prompt": "secret"}
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: None)

    unavailable = decode(tools.claude_session_start(args))

    assert unavailable["ok"] is False
    assert "unavailable" in unavailable["error"].lower()
    assert "secret" not in json.dumps(unavailable)

    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *run_args, **kwargs: (_ for _ in ()).throw(OSError("secret")),
    )

    os_error = decode(tools.claude_session_start(args))

    assert os_error["ok"] is False
    assert os_error["error"] == "Claude command could not be started"
    assert "secret" not in json.dumps(os_error)


def test_start_malformed_internal_result_is_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "_claude_session_start", lambda args: "not-json")

    result = decode(tools.claude_session_start({"prompt": "secret"}))

    assert result == {
        "ok": False,
        "error": "Claude session start returned malformed internal JSON",
    }


def test_start_bounds_captured_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "o" * 20_000, "e" * 20_000
        ),
    )

    result_text = tools.claude_session_start(
        {"name": "job", "cwd": str(tmp_path), "prompt": "work"}
    )
    result = decode(result_text)

    assert len(result_text) <= tools.MAX_OUTPUT_CHARS
    assert result == {
        "ok": True,
        "returncode": 0,
        "output_omitted": True,
    }


def test_list_returns_native_rows_unchanged_and_shell_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"id": "native", "nested": {"state": "idle"}}, {"new": [1, 2]}]
    calls: list[dict[str, Any]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args[0], 0, json.dumps(rows), "")

    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    assert decode(tools.claude_session_list({})) == {"ok": True, "sessions": rows}
    assert calls[0]["args"] == (["/mock/claude", "agents", "--json", "--all"],)
    assert calls[0]["kwargs"] == {
        "capture_output": True,
        "text": True,
        "timeout": 20,
        "shell": False,
        "check": False,
    }


def test_list_rejects_oversized_success_without_truncating_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = [{"value": "x" * (tools.MAX_OUTPUT_CHARS + 1)}]
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(oversized),
            "雪" * (tools.MAX_OUTPUT_CHARS + 1),
        ),
    )

    result_text = tools.claude_session_list({})
    result = decode(result_text)

    assert result["ok"] is False
    assert "too large" in result["error"].lower()
    assert "x" * 100 not in result_text
    assert len(result_text) <= tools.MAX_OUTPUT_CHARS


def test_list_rejects_success_that_expands_past_serialized_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"value": "雪" * 2_000}]
    native_json = json.dumps(rows, ensure_ascii=False)
    assert len(native_json) < tools.MAX_OUTPUT_CHARS
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, native_json, ""
        ),
    )

    result_text = tools.claude_session_list({})
    result = decode(result_text)

    assert result["ok"] is False
    assert "too large" in result["error"].lower()
    assert len(result_text) <= tools.MAX_OUTPUT_CHARS


def test_list_oserror_is_controlled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("sensitive detail")),
    )

    result_text = tools.claude_session_list({})
    result = decode(result_text)

    assert result == {
        "ok": False,
        "error": "Claude command could not be started",
        "stdout": "",
        "stderr": "",
    }
    assert "sensitive detail" not in result_text


def test_list_nonzero_error_has_total_response_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            7,
            "o" * tools.MAX_OUTPUT_CHARS,
            "雪" * tools.MAX_OUTPUT_CHARS,
        ),
    )

    result_text = tools.claude_session_list({})
    result = decode(result_text)

    assert len(result_text) <= tools.MAX_OUTPUT_CHARS
    assert result == {
        "ok": False,
        "error": "Claude command exited with a non-zero status",
        "returncode": 7,
        "output_omitted": True,
    }


@pytest.mark.parametrize(
    "stdout",
    [
        "{" + "雪" * 1_000,
        json.dumps("雪" * 1_000, ensure_ascii=False),
    ],
)
def test_list_parse_errors_have_total_response_bound(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout, "雪" * tools.MAX_OUTPUT_CHARS
        ),
    )

    result_text = tools.claude_session_list({})
    result = decode(result_text)

    assert len(result_text) <= tools.MAX_OUTPUT_CHARS
    assert result["ok"] is False
    assert result["output_omitted"] is True
    assert "stdout" not in result
    assert "stderr" not in result


@pytest.mark.parametrize(
    "failure", ["unavailable", "timeout", "nonzero", "invalid_json", "nonlist"]
)
def test_list_errors_are_controlled_json(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "unavailable":
        monkeypatch.setattr(tools, "resolve_claude_bin", lambda: None)
    elif failure == "timeout":
        monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
        monkeypatch.setattr(
            tools.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(args[0], 20, output="secret", stderr="secret")
            ),
        )
    elif failure == "nonzero":
        monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
        monkeypatch.setattr(
            tools.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0], 3, "out", "err"
            ),
        )
    elif failure == "invalid_json":
        monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
        monkeypatch.setattr(
            tools.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "{", ""),
        )
    else:
        monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
        monkeypatch.setattr(
            tools.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "{}", ""),
        )

    result = decode(tools.claude_session_list({}))

    assert result["ok"] is False
    assert "error" in result
    assert "secret" not in json.dumps(result)


def test_start_timeout_and_nonzero_are_controlled_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], 20)
        ),
    )
    timeout = decode(
        tools.claude_session_start(
            {"name": "job", "cwd": str(tmp_path), "prompt": "secret"}
        )
    )
    assert timeout == {
        "ok": False,
        "error": "Claude command timed out after 20 seconds",
        "name": "job",
        "cwd": str(tmp_path),
        "mode": "manual",
        "stdout": "",
        "stderr": "",
    }

    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 2, "out", "err"),
    )
    nonzero = decode(
        tools.claude_session_start(
            {"name": "job", "cwd": str(tmp_path), "prompt": "secret"}
        )
    )
    assert nonzero["ok"] is False
    assert nonzero["returncode"] == 2
    assert nonzero["stdout"] == "out"
    assert nonzero["stderr"] == "err"


def test_start_uses_end_of_options_before_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        captured.extend(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    result = decode(
        tools.claude_session_start(
            {
                "name": "job",
                "cwd": str(tmp_path),
                "prompt": "--dangerously-skip-permissions",
            }
        )
    )

    assert result["ok"] is True
    assert captured[-2:] == ["--", "--dangerously-skip-permissions"]


def test_handlers_never_raise_unexpected_internal_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_resolution() -> str:
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(tools, "resolve_claude_bin", fail_resolution)

    start = decode(
        tools.claude_session_start(
            {"name": "job", "cwd": str(tmp_path), "prompt": "secret prompt"}
        )
    )
    listing = decode(tools.claude_session_list({}))

    assert start["ok"] is False
    assert listing["ok"] is False
    assert "sensitive internal detail" not in json.dumps([start, listing])


def test_approval_hook_only_requests_start_without_prompt() -> None:
    prompt = "do not expose this"
    approval = hooks.pre_tool_call(
        "claude_session_start",
        {
            "name": "job",
            "cwd": "/tmp/work",
            "prompt": prompt,
            "permission_mode": "plan",
        },
        task_id="ignored",
    )

    assert approval is not None
    assert approval["action"] == "approve"
    assert prompt not in approval["message"]
    assert "job" in approval["message"]
    assert "/tmp/work" in approval["message"]
    assert "plan" in approval["message"]
    assert hooks.pre_tool_call("claude_session_list", {}, task_id="ignored") is None
    assert hooks.pre_tool_call("other", {"prompt": prompt}, task_id="ignored") is None


def test_approval_hook_handles_unavailable_or_nonstring_metadata() -> None:
    class BrokenMetadata(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("sensitive metadata failure")

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

    numeric = hooks.pre_tool_call(
        "claude_session_start",
        {"name": 1, "cwd": 2, "prompt": "secret", "permission_mode": 3},
    )
    unavailable = hooks.pre_tool_call("claude_session_start", BrokenMetadata())

    assert numeric is not None
    assert "name=1" in numeric["message"]
    assert "cwd=2" in numeric["message"]
    assert "permission_mode=3" in numeric["message"]
    assert unavailable == {
        "action": "approve",
        "message": "Approve native Claude session start with unavailable metadata",
    }


@pytest.mark.parametrize("alias", ["name", "cwd", "permission_mode"])
def test_prompt_is_redacted_when_it_aliases_disclosed_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alias: str
) -> None:
    values = {
        "name": "job",
        "cwd": str(tmp_path),
        "permission_mode": "manual",
    }
    prompt = values[alias]
    args = {**values, "prompt": prompt}
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *run_args, **kwargs: subprocess.CompletedProcess(run_args[0], 0, "", ""),
    )

    result = tools.claude_session_start(args)
    approval = hooks.pre_tool_call("claude_session_start", args)

    assert prompt not in result
    assert approval is not None
    assert prompt not in approval["message"]


def test_prompt_substrings_are_redacted_from_disclosed_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = "sensitive"
    args = {
        "name": "prefix-sensitive-suffix",
        "cwd": str(tmp_path),
        "prompt": prompt,
        "permission_mode": "manual",
    }
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *run_args, **kwargs: subprocess.CompletedProcess(run_args[0], 0, "", ""),
    )

    result = tools.claude_session_start(args)
    approval = hooks.pre_tool_call("claude_session_start", args)

    assert prompt not in result
    assert approval is not None
    assert prompt not in approval["message"]


def test_prompt_redaction_preserves_json_structure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt = ","
    args = {
        "name": prompt,
        "cwd": str(tmp_path),
        "prompt": prompt,
        "permission_mode": "manual",
    }
    monkeypatch.setattr(tools, "resolve_claude_bin", lambda: "/mock/claude")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *run_args, **kwargs: subprocess.CompletedProcess(run_args[0], 0, "", ""),
    )

    result = decode(tools.claude_session_start(args))

    assert result["name"] == ""
    assert result["ok"] is True


def test_registers_exactly_two_tools_and_one_pre_tool_hook() -> None:
    context = RecordingContext()

    plugin.register(context)

    assert [item["name"] for item in context.tools] == [
        "claude_session_start",
        "claude_session_list",
    ]
    assert {item["toolset"] for item in context.tools} == {"claude_sessions"}
    assert [item["schema"] for item in context.tools] == [
        schemas.CLAUDE_SESSION_START,
        schemas.CLAUDE_SESSION_LIST,
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


def test_manifest_declares_exact_tools_and_hook() -> None:
    manifest = (Path(__file__).parents[1] / "plugin.yaml").read_text()

    assert (
        "provides_tools:\n  - claude_session_start\n  - claude_session_list" in manifest
    )
    assert "provides_hooks:\n  - pre_tool_call" in manifest
    assert manifest.count("  - claude_session_") == 2
    assert manifest.count("  - pre_tool_call") == 1
