from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_claude_sessions import plugin

ROOT = Path(__file__).parents[1]
SKILL_PATH = ROOT / "skills" / "native-claude-sessions" / "SKILL.md"
README_PATH = ROOT / "README.md"


class RecordingContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.hooks: list[tuple[str, object]] = []
        self.cli_commands: list[dict[str, object]] = []
        self.skills: list[dict[str, object]] = []

    def register_tool(self, **kwargs: object) -> None:
        self.tools.append(dict(kwargs))

    def register_hook(self, name: str, callback: object) -> None:
        self.hooks.append((name, callback))

    def register_cli_command(self, **kwargs: object) -> None:
        self.cli_commands.append(dict(kwargs))

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        self.skills.append({"name": name, "path": path, "description": description})


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    _, raw, _ = text.split("---\n", 2)
    result: dict[str, str] = {}
    for line in raw.splitlines():
        key, value = line.split(": ", 1)
        result[key] = value.strip('"')
    return result


def test_registers_exactly_one_bundled_operating_skill() -> None:
    context = RecordingContext()

    plugin.register(context)

    assert context.skills == [
        {
            "name": "native-claude-sessions",
            "path": SKILL_PATH,
            "description": "Use when operating native Claude background sessions.",
        }
    ]
    assert SKILL_PATH.is_file()
    assert context.skills[0]["path"].resolve() == SKILL_PATH.resolve()


def test_operating_skill_has_valid_frontmatter_and_safety_boundaries() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = _frontmatter(text)

    assert frontmatter["name"] == "native-claude-sessions"
    assert frontmatter["description"].startswith(
        "Use when operating native Claude background sessions"
    )
    for required in (
        "claude_session_start",
        "claude_session_list",
        "claude_permission_list",
        "claude_permission_decide",
        "claude_session_wait",
        "claude_session_logs",
        "claude_session_stop",
        "default `auto` classifier",
        "hermes claude agents",
        "hermes claude attach <id>",
        "never request `bypassPermissions`",
        "independently inspect",
        "deny closed",
        "Do not retry a failed paid launch automatically",
        "end the current turn",
        "never loop over waits, sleeps",
        "fresh internal turn",
        "exclusively consumed",
        "do not route Claude through ZMX, tmux",
        "model=`fable`, effort=`low`",
        "model=`sonnet`, effort=`high`",
        "model=`opus`, effort=`xhigh`",
        "omit `model` and `effort`",
    ):
        assert required.lower() in text.lower()


def test_readme_documents_ownership_handoffs_and_non_goals() -> None:
    text = README_PATH.read_text(encoding="utf-8")

    for required in (
        "claude_session_start",
        "claude_session_list",
        "claude_permission_list",
        "claude_permission_decide",
        "claude_session_wait",
        "claude_session_logs",
        "claude_session_stop",
        "--permission-mode auto",
        "hermes claude agents",
        "hermes claude attach <id>",
        "claude-sessions:native-claude-sessions",
        "CLAUDE_BIN",
        "PATH",
        "~/.local/bin/claude",
        "Claude Desktop",
        "$HERMES_HOME/plugins/claude-sessions",
        "no configured distribution remote",
        "do not invent github owner/repository coordinates",
        "hermes plugins doctor . --ci",
        "screen scraping",
        "keystroke injection",
        "bypassPermissions",
        "governance boundary, not an OS sandbox",
        "fail-closed",
        "opt-in",
        "cost",
        "--model",
        "--effort",
        "task-based routing",
        "`fable`/`low`",
    ):
        assert required.lower() in text.lower()
