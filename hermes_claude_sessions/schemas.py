"""Model-facing schemas for Hermes-managed native Claude sessions."""

from __future__ import annotations

from .tools import ALLOWED_PERMISSION_MODES, DEFAULT_PERMISSION_MODE

CLAUDE_SESSION_START: dict[str, object] = {
    "name": "claude_session_start",
    "description": (
        "Start one native Claude background session. Auto mode is the unattended "
        "default: Claude's classifier decides routine actions and residual permission "
        "requests are routed back to Hermes."
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
                "enum": list(ALLOWED_PERMISSION_MODES),
                "default": DEFAULT_PERMISSION_MODE,
            },
        },
        "required": ["name", "cwd", "prompt"],
    },
}

CLAUDE_SESSION_LIST: dict[str, object] = {
    "name": "claude_session_list",
    "description": "Read-only list of native Claude background sessions.",
    "parameters": {"type": "object", "properties": {}},
}

CLAUDE_PERMISSION_LIST: dict[str, object] = {
    "name": "claude_permission_list",
    "description": (
        "List pending permission requests from auto-mode Claude sessions for Hermes "
        "to judge against the delegated task."
    ),
    "parameters": {"type": "object", "properties": {}},
}

CLAUDE_PERMISSION_DECIDE: dict[str, object] = {
    "name": "claude_permission_decide",
    "description": (
        "Allow or deny one pending Claude permission request. Decisions are one-shot "
        "and never persist an allow rule."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "pattern": "^[0-9a-f]{32}$",
                "description": "Pending request identifier.",
            },
            "decision": {"type": "string", "enum": ["allow", "deny"]},
            "reason": {
                "type": "string",
                "description": "Scoped justification shown in the audit record.",
            },
        },
        "required": ["request_id", "decision", "reason"],
    },
}

CLAUDE_SESSION_LOGS: dict[str, object] = {
    "name": "claude_session_logs",
    "description": "Read native logs for a session launched and owned by Hermes.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": "^[0-9a-f]{8}$"},
        },
        "required": ["id"],
    },
}

CLAUDE_SESSION_STOP: dict[str, object] = {
    "name": "claude_session_stop",
    "description": (
        "Stop one session launched and owned by Hermes, then verify its terminal state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": "^[0-9a-f]{8}$"},
        },
        "required": ["id"],
    },
}
