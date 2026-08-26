#!/usr/bin/env python3
"""Executable entry point for the Hermes PermissionRequest bridge."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from hermes_claude_sessions.permission_hook import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
