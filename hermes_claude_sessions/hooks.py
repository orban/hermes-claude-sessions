"""Hermes hook callbacks for Claude session tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .safety import without_prompt
from .tools import DEFAULT_PERMISSION_MODE

MAX_APPROVAL_FIELD_CHARS = 2_048
_TRUNCATION_MARKER = "...[truncated]"


def _bounded_repr(value: Any, prompt: Any) -> str:
    rendered = repr(without_prompt(value, prompt))
    if len(rendered) <= MAX_APPROVAL_FIELD_CHARS:
        return rendered
    return (
        rendered[: MAX_APPROVAL_FIELD_CHARS - len(_TRUNCATION_MARKER)]
        + _TRUNCATION_MARKER
    )


def pre_tool_call(
    tool_name: str, args: Mapping[str, Any], **kwargs: object
) -> dict[str, str] | None:
    """Run auto-mode launches unattended; gate every less-managed launch."""
    del kwargs
    if tool_name != "claude_session_start":
        return None
    try:
        permission_mode = args.get("permission_mode", DEFAULT_PERMISSION_MODE)
        if permission_mode == "auto":
            return None
        prompt = args.get("prompt")
        message = (
            "Approve non-auto native Claude session start: "
            f"name={_bounded_repr(args.get('name'), prompt)}; "
            f"cwd={_bounded_repr(args.get('cwd'), prompt)}; "
            f"permission_mode={_bounded_repr(permission_mode, prompt)}; "
            f"model={_bounded_repr(args.get('model'), prompt)}; "
            f"effort={_bounded_repr(args.get('effort'), prompt)}"
        )
        redacted_message = without_prompt(message, prompt)
        if isinstance(redacted_message, str):
            message = redacted_message
    except Exception:
        message = "Approve native Claude session start with unavailable metadata"
    return {"action": "approve", "message": message}
