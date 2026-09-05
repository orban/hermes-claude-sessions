---
name: native-claude-sessions
description: "Use when operating native Claude background sessions."
---

# Native Claude background sessions

1. Use this native adapter for all routine Claude delegation; do not route Claude through ZMX, tmux, a scraped PTY, or raw `claude --bg` commands. Call `claude_session_start` with a unique name, absolute existing `cwd`, and a self-contained prompt. Omit `permission_mode` so the default `auto` classifier runs. Auto launches are unattended; never request `bypassPermissions`.
2. Route each independent task lane deliberately:
   - Use model=`fable`, effort=`low` for bounded read-only reconnaissance, file discovery, and simple test-failure classification.
   - Use model=`sonnet`, effort=`high` for normal implementation, refactoring, focused debugging, and code review. This is the default route for a coding lane.
   - Use model=`opus`, effort=`xhigh` for hard architecture, subtle concurrency or security analysis, adversarial review, and debugging that already defeated a focused attempt.
   - Reserve `max` for unusually deep, long-running lanes where extra latency and cost are justified. Do not escalate merely because a previous launch failed for an operational reason.
   - Omit `model` and `effort` only when intentionally inheriting Claude's configured defaults. Aliases select the current Claude family version; a full provider model name is also valid.
3. After a successful launch, read the result's `wake` field. `routable`: either end the current turn or call `claude_session_wait` once when the same turn must react to the next semantic event. `none` (CLI, cron, kanban worker: no return address): you MUST call `claude_session_wait` before ending the turn, because nothing can wake you afterwards and the outcome would be orphaned. Never loop over waits, sleeps, `claude_session_list`, or `claude_permission_list`. An event returned by `claude_session_wait` is exclusively consumed; otherwise attention hooks and the native job-state watcher persist the delta and Hermes re-enters the originating session as a fresh internal turn. Hooks are reconciliation hints; Claude's native job state is the sole terminal authority. If the session's terminal event was already delivered (as a wake or by an earlier wait), `claude_session_wait` returns immediately with `status: ended` and that event; the session is finished, so do not wait on it again. Use `claude_session_list` only after an event or for an explicit reconciliation check. Do not tell the user to attach for routine operation.
4. On a `permission_required` notification, call `claude_permission_list`, compare the exact `tool_name` and `tool_input` with the user's delegated task, the managed working directory, and the smallest necessary effect, then call `claude_permission_decide` once with `allow` or `deny` and a concrete reason. The request includes the selected model and effort.
5. Allow bounded build, test, lint, formatting, repository inspection, and task-required edit commands inside the delegated workspace. Deny credential access, authentication or permission changes, unrelated process control, destructive commands, uploads/publication, payment, and effects outside the workspace unless the user explicitly authorized that exact effect.
6. A denial should explain the safe constraint so Claude can adapt. Never install a persistent allow rule. Missing evidence or an ambiguous dangerous effect means deny.
7. Use `claude_session_logs` for owned-session output; after native retirement it serves the adapter's durable bounded snapshot. The adapter snapshots logs before calling Claude's supported `rm`, preserves worktree-protected sessions, and compacts delivered tombstones after seven days. Use `claude_session_stop` if the session is stalled, out of scope, or no longer needed; the tool verifies terminal state.
8. Independently inspect the produced worktree and run the project's real verification gates before reporting completion. Claude's status or logs are not completion evidence.
9. `hermes claude agents` and `hermes claude attach <id>` are optional human observation paths, not dependencies of normal delegation.
10. The bridge performs no screen scraping or keystroke injection, never enables `bypassPermissions`, and must deny closed on malformed input or timeout. Its same-user boundary is governance, not an OS sandbox.
11. Do not retry a failed paid launch automatically. Diagnose the controlled result first; launch another only when repair requires it and the user's task still authorizes the work.
