"""Claude sessions native plugin registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from . import hooks, schemas, tools


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
