"""Hermes hook callbacks for Claude session tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def pre_tool_call(
    tool_name: str, args: Mapping[str, Any], **kwargs: object
) -> dict[str, str] | None:
    """Require Hermes' normal human approval before starting a session."""
    del kwargs
    if tool_name != "claude_session_start":
        return None
    name = args.get("name")
    cwd = args.get("cwd")
    mode = args.get("permission_mode", "manual")
    return {
        "action": "approve",
        "message": (
            "Approve native Claude session start: "
            f"name={name!r}; cwd={cwd!r}; permission_mode={mode!r}"
        ),
    }
