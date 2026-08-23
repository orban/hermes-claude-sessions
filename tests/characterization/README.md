# Claude CLI characterization fixtures

The live contract test is deliberately opt-in and only runs when all three
variables are set:

```sh
RUN_CLAUDE_CHARACTERIZATION=1 \
CLAUDE_CHARACTERIZATION_ACK_COST=1 \
CLAUDE_BIN=/absolute/path/to/claude \
python -m pytest tests/characterization/test_cli_contract_live.py
```

The capture invokes only these non-mutating commands: `--version`, `--help`,
`agents --help`, `attach --help`, `logs --help`, `stop --help`, `respawn --help`,
`rm --help`, `daemon status`, and `agents --json --all`. It never launches,
stops, respawns, or removes a Claude session.

Generated `contract.json` files are sanitized: user names, home paths, absolute
temporary paths, namespaces, and prompts are replaced before persistence.

## Unattended observer-hook probe (Task 0B)

The bounded automated observer probe uses the same two acknowledgements, but
only accepts `CLAUDE_BIN=${HOME}/.local/bin/claude` after exact resolution. It
creates one fresh temporary Git repository and four isolated background
sessions: one for each Notification matcher (`permission_prompt`, `idle_prompt`,
`agent_needs_input`, and `agent_completed`). Each case has a separate temporary
`--plugin-dir`, evidence directory, and UUID-suffixed `observer-*` display name.
Claude's `--bg` mode ignores `--session-id`, so the probe snapshots native state,
launches by unique name, parses the exact short cleanup token from Claude's
`backgrounded · <id> · <name>` response, then cross-checks that token and name
against exactly one returned full `sessionId`. All cases use `manual`; no
auto-approval mode is used.

It writes only bounded event metadata under its temporary directory and performs
a read-only observation poll before cleanup. Every accepted launch enters a
`finally` cleanup path that stops only its launch-returned short ID, including
identity-readback timeout, ambiguity, and observer failures. Cleanup then verifies
absence or a terminal stopped state; a verification failure raises with the exact
owned ID and never broadens cleanup. It also compares pre-existing unrelated
session identities/states before and after. The committed
`unattended_observer_hooks.fixture.json` is schema-only, sanitized, and is not
live evidence. Permission, lifecycle/worktree, state-normalization, and runtime
integration characterization remain deferred.
