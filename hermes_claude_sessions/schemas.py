"""Model-facing schemas for native Claude background sessions."""

from __future__ import annotations

CLAUDE_SESSION_START: dict[str, object] = {
    "name": "claude_session_start",
    "description": (
        "Start native Claude background sessions only; do not use this for "
        "non-native Claude workflows."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Session name."},
            "cwd": {
                "type": "string",
                "description": "Existing absolute directory for the session.",
            },
            "prompt": {"type": "string", "description": "Claude task prompt."},
            "permission_mode": {
                "type": "string",
                "enum": ["manual", "acceptEdits", "plan"],
                "default": "manual",
            },
        },
        "required": ["name", "cwd", "prompt"],
    },
}

CLAUDE_SESSION_LIST: dict[str, object] = {
    "name": "claude_session_list",
    "description": "Read-only list of native Claude background sessions only.",
    "parameters": {"type": "object", "properties": {}},
}
