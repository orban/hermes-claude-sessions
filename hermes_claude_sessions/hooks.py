"""Hermes hook callbacks for Claude session tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _without_prompt(value: Any, prompt: Any) -> Any:
    if isinstance(value, str) and isinstance(prompt, str) and prompt:
        return value.replace(prompt, "<redacted>")
    return value


def pre_tool_call(
    tool_name: str, args: Mapping[str, Any], **kwargs: object
) -> dict[str, str] | None:
    """Require Hermes' normal human approval before starting a session."""
    del kwargs
    if tool_name != "claude_session_start":
        return None
    try:
        prompt = args.get("prompt")
        name = _without_prompt(args.get("name"), prompt)
        cwd = _without_prompt(args.get("cwd"), prompt)
        mode = _without_prompt(args.get("permission_mode", "manual"), prompt)
        message = (
            "Approve native Claude session start: "
            f"name={name!r}; cwd={cwd!r}; permission_mode={mode!r}"
        )
    except Exception:
        message = "Approve native Claude session start with unavailable metadata"
    return {"action": "approve", "message": message}
