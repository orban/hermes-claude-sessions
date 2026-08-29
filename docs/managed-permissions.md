# Managed Permission Protocol

## Goal

A user delegates one task to Hermes. Hermes launches a native Claude background session, lets Claude's `auto` classifier decide routine actions, resolves any remaining permission prompts itself, monitors the owned session, and verifies the resulting artifact without requiring the user to attach to Claude.

## Protocol

1. `claude_session_start` defaults to `permission_mode=auto`.
2. The launcher creates a random session directory under `$HERMES_HOME/state/claude-sessions/managed/` and passes only that path to a bundled Claude hook plugin.
3. The native Claude process runs with `--permission-mode auto` and the bundled `PermissionRequest` hook.
4. A residual request and a bounded semantic wake event are persisted before a best-effort Unix datagram signal is sent to the managing Hermes process.
5. Hermes's durable background-event rail re-enters the originating conversation, lists pending requests, and makes one explicit `allow` or `deny` decision with a reason.
6. The hook emits Claude's documented `hookSpecificOutput.decision.behavior` response, moves the request to the resolved audit directory, and deletes the pending response.
7. Claude permission/input hooks and the OS-native job-state watcher publish semantic state changes and terminal events through the same rail. Hermes can claim one event with `claude_session_wait`, or the rail wakes a fresh turn, but never both. Hermes then reads the authoritative roster and bounded logs and independently verifies the requested outcome.

## Decision policy

Allow when all are true:

- The action is necessary for the user's delegated task.
- The target and effect stay within the delegated workspace or an explicitly authorized external target.
- The command is bounded and reviewable.
- No credential, payment, permission, publication, destructive, or unrelated process-control effect is hidden in the action.

Deny when any are true:

- The action is unrelated, broader than necessary, destructive, or irreversible.
- It accesses credentials or changes authentication/authorization.
- It writes outside the delegated workspace without explicit user authority.
- It uploads, publishes, pays, or controls unrelated sessions/processes without explicit authority.
- The request is malformed, oversized, stale, duplicated, or ambiguous.

A decision applies once. The bridge never returns `updatedPermissions`, never installs a persistent allow rule, and never exposes `bypassPermissions`.

## Failure behavior

- Missing or corrupt state: deny.
- Invalid hook input: deny.
- Oversized request: deny.
- Invalid or duplicate decision: reject the decision; the original request remains denied on timeout.
- No Hermes decision before the hook deadline: deny and resolve the audit record.
- Unowned session ID for logs or stop: reject without invoking Claude.
- Stop request: invoke the exact owned ID and read back the native roster before reporting success.

## Threat boundary

The bridge is not an OS sandbox. Claude and Hermes operate as the same macOS user, so a command that Hermes explicitly allows has that user's ambient access. The protection is a review and ownership protocol: session-scoped random state, mode-restricted files, bounded records, exact IDs, one-shot decisions, no shell interpolation, and fail-closed behavior. Stronger isolation requires a separate sandbox/container profile and is outside this plugin's scope.
