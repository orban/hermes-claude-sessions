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
