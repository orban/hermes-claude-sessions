"""Non-blocking Claude lifecycle hooks that spool semantic Hermes events."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from . import bridge

MAX_HOOK_INPUT_CHARS = 131_072
EventMapping = tuple[str, str, str | None, dict[str, object]]


def _event_for(payload: Mapping[str, object], session: Path) -> EventMapping | None:
    hook = payload.get("hook_event_name")
    terminal_id = f"terminal-{session.name}"
    if hook == "Notification":
        notification_type = str(payload.get("notification_type") or "")
        if notification_type == "agent_needs_input":
            return (
                "needs_input",
                "Managed Claude session needs input. "
                "Inspect its authoritative state and logs.",
                None,
                {"notification_type": notification_type},
            )
        if notification_type == "agent_completed":
            return (
                "completed",
                "Managed Claude session completed. Read its logs, inspect the "
                "artifacts, and independently verify the work.",
                terminal_id,
                {"notification_type": notification_type},
            )
        return None
    if hook == "Stop":
        return (
            "state_changed",
            "Managed Claude session reached a response boundary. Inspect its "
            "authoritative state before continuing the delegated task.",
            None,
            {},
        )
    if hook == "StopFailure":
        return (
            "failed",
            "Managed Claude session hit an API failure. Inspect its authoritative "
            "state and logs before deciding whether to retry.",
            terminal_id,
            {},
        )
    if hook == "SessionEnd":
        return (
            "session_ended",
            "Managed Claude session ended. Inspect its logs and verify any produced "
            "artifacts.",
            terminal_id,
            {},
        )
    return None


def run(
    *,
    stdin: IO[str],
    stdout: IO[str],
    environ: Mapping[str, str],
) -> int:
    """Persist a lifecycle delta and return without influencing Claude control flow."""
    del stdout
    try:
        raw = stdin.read(MAX_HOOK_INPUT_CHARS + 1)
        if len(raw) > MAX_HOOK_INPUT_CHARS:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        raw_session = environ.get(bridge.SESSION_DIR_ENV)
        if not raw_session:
            return 0
        session = Path(raw_session)
        if not (session / "metadata.json").is_file():
            return 0
        mapped = _event_for(payload, session)
        if mapped is None:
            return 0
        kind, message, event_id, safe_payload = mapped
        bridge.enqueue_event(
            session,
            kind=kind,
            message=message,
            event_id=event_id,
            payload=safe_payload,
        )
        bridge.signal_dispatcher(session)
    except Exception:
        return 0
    return 0


def main(*, session_dir: str | None = None) -> int:
    environ = dict(os.environ)
    if session_dir:
        environ[bridge.SESSION_DIR_ENV] = session_dir
    return run(stdin=sys.stdin, stdout=sys.stdout, environ=environ)


if __name__ == "__main__":
    raise SystemExit(main())
