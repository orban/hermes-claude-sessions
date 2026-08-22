"""Non-mutating, opt-in Claude CLI contract characterization primitives.

This module deliberately uses argv arrays and ``shell=False``.  Its live collector
is limited to help, version, status, and agent-listing commands.
"""

from __future__ import annotations

import dataclasses
import getpass
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

MAX_OUTPUT_BYTES = 32_768
DEFAULT_TIMEOUT_SECONDS = 10.0
REQUIRED_FLAGS = (
    "--bg",
    "--session-id",
    "--name",
    "--permission-mode",
    "--plugin-dir",
    "--model",
    "--effort",
)
REQUIRED_COMMANDS = ("attach", "logs", "stop", "respawn", "rm", "agents")
SAFE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("--version",),
    ("--help",),
    ("agents", "--help"),
    ("attach", "--help"),
    ("logs", "--help"),
    ("stop", "--help"),
    ("respawn", "--help"),
    ("rm", "--help"),
    ("daemon", "status"),
    ("agents", "--json", "--all"),
)


class CharacterizationError(ValueError):
    """Raised when an explicit opt-in safety prerequisite is not met."""


@dataclasses.dataclass(frozen=True)
class CommandCapture:
    """Bounded result of one argv-only subprocess execution."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    error: str | None


def validate_prerequisites(environment: Mapping[str, str]) -> Path:
    """Validate both acknowledgements and an absolute executable Claude binary."""
    if environment.get("RUN_CLAUDE_CHARACTERIZATION") != "1":
        raise CharacterizationError("RUN_CLAUDE_CHARACTERIZATION=1 is required")
    if environment.get("CLAUDE_CHARACTERIZATION_ACK_COST") != "1":
        raise CharacterizationError("CLAUDE_CHARACTERIZATION_ACK_COST=1 is required")
    configured = environment.get("CLAUDE_BIN", "")
    binary = Path(configured)
    if not configured or not binary.is_absolute():
        raise CharacterizationError("CLAUDE_BIN must be an absolute path")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CharacterizationError("CLAUDE_BIN must point to an executable file")
    return binary


def _bounded(value: str | bytes | None, output_limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[:output_limit]


def run_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = MAX_OUTPUT_BYTES,
) -> CommandCapture:
    """Run a bounded command without a shell, retaining failures as data."""
    normalized_argv = tuple(argv)
    try:
        completed = subprocess.run(
            normalized_argv,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        return CommandCapture(
            normalized_argv,
            None,
            _bounded(error.stdout, output_limit),
            _bounded(error.stderr, output_limit),
            True,
            None,
        )
    except OSError as error:
        return CommandCapture(
            normalized_argv, None, "", "", False, f"{type(error).__name__}: {error}"
        )
    return CommandCapture(
        normalized_argv,
        completed.returncode,
        _bounded(completed.stdout, output_limit),
        _bounded(completed.stderr, output_limit),
        False,
        None,
    )


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a binary without loading it all at once."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_text(
    value: str,
    *,
    username: str | None = None,
    sensitive_values: Iterable[str] = (),
) -> str:
    """Remove local identity, path, namespace, and caller-designated prompt data."""
    user = username if username is not None else getpass.getuser()
    result = value
    home = str(Path.home())
    if home:
        result = result.replace(home, "<HOME>")
    result = re.sub(
        r"/var/folders/[^/\s]+/[^/\s]+/T(?:/[^\s]*)?", "<TEMP_PATH>", result
    )
    result = re.sub(r"/(?:tmp|private/tmp)(?:/[^\s]*)?", "<TEMP_PATH>", result)
    if user:
        result = re.sub(re.escape(user), "<USER>", result, flags=re.IGNORECASE)
    for sensitive in sorted(set(sensitive_values), key=len, reverse=True):
        if sensitive:
            result = result.replace(sensitive, "<REDACTED>")
    result = re.sub(r"\b(?:ns|namespace)[_-][A-Za-z0-9_-]{8,}\b", "<NAMESPACE>", result)
    result = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<NAMESPACE>", result, flags=re.I
    )
    return result


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(cast(Any, value)))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def sanitize_contract(
    contract: Mapping[str, Any],
    *,
    username: str | None = None,
    sensitive_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Make a JSON-compatible contract safe to persist as a shared fixture."""

    def sanitize(value: Any, *, field_name: str = "") -> Any:
        normalized_field = field_name.lower()
        if "prompt" in normalized_field:
            return "<REDACTED_PROMPT>"
        if "namespace" in normalized_field:
            return "<NAMESPACE>"
        if isinstance(value, str):
            return sanitize_text(
                value, username=username, sensitive_values=sensitive_values
            )
        if isinstance(value, Mapping):
            return {
                str(key): sanitize(item, field_name=str(key))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    converted = _jsonable(contract)
    if not isinstance(converted, dict):  # pragma: no cover - defensive public boundary
        raise TypeError("contract must serialize to a mapping")
    return cast(dict[str, Any], sanitize(converted))


def extract_required_contract(help_text: str) -> dict[str, list[str]]:
    """Extract only the named launch flags and lifecycle commands from help text."""
    flags = sorted(
        flag for flag in REQUIRED_FLAGS if re.search(re.escape(flag), help_text)
    )
    commands = sorted(
        command
        for command in REQUIRED_COMMANDS
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(command)}(?![A-Za-z0-9_-])", help_text
        )
    )
    return {"flags": flags, "commands": commands}


def contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Produce a stable SHA-256 fingerprint from canonical JSON."""
    canonical = json.dumps(
        _jsonable(contract), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def atomic_write_contract(destination: Path, contract: Mapping[str, Any]) -> None:
    """Atomically persist a sanitized contract with its deterministic fingerprint."""
    payload = dict(sanitize_contract(contract))
    payload.pop("fingerprint", None)
    payload["fingerprint"] = contract_fingerprint(payload)
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(serialized)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_name = temporary.name
    os.replace(temporary_name, destination)


def collect_live_contract(binary: Path) -> dict[str, Any]:
    """Collect the strictly enumerated non-mutating CLI contract surface."""
    captures = [run_command([str(binary), *suffix]) for suffix in SAFE_COMMANDS]
    combined_help = "\n".join(
        f"{capture.stdout}\n{capture.stderr}"
        for suffix, capture in zip(SAFE_COMMANDS, captures, strict=True)
        if "--help" in suffix
    )
    version_capture = captures[0]
    contract: dict[str, Any] = {
        "binary": str(binary),
        "binary_sha256": sha256_file(binary),
        "version": version_capture.stdout or version_capture.stderr,
        "commands": captures,
        "required_contract": extract_required_contract(combined_help),
        "safe_command_suffixes": [list(command) for command in SAFE_COMMANDS],
    }
    return sanitize_contract(contract)
