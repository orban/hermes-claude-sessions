# Security policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the maintainer through the
private reporting channel of the repository host where you obtained this
code. Include the affected version, a minimal reproduction, and the observed
impact. Don't include real prompts, session transcripts, credentials, or
machine-specific paths unless they're required to reproduce the issue.

## Security boundaries

The plugin is a governance layer, not an OS sandbox. Hermes, the bridge, and
Claude run as the same local user. Every change must preserve these controls:

- Hermes approves the initial launch through its normal tool policy.
- `auto` mode lets Claude classify routine actions; residual requests are
  routed back to Hermes as one-shot decisions with no persistent allow rules.
- `bypassPermissions` and `--dangerously-skip-permissions` are never exposed.
- Every subprocess uses an argv array with `shell=False`, and prompts appear
  only after `--`.
- Prompts are redacted from model-facing results and permission records.
- Model-facing output, diagnostics, and snapshots are bounded.
- Logs, stop, decisions, and retirement require exact managed ownership.
- Claude's native job state remains terminal authority. Hooks signal attention
  but never declare completion by themselves.
- Native retirement never forces deletion of protected worktrees.
- Live characterization is opt-in, cost-acknowledged, and disabled in CI.

A change that weakens one of these controls is a security regression.
