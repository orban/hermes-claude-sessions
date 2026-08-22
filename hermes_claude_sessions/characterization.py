"""Non-mutating, opt-in Claude CLI contract characterization primitives.

This module deliberately uses argv arrays and ``shell=False``.  Its live collector
is limited to help, version, status, and agent-listing commands.
"""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

MAX_OUTPUT_BYTES = 32_768
DEFAULT_TIMEOUT_SECONDS = 10.0
TERMINATION_GRACE_SECONDS = 1.0
DRAIN_JOIN_SECONDS = 0.2
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
REQUIRED_AGENT_OBJECT_KEYS = frozenset(("id", "name", "state"))
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


_CAPABILITY_TOKEN = object()


@dataclasses.dataclass(frozen=True, init=False)
class AcknowledgedClaudeBinary:
    """Unforgeable-within-the-public-API proof of an acknowledged Claude binary."""

    _binary: Path

    def __init__(self, binary: Path, token: object) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise TypeError(
                "acknowledged Claude binaries are issued by validation only"
            )
        object.__setattr__(self, "_binary", binary)


def _require_acknowledged_binary(capability: object) -> Path:
    if not isinstance(capability, AcknowledgedClaudeBinary):
        raise CharacterizationError("a validated Claude capability is required")
    return capability._binary


@dataclasses.dataclass(frozen=True)
class CommandCapture:
    """Bounded result of one argv-only subprocess execution."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    error: str | None


def validate_prerequisites(environment: Mapping[str, str]) -> AcknowledgedClaudeBinary:
    """Issue a capability for an acknowledged absolute executable Claude binary."""
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
    return AcknowledgedClaudeBinary(binary, _CAPABILITY_TOKEN)


def _decode_bounded(value: bytes) -> str:
    """Decode stored bytes without allowing replacement characters to exceed the cap."""
    return value.decode("utf-8", errors="ignore")


def _drain_stream(stream: BinaryIO, destination: bytearray, output_limit: int) -> None:
    """Continuously drain a pipe while retaining at most ``output_limit`` bytes."""
    try:
        while chunk := stream.read(8_192):
            remaining = output_limit - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
    except (OSError, ValueError):
        # The timeout path may close a stream to unblock a lingering reader.
        pass
    finally:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a private process group, falling back to the direct child."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Force-kill a private process group, falling back to the direct child."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        process.kill()


def _close_stream(stream: BinaryIO) -> None:
    """Best-effort unblock of a drain thread after timeout containment."""
    with contextlib.suppress(OSError, ValueError):
        stream.close()


def _run_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = MAX_OUTPUT_BYTES,
) -> CommandCapture:
    """Run arbitrary argv privately with bounded concurrent pipe draining."""
    if output_limit < 0:
        raise ValueError("output_limit must not be negative")
    normalized_argv = tuple(argv)
    try:
        process = subprocess.Popen(
            normalized_argv,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        return CommandCapture(
            normalized_argv, None, "", "", False, f"{type(error).__name__}: {error}"
        )

    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    stdout_thread = threading.Thread(
        target=_drain_stream, args=(process.stdout, stdout, output_limit), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain_stream, args=(process.stderr, stderr, output_limit), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            process.wait()
        else:
            # The direct child may exit on TERM while a descendant ignores it.
            # Kill the private group anyway before returning to avoid leaving it alive.
            if os.name == "posix":
                _kill_process_tree(process)
        returncode = None
    finally:
        if timed_out:
            # A descendant that escaped containment may still hold these pipes.
            # Never let its reader threads defeat the caller's timeout contract.
            _close_stream(cast(BinaryIO, process.stdout))
            _close_stream(cast(BinaryIO, process.stderr))
            stdout_thread.join(timeout=DRAIN_JOIN_SECONDS)
            stderr_thread.join(timeout=DRAIN_JOIN_SECONDS)
        else:
            stdout_thread.join()
            stderr_thread.join()

    return CommandCapture(
        normalized_argv,
        returncode,
        _decode_bounded(bytes(stdout)),
        _decode_bounded(bytes(stderr)),
        timed_out,
        None,
    )


def run_command(
    capability: AcknowledgedClaudeBinary,
    suffix: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = MAX_OUTPUT_BYTES,
) -> CommandCapture:
    """Run an exact allowlisted command through an acknowledged Claude capability."""
    binary = _require_acknowledged_binary(capability)
    normalized_suffix = tuple(suffix)
    if normalized_suffix not in SAFE_COMMANDS:
        raise CharacterizationError("command suffix is not an allowlisted safe command")
    return _run_command(
        (str(binary), *normalized_suffix),
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
    )


def validate_agents_json_payload(payload: Any) -> None:
    """Validate the observed ``claude agents --json --all`` response schema."""
    if not isinstance(payload, list):
        raise TypeError("agents payload must be a top-level JSON array")
    for index, agent in enumerate(payload):
        if not isinstance(agent, Mapping):
            raise TypeError(f"agents payload item {index} must be an object")
        missing = REQUIRED_AGENT_OBJECT_KEYS - agent.keys()
        if missing:
            keys = ", ".join(sorted(missing))
            raise ValueError(f"agents payload item {index} is missing keys: {keys}")


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a binary without loading it all at once."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_plain_text(
    value: str,
    *,
    username: str | None,
    sensitive_values: Iterable[str],
) -> str:
    user = username if username is not None else getpass.getuser()
    result = value
    home = str(Path.home())
    if home:
        result = result.replace(home, "<HOME>")
    result = re.sub(
        r"/(?:private/)?var/folders/[^/\s]+/[^/\s]+/T(?:/[^\s]*)?",
        "<TEMP_PATH>",
        result,
    )
    result = re.sub(r"/(?:tmp|private/tmp)(?:/[^\s]*)?", "<TEMP_PATH>", result)
    if user:
        result = re.sub(re.escape(user), "<USER>", result, flags=re.IGNORECASE)
    for sensitive in sorted(set(sensitive_values), key=len, reverse=True):
        if sensitive:
            result = result.replace(sensitive, "<REDACTED>")
    result = re.sub(
        r"(?im)(\bprompt\s*(?:=|:)\s*)[^\r\n]*", r"\1<REDACTED_PROMPT>", result
    )
    result = re.sub(r"(?i)(\bnamespace\s*=\s*)[^\s,;]+", r"\1<NAMESPACE>", result)
    result = re.sub(r"(?im)(\bnamespace\s*:\s*)[^\r\n]*", r"\1<NAMESPACE>", result)
    result = re.sub(r"\b(?:ns|namespace)[_-][A-Za-z0-9_-]{8,}\b", "<NAMESPACE>", result)
    return re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<NAMESPACE>", result, flags=re.I)


def _sanitize_value(
    value: Any,
    *,
    username: str | None,
    sensitive_values: Iterable[str],
    field_name: str = "",
) -> Any:
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
            str(key): _sanitize_value(
                item,
                username=username,
                sensitive_values=sensitive_values,
                field_name=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_value(item, username=username, sensitive_values=sensitive_values)
            for item in value
        ]
    return value


def sanitize_text(
    value: str,
    *,
    username: str | None = None,
    sensitive_values: Iterable[str] = (),
) -> str:
    """Remove sensitive identity and arbitrary prompt/namespace data from text."""
    sensitive = tuple(sensitive_values)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _sanitize_plain_text(
            value, username=username, sensitive_values=sensitive
        )
    if isinstance(parsed, Mapping | list):
        sanitized = _sanitize_value(
            parsed, username=username, sensitive_values=sensitive
        )
        return json.dumps(sanitized, ensure_ascii=True, sort_keys=True)
    return _sanitize_plain_text(value, username=username, sensitive_values=sensitive)


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

    converted = _jsonable(contract)
    if not isinstance(converted, dict):  # pragma: no cover - defensive public boundary
        raise TypeError("contract must serialize to a mapping")
    return cast(
        dict[str, Any],
        _sanitize_value(
            converted,
            username=username,
            sensitive_values=tuple(sensitive_values),
        ),
    )


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


def collect_live_contract(capability: AcknowledgedClaudeBinary) -> dict[str, Any]:
    """Collect the strictly enumerated non-mutating CLI contract surface."""
    binary = _require_acknowledged_binary(capability)
    captures = [run_command(capability, suffix) for suffix in SAFE_COMMANDS]
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
