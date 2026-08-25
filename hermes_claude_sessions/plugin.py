"""Claude sessions native plugin registration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import cli, hooks, schemas, tools

_SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "native-claude-sessions"
    / "SKILL.md"
)
_SKILL_DESCRIPTION = "Use when operating native Claude background sessions."


@runtime_checkable
class PluginContext(Protocol):
    """Minimal Hermes plugin surface used by this package."""

    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, object],
        handler: Callable[..., str],
    ) -> None: ...

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None: ...

    def register_skill(self, name: str, path: Path, description: str = "") -> None: ...

    def register_cli_command(
        self,
        *,
        name: str,
        help: str,
        setup_fn: Callable[..., Any],
        handler_fn: Callable[..., int],
    ) -> None: ...


def register(ctx: object) -> None:
    """Register the two native-session tools and their approval hook."""
    if not isinstance(ctx, PluginContext):
        return
    ctx.register_tool(
        name="claude_session_start",
        toolset="claude_sessions",
        schema=schemas.CLAUDE_SESSION_START,
        handler=tools.claude_session_start,
    )
    ctx.register_tool(
        name="claude_session_list",
        toolset="claude_sessions",
        schema=schemas.CLAUDE_SESSION_LIST,
        handler=tools.claude_session_list,
    )
    ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
    ctx.register_skill("native-claude-sessions", _SKILL_PATH, _SKILL_DESCRIPTION)
    ctx.register_cli_command(
        name="claude",
        help="Open native Claude agent views and attachments",
        setup_fn=cli.setup_argparse,
        handler_fn=cli.handle_cli,
    )
