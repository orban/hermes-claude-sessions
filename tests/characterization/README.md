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
live evidence. Permission, lifecycle/state-normalization, and runtime integration characterization
remain deferred.

## Disposable worktree/edit probe (Task 0C-A)

`test_worktree_edit_live.py` is separately opt-in with the same two cost
acknowledgements and accepts only `CLAUDE_BIN=${HOME}/.local/bin/claude` after
exact resolution. It creates one fresh temporary Git repository, locally
configures only that repository, commits a test-owned `seed.txt`, snapshots
native agents and `git worktree list --porcelain`, and launches exactly one
UUID-namespaced `--bg` session in `acceptEdits` mode. The prompt authorizes
only one deterministic append to `seed.txt`; it asks for no other action.

The session-local `--plugin-dir` contains only the existing silent metadata
observer. It stores bounded event type, byte count, and `cwd` only; no raw
payload, network operation, decision hook, `WorktreeCreate`, or
`WorktreeRemove` is registered. The probe records whether hook cwd agrees with
the Git-discovered effective worktree, but treats absent hook evidence as
inconclusive rather than guessing.

After a successful launch, the exact short launch token must reconcile with one
name-matching canonical UUID absent from the pre-launch native snapshot. Every
accepted launch enters `finally`: it runs only `claude stop <exact-token>`, then
requires absence or an exact terminal row. Ambiguous identities, duplicate rows,
missing tokens, or failed cleanup fail loudly; no worktree is removed. Before
temporary teardown, the probe atomically persists only a bounded sanitized diff
and metadata, then compares all pre-existing native identity/state rows. It
reports characteristics such as isolation, edit location, primary-checkout
integrity, and post-stop artifact preservation without claiming full
characterization.
