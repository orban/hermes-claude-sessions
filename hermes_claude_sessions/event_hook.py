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
_ATTENTION_NOTIFICATIONS = {
    "permission_prompt",
    "idle_prompt",
    "agent_needs_input",
}


def _event_for(payload: Mapping[str, object], session: Path) -> EventMapping | None:
    hook = payload.get("hook_event_name")
    if hook == "Notification":
        return None
    if hook == "Stop":
        return (
            "state_changed",
            "Managed Claude session reached a response boundary. Inspect its "
            "authoritative state before continuing the delegated task.",
            None,
            {},
        )
    return None


def _is_reconciliation_hint(payload: Mapping[str, object]) -> bool:
    hook = payload.get("hook_event_name")
    if hook in {"StopFailure", "SessionEnd"}:
        return True
    if hook != "Notification":
        return False
    notification_type = payload.get("notification_type")
    return notification_type == "agent_completed" or notification_type in (
        _ATTENTION_NOTIFICATIONS
    )


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
        if mapped is not None:
            kind, message, event_id, safe_payload = mapped
            bridge.enqueue_event(
                session,
                kind=kind,
                message=message,
                event_id=event_id,
                payload=safe_payload,
            )
        if mapped is not None or _is_reconciliation_hint(payload):
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
