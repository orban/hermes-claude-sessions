"""Claude PermissionRequest hook that waits for a Hermes decision."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from . import bridge

DEFAULT_TIMEOUT_SECONDS = 540.0
MAX_TIMEOUT_SECONDS = 540.0


def _deny(message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": message,
                "interrupt": False,
            },
        }
    }


def _allow() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def _write(stdout: IO[str], value: dict[str, Any]) -> None:
    json.dump(value, stdout, ensure_ascii=False, sort_keys=True)
    stdout.write("\n")
    stdout.flush()


def _timeout(environ: Mapping[str, str]) -> float:
    try:
        value = float(environ.get(bridge.TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return min(MAX_TIMEOUT_SECONDS, max(0.01, value))


def run(
    *,
    stdin: IO[str],
    stdout: IO[str],
    environ: Mapping[str, str],
) -> int:
    request: dict[str, Any] | None = None
    session: Path | None = None
    try:
        payload = json.load(stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        if payload.get("hook_event_name") != "PermissionRequest":
            raise ValueError("unexpected hook event")
        raw_session = environ.get(bridge.SESSION_DIR_ENV)
        if not raw_session:
            raise ValueError("managed session state is unavailable")
        session = Path(raw_session)
        if not (session / "metadata.json").is_file():
            raise ValueError("managed session state is unavailable")
        request = bridge.enqueue_request(session, payload)
        bridge.enqueue_event(
            session,
            kind="permission_required",
            message=(
                "Managed Claude session is waiting for a permission decision. "
                "Inspect it with claude_permission_list and decide only against "
                "the delegated task."
            ),
            event_id=f"permission-{request['request_id']}",
            payload={"request_id": request["request_id"]},
        )
        bridge.signal_dispatcher(session)
        deadline = time.monotonic() + _timeout(environ)
        while time.monotonic() < deadline:
            response = bridge.read_response(session, str(request["request_id"]))
            if response is None:
                time.sleep(0.02)
                continue
            decision = response.get("decision")
            reason = response.get("reason")
            if decision not in {"allow", "deny"} or not isinstance(reason, str):
                decision = "deny"
                reason = "Hermes returned an invalid permission decision"
            bridge.resolve_request(session, request, decision, reason)
            _write(stdout, _allow() if decision == "allow" else _deny(reason))
            return 0

        reason = "Hermes permission decision timed out"
        bridge.resolve_request(session, request, "deny", reason)
        _write(stdout, _deny(reason))
        return 0
    except Exception:
        if session is not None and request is not None:
            bridge.resolve_request(
                session,
                request,
                "deny",
                "Hermes permission bridge failed closed",
            )
        _write(stdout, _deny("Hermes permission bridge failed closed"))
        return 0


def main(*, session_dir: str | None = None) -> int:
    environ = dict(os.environ)
    if session_dir:
        environ[bridge.SESSION_DIR_ENV] = session_dir
    return run(stdin=sys.stdin, stdout=sys.stdout, environ=environ)


if __name__ == "__main__":
    raise SystemExit(main())
