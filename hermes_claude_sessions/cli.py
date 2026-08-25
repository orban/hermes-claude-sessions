"""Direct terminal handoff to native Claude Agent View commands."""

from __future__ import annotations

import argparse
import subprocess
import sys

from .tools import resolve_claude_bin


def setup_argparse(parser: argparse.ArgumentParser) -> None:
    """Build the ``hermes claude`` command tree."""
    commands = parser.add_subparsers(dest="claude_command", required=True)
    commands.add_parser("agents", help="Open the native Claude Agent View")
    attach = commands.add_parser("attach", help="Attach to a native Claude agent")
    attach.add_argument("identifier")


def handle_cli(args: argparse.Namespace) -> int:
    """Run one native Claude terminal command with inherited terminal streams."""
    claude_bin = resolve_claude_bin()
    if claude_bin is None:
        print(
            "Claude CLI is unavailable; set CLAUDE_BIN or install an executable "
            "claude binary",
            file=sys.stderr,
        )
        return 127

    argv = [claude_bin, args.claude_command]
    if args.claude_command == "attach":
        argv.append(args.identifier)
    try:
        return subprocess.run(argv, text=True, shell=False, check=False).returncode
    except OSError:
        print(
            "Claude CLI could not be started; check CLAUDE_BIN or the claude "
            "installation",
            file=sys.stderr,
        )
        return 127
    except KeyboardInterrupt:
        return 130
