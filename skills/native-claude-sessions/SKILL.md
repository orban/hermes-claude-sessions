---
name: native-claude-sessions
description: "Use when operating native Claude background sessions."
---

# Native Claude background sessions

1. Call `claude_session_start` with a unique name, absolute existing `cwd`, and a self-contained prompt. Omit `permission_mode` so the default `auto` classifier runs. Auto launches are unattended; never request `bypassPermissions`.
2. After launch, alternate `claude_session_list` and `claude_permission_list` until the owned session reaches a terminal state. Do not tell the user to attach for routine operation.
3. For each permission request, compare the exact `tool_name` and `tool_input` with the user's delegated task, the managed working directory, and the smallest necessary effect. Call `claude_permission_decide` once with `allow` or `deny` and a concrete reason.
4. Allow bounded build, test, lint, formatting, repository inspection, and task-required edit commands inside the delegated workspace. Deny credential access, authentication or permission changes, unrelated process control, destructive commands, uploads/publication, payment, and effects outside the workspace unless the user explicitly authorized that exact effect.
5. A denial should explain the safe constraint so Claude can adapt. Never install a persistent allow rule. Missing evidence or an ambiguous dangerous effect means deny.
6. Use `claude_session_logs` for owned-session output. Use `claude_session_stop` if the session is stalled, out of scope, or no longer needed; the tool verifies terminal state.
7. Independently inspect the produced worktree and run the project's real verification gates before reporting completion. Claude's status or logs are not completion evidence.
8. `hermes claude agents` and `hermes claude attach <id>` are optional human observation paths, not dependencies of normal delegation.
9. The bridge performs no screen scraping or keystroke injection, never enables `bypassPermissions`, and must deny closed on malformed input or timeout. Its same-user boundary is governance, not an OS sandbox.
10. Do not retry a failed paid launch automatically. Diagnose the controlled result first; launch another only when repair requires it and the user's task still authorizes the work.
