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
creates one fresh temporary Git repository and four isolated generated UUID
background sessions: one for each Notification matcher (`permission_prompt`,
`idle_prompt`, `agent_needs_input`, and `agent_completed`). Each case has a
separate temporary `--plugin-dir`, evidence directory, and `observer-*` display
name; the UUID is used only for `--session-id` and exact cleanup. The harmless
write case uses `manual`; the read-only cases use `plan`. No auto-approval mode
is used.

It writes only bounded event metadata under its temporary directory, performs a
read-only observation poll before cleanup, stops only an exact UUID that was
read back as test-owned, and verifies absence or a terminal stopped state. A
cleanup verification failure raises with the exact owned ID; it never broadens
cleanup. It also compares pre-existing unrelated session identities/states
before and after. The committed
`unattended_observer_hooks.fixture.json` is schema-only, sanitized, and is not
live evidence. Permission, lifecycle/worktree, state-normalization, and runtime
integration characterization remain deferred.
