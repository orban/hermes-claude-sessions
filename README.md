# Hermes Claude Sessions

A lean Hermes plugin that starts and lists native Claude background sessions while leaving Claude and the human in control of their native session experience.

## Responsibilities

| Owner | Responsibility |
| --- | --- |
| Hermes | Offers approved model-tool launch/list access and reports controlled results. |
| Claude native supervisor | Owns session lifecycle, terminal UI, worktrees, permissions, and native status. |
| Human | Approves launches and uses native terminal handoffs to observe or interact with Claude. |

## Capabilities

- Model tools: `claude_session_start` launches native Claude `--bg`; `claude_session_list` reads the native roster/status.
- Approval hook: `pre_tool_call` requests human approval for a start; it does not decide Claude permissions.
- Native CLI handoffs: `hermes claude agents` and `hermes claude attach <id>` inherit the actual terminal and are not model tools.
- Bundled, read-only plugin skill: `claude-sessions:native-claude-sessions`, loaded explicitly through `skill_view`.

## Safety and non-goals

This plugin performs no screen scraping or keystroke injection. It does not auto-approve permissions or use `bypassPermissions`, decide Claude hooks, or provide persistence/reconciliation/stop/respawn/cleanup. It never infers state from terminal text. Claude's native supervisor remains authoritative. zmx fallback only: use zmx only when the native supervisor is unavailable and the fallback is explicitly requested.

## Prerequisites and activation

- Hermes with plugin support.
- An executable Claude binary. Resolution order is `CLAUDE_BIN`, then `PATH`, then `~/.local/bin/claude`.
- `CLAUDE_BIN` is a non-secret environment capability; set it to an executable binary when the default resolver cannot find Claude.

For source development, run from this checkout without changing the active profile:

```sh
hermes plugins doctor . --ci
```

For production, install the plugin through your normal Hermes plugin installation process. If placing a source checkout directly in the plugin directory, use `$HERMES_HOME/plugins/claude-sessions`; do not copy the bundled skill into `~/.hermes/skills`.

## Model workflow

1. Ask the model to call `claude_session_start` with a unique `name`, absolute existing `cwd`, and task `prompt`. Choose `manual` (default), `acceptEdits`, or `plan`.
2. Review Hermes's approval request; do not bypass permissions.
3. Use `claude_session_list` when a read-only native roster is needed.
4. Hand terminal interaction to the human:

```sh
hermes claude agents
hermes claude attach <id>
```

Errors are returned as controlled results. Do not automatically retry a paid launch.

## Development checks

Run the deterministic checks used by this project:

```sh
python -m pytest -q
ruff check .
ruff format --check .
mypy hermes_claude_sessions
hermes plugins doctor . --ci
git diff --check
```

Live Claude characterization is opt-in and may incur cost. Run it only with an executable absolute `CLAUDE_BIN` plus both explicit acknowledgements, `RUN_CLAUDE_CHARACTERIZATION=1` and `CLAUDE_CHARACTERIZATION_ACK_COST=1`; it is not part of normal development verification.

## Native compatibility caveat

The adapter targets the observed native Claude CLI contract (`--bg`, `agents`, and `attach`), not a replacement supervisor. Native Claude CLI behavior can change; characterize a compatible installed binary before relying on live behavior.
