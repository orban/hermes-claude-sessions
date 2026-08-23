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
only accepts the exact executable `CLAUDE_BIN=/Users/ryo/.local/bin/claude`.
It creates one fresh temporary Git repository and one generated `observer-*`
background session with a temporary `--plugin-dir`. The session-local hooks
observe only `SessionStart`, `SessionEnd`, and the Notification matchers
`permission_prompt`, `idle_prompt`, `agent_needs_input`, and `agent_completed`.

It writes only bounded event metadata under its temporary directory, stops only
the generated session ID, and verifies that exact ID is absent from the
post-cleanup listing. A cleanup verification failure raises with the exact owned
ID; it never broadens cleanup. The committed
`unattended_observer_hooks.fixture.json` is schema-only, sanitized, and is not
live evidence. Permission, lifecycle/worktree, state-normalization, and runtime
integration characterization remain deferred.
