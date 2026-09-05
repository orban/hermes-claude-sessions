# Contributing

This project adapts Hermes to Claude Code's native background-session
supervisor. Changes should preserve that boundary rather than invent a second
terminal UI or process supervisor.

## Development setup

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --editable ".[dev]"
```

Python 3.11 or newer is required.

## Required checks

```sh
python -m pytest --cov=hermes_claude_sessions --cov-branch --cov-fail-under=80
ruff check .
ruff format --check .
mypy
python -m compileall -q hermes_claude_sessions tests
hermes plugins doctor . --ci
python -m build
git diff --check
```

Live characterization launches paid Claude sessions and is never run
implicitly. Run it only when you intend to pay for the probes:

```sh
RUN_CLAUDE_CHARACTERIZATION=1 \
CLAUDE_CHARACTERIZATION_ACK_COST=1 \
CLAUDE_BIN=/absolute/path/to/claude \
python -m pytest tests/characterization
```

## Ground rules

- Keep `hermes_claude_sessions` free of test-support imports.
- Keep subprocess calls shell-free and prompts after `--`.
- Never expose `bypassPermissions` or persistent allow rules.
- Preserve exact ownership checks before logs, stop, and native retirement.
- Treat Claude's job state as lifecycle authority; hooks are attention signals.
- Keep output bounded and redact prompts from every response.
- Keep live fixtures free of usernames, home paths, prompts, and machine counts.
- Keep the versions in `pyproject.toml`, `plugin.yaml`, and the bundled Claude
  plugin manifest identical.

Commit messages use imperative mood, lowercase text, no trailing period, and
fewer than 72 characters.
