---
name: native-claude-sessions
description: "Use when operating native Claude background sessions."
---

# Native Claude background sessions

1. Call `claude_session_start` only to launch one native Claude `--bg` session. Supply `name`, absolute existing `cwd`, and `prompt`; use `manual` (default), `acceptEdits`, or `plan`. Hermes requests human approval before launch. Never bypass permissions.
2. Call `claude_session_list` only for the read-only native roster/status.
3. Direct a human to `hermes claude agents` to view native agents or `hermes claude attach <id>` to interact. Those commands inherit the actual terminal; they are not model tools.
4. Let Claude's native supervisor own lifecycle, terminal UI, worktrees, permissions, and native status. Hermes does not scrape, inject keystrokes, reconcile, persist, stop, respawn, or clean up sessions.
5. Never infer state from terminal text. Do not substitute zmx except as an explicitly requested fallback when the native Claude supervisor is unavailable.
6. On an error, report the controlled result. Do not retry paid launches automatically.
