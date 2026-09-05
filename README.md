# Hermes Claude Sessions

Hermes-managed native Claude background sessions with autonomous permission routing.

## Control model

| Layer | Responsibility |
| --- | --- |
| Hermes | Delegates the task, judges residual permission requests, monitors state, reads logs, stops owned sessions, and independently verifies the result. |
| Claude auto mode | Classifies routine tool actions against the user request, repository context, destructive-operation rules, remotes, and prompt-injection signals. |
| Permission bridge | Converts any remaining native `PermissionRequest` into a session-scoped request that Hermes can allow or deny once. |
| Human | Defines the task. No launch or routine Claude permission interaction is required for the default flow. |

The default is `--permission-mode auto`. `bypassPermissions` is not exposed.

## Event-driven wake flow

Managed sessions do not require Hermes to poll the native roster. The bundled
Claude hooks persist permission and input events. Claude `--bg` does not emit
terminal hooks consistently on natural completion, so the adapter also watches
Claude's authoritative job-state directory through the host OS event facility
(kqueue on macOS). Both paths write the same durable spool and signal an
in-process Unix datagram dispatcher. The dispatcher publishes each event into
Hermes's durable background-event ledger, and the existing completion rail
re-enters the originating conversation as a fresh internal turn.

Only semantic transitions wake Hermes: `permission_required`, `needs_input`,
`failed`, response boundaries, and meaningful terminal completion. Raw deltas
are not wake events; inspect them with `claude_session_logs` after a semantic
transition. The event spool remains authoritative if a low-latency hint is
lost, and duplicate terminal IDs are idempotent in both ledgers. A single
`claude_session_wait` may instead claim the next event inside the current turn;
that atomic claim prevents the same event from also waking a fresh turn.
Polling the roster remains a recovery fallback, not an agent loop.

## Model tools

- `claude_session_start` — launches one native Claude `--bg` session. Default
  `permission_mode` is `auto`. Each launch receives an adapter-generated unique
  native display name; Claude background mode does not accept caller-selected
  session IDs.
- `claude_session_list` — reads the authoritative native roster; optional `id`
  filters to one exact short ID and optional `limit` bounds the returned rows.
- `claude_permission_list` — lists pending requests waiting for Hermes judgment.
- `claude_permission_decide` — allows or denies one exact request once. It never installs a persistent allow rule.
- `claude_session_wait` — performs one bounded blocking wait for a semantic event. Its atomic claim prevents duplicate fresh-turn delivery.
- `claude_session_logs` — reads bounded logs for a session launched by Hermes.
- `claude_session_stop` — stops an owned session and verifies its terminal state.

The bundled operating procedure is `claude-sessions:native-claude-sessions`.

Auto-mode launches do not invoke the Hermes human approval hook. Explicit `manual`, `acceptEdits`, and `plan` launches remain human-gated.

### Task-based routing

`claude_session_start` accepts optional `model` and `effort` fields, passed to Claude as `--model` and `--effort` without a shell. Omit either field to inherit Claude's configured default for it. `model` accepts Claude aliases such as `fable`, `sonnet`, and `opus`, or a full provider model name. `effort` accepts `low`, `medium`, `high`, `xhigh`, or `max`.

Use `fable`/`low` for bounded reconnaissance, `sonnet`/`high` for normal implementation and focused review, and `opus`/`xhigh` for hard architecture, debugging, security, or adversarial review. Reserve `max` for unusually deep lanes whose expected value justifies the added latency and cost. Explicit routes are included in launch results and managed permission records so normal orchestration loops can inspect the choice.

## Managed permission flow

1. Every managed launch receives session-scoped lifecycle hooks. `auto` mode also receives the blocking permission hook; other modes receive event-only hooks.
2. Claude's classifier handles routine actions.
3. If Claude emits `PermissionRequest`, the hook writes both a bounded JSON request and a durable semantic wake event below `$HERMES_HOME/state/claude-sessions/managed/`, signals the managing Hermes process, and blocks.
4. The wake event re-enters the originating Hermes session. Hermes calls `claude_permission_list`, evaluates the exact tool and input against the delegated task and working directory, then calls `claude_permission_decide`.
5. The hook returns Claude's documented one-shot `allow` or `deny` decision and records a resolved audit artifact.
6. Missing state, malformed input, invalid decisions, and timeouts deny closed.
   If the hook process disappears while a terminal session still has a pending
   request, terminal reconciliation denies and tombstones that request after a
   bounded grace period so retirement cannot remain blocked forever.

Hermes should allow only actions needed by the user's task. It should deny credential access, authentication changes, permission changes, destructive operations, unrelated process control, uploads, publication, payment, and writes outside the delegated workspace unless the user explicitly authorized that exact effect.

## Terminal lifecycle

### Knowing whether a wake can reach you

`claude_session_start` reports `wake: "routable"` when the launching turn carries a
session identity the host can re-enter (gateway chat sessions), and `wake: "none"`
with a `wake_hint` when it does not (interactive CLI, `hermes chat -q`, cron, kanban
workers). With `none`, the agent must `claude_session_wait` before ending the turn;
otherwise the completion is only ever visible through `claude_session_list`.

### Undeliverable events

An event whose route carries no session identity (a session launched from the CLI, a
kanban worker, or before the host exposed `capture_background_event_route`) can never be
published as a wake. The dispatcher moves it to `events/undeliverable/` on the first
drain instead of retrying forever, and a publish that keeps failing is dead-lettered
after `MAX_PUBLISH_ATTEMPTS` (5). A dead-lettered record still reaches
`claude_session_wait` for its own session, and it counts as settled for terminal
retirement, so an unroutable outcome no longer pins a finished session in place.

Claude's per-job `state.json` is the sole terminal authority. Hooks only persist
attention events or signal the adapter to reconcile that native state; they do
not declare completion themselves.

Terminal retirement is a replayable state machine:

1. persist the stable terminal wake event and wait until Hermes has durably
   published or consumed it;
2. capture bounded logs to a mode-`0600` snapshot;
3. call Claude's supported `rm` command without force;
4. record either `retired` or a retryable `retirement_blocked` state;
5. serve archived logs after native retirement; and
6. compact the adapter tombstone after seven days only when the terminal event
   is durably `published` or `consumed`.

An explicit `claude_session_stop` captures bounded logs before asking Claude to
stop the native job. This preserves authorized log readback even when Claude
removes the job immediately after acknowledging the stop.

Claude refuses retirement when a worktree contains uncommitted or unpushed
work. The adapter preserves that session and retries with exponential backoff,
capped at 24 hours; it never deletes the protected worktree itself. If an
adapter-owned native job disappears from both the roster and its state path,
the adapter waits through a bounded grace period and then persists a synthetic
`missing` terminal observation before applying the same durable-event gate.

Launch failures are fenced separately. Timeout, malformed output, and other
uncertain outcomes retain their intent record for bounded roster reconciliation
using the unique launch name, canonical working directory, launch time, and the
native short-ID/full-session-ID relationship. The adapter never guesses between
multiple candidates.

Only sessions launched by this lifecycle-aware adapter (`launch_state=bound`)
are retired automatically. Legacy managed records remain observable but require
explicit cleanup. Bounded native `rm` diagnostics are retained in mode-`0600`
lifecycle state and are never copied into wake-event payloads.

## Security boundary

This is a governance boundary, not an OS sandbox. Claude and the bridge run as the same local user. The bridge limits control through:

- per-launch random state directories with mode `0700`;
- request and decision files with mode `0600`;
- exact one-shot request IDs;
- no persistent allow-rule output;
- ownership checks before logs or stop;
- strict shell-free subprocess argument arrays;
- prompt redaction and bounded output;
- fail-closed hook behavior.

The plugin performs no screen scraping or keystroke injection. It never enables `bypassPermissions`.

## Native CLI handoffs

Human observation remains available but is optional:

```sh
hermes claude agents
hermes claude attach <id>
```

These commands inherit the actual terminal.

## Prerequisites and activation

- Hermes with plugin support.
- An executable Claude binary. Resolution order is `CLAUDE_BIN`, then `PATH`, then `~/.local/bin/claude`, then the newest executable Claude Desktop bundle on macOS.

For source development:

```sh
hermes plugins doctor . --ci
```

For production, install through the normal Hermes plugin process. This checkout has no configured distribution remote; do not invent GitHub owner/repository coordinates. A source checkout placed directly in the active profile belongs at `$HERMES_HOME/plugins/claude-sessions`.

## Development checks

```sh
python -m pytest --cov=hermes_claude_sessions --cov-branch
ruff check hermes_claude_sessions tests
ruff format --check hermes_claude_sessions tests
mypy hermes_claude_sessions
hermes plugins doctor . --ci
python -m build
```

Live Claude characterization remains opt-in because it incurs cost. Set `RUN_CLAUDE_CHARACTERIZATION=1`, `CLAUDE_CHARACTERIZATION_ACK_COST=1`, and an exact executable `CLAUDE_BIN` only for an intentional live run.
