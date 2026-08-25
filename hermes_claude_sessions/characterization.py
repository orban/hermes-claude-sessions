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
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

MAX_OUTPUT_BYTES = 32_768
DEFAULT_TIMEOUT_SECONDS = 10.0
TERMINATION_GRACE_SECONDS = 1.0
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
EXPECTED_CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
# Compatibility name; its value is derived from HOME, never a committed username.
EXACT_CLAUDE_BIN = str(EXPECTED_CLAUDE_BIN)
OBSERVER_EVENT_MATCHERS = (
    "SessionStart",
    "SessionEnd",
    "permission_prompt",
    "idle_prompt",
    "agent_needs_input",
    "agent_completed",
)
OBSERVER_TIMEOUT_SECONDS = 10.0
OBSERVER_OBSERVATION_SECONDS = 2.0
OBSERVER_SUITE_TIMEOUT_SECONDS = 40.0
OBSERVER_CLEANUP_SECONDS = 3.0
OBSERVER_EVIDENCE_LIMIT = 16
OBSERVER_INPUT_LIMIT = 32_768
WORKTREE_TIMEOUT_SECONDS = 15.0
WORKTREE_SUITE_TIMEOUT_SECONDS = 75.0
WORKTREE_CLEANUP_SECONDS = 8.0
WORKTREE_DIFF_LIMIT = 8_192
_TERMINAL_STATES = frozenset(("completed", "done", "failed", "stopped"))
_SUCCESSFUL_TERMINAL_STATES = frozenset(("completed", "done"))


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


def _require_exact_observer_capability(capability: object) -> Path:
    """Narrow Task 0B to the explicitly approved executable, not any capability."""
    binary = _require_acknowledged_binary(capability)
    if binary.resolve() != EXPECTED_CLAUDE_BIN.resolve():
        raise CharacterizationError(
            "CLAUDE_BIN must resolve exactly to ${HOME}/.local/bin/claude "
            "for observer probing"
        )
    return binary


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


def _retain_chunk(destination: bytearray, chunk: bytes, output_limit: int) -> None:
    """Retain a bounded prefix while callers continue draining the whole pipe."""
    remaining = output_limit - len(destination)
    if remaining > 0:
        destination.extend(chunk[:remaining])


def _drain_nonblocking(fd: int, destination: bytearray, output_limit: int) -> bool:
    """Drain all currently available bytes and report whether the pipe reached EOF."""
    while True:
        try:
            chunk = os.read(fd, 8_192)
        except BlockingIOError:
            return False
        except OSError:
            return True
        if not chunk:
            return True
        _retain_chunk(destination, chunk, output_limit)


def _drain_stream(stream: BinaryIO, destination: bytearray, output_limit: int) -> None:
    """Windows fallback drain: daemonized and never joined without a deadline."""
    try:
        while chunk := stream.read(8_192):
            _retain_chunk(destination, chunk, output_limit)
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a private process group, falling back to the direct child."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        with contextlib.suppress(OSError):
            process.terminate()


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Force-kill a private process group, falling back to the direct child."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            process.kill()


def _close_stream(stream: BinaryIO) -> None:
    with contextlib.suppress(OSError, ValueError):
        stream.close()


def _capture_posix_until_deadline(
    process: subprocess.Popen[bytes],
    stdout: bytearray,
    stderr: bytearray,
    output_limit: int,
    deadline: float,
) -> tuple[int | None, bool, bool]:
    """Poll a direct child and both pipes under one deadline without reader threads."""
    assert process.stdout is not None
    assert process.stderr is not None
    streams = ((process.stdout, stdout), (process.stderr, stderr))
    selector = selectors.DefaultSelector()
    direct_child_reaped = False
    returncode: int | None = None
    try:
        for stream, destination in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, destination)
        while True:
            polled = process.poll()
            if polled is not None:
                returncode = polled
                direct_child_reaped = True
            if direct_child_reaped and not selector.get_map():
                return returncode, True, False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return returncode, direct_child_reaped, True
            if not selector.get_map():
                try:
                    return process.wait(timeout=remaining), True, False
                except subprocess.TimeoutExpired:
                    return None, False, True
            for key, _ in selector.select(remaining):
                destination = cast(bytearray, key.data)
                if _drain_nonblocking(key.fd, destination, output_limit):
                    selector.unregister(key.fileobj)
                    _close_stream(cast(BinaryIO, key.fileobj))
    finally:
        selector.close()
        for stream, _ in streams:
            _close_stream(cast(BinaryIO, stream))


def _capture_windows_until_deadline(
    process: subprocess.Popen[bytes],
    stdout: bytearray,
    stderr: bytearray,
    output_limit: int,
    deadline: float,
) -> tuple[int | None, bool, bool]:
    """Bounded fallback where Windows selectors cannot wait on anonymous pipes."""
    assert process.stdout is not None
    assert process.stderr is not None
    threads = tuple(
        threading.Thread(
            target=_drain_stream,
            args=(stream, destination, output_limit),
            daemon=True,
            name=f"command-{name}-drain",
        )
        for name, stream, destination in (
            ("stdout", process.stdout, stdout),
            ("stderr", process.stderr, stderr),
        )
    )
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return returncode, True, any(thread.is_alive() for thread in threads)
    except subprocess.TimeoutExpired:
        return None, False, True


def _wait_for_process_until(process: subprocess.Popen[bytes], deadline: float) -> bool:
    """Reap a direct child without blocking beyond an absolute deadline."""
    remaining = max(0.0, deadline - time.monotonic())
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
        return True
    except subprocess.TimeoutExpired:
        return False


def _run_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    overall_timeout_seconds: float | None = None,
    output_limit: int = MAX_OUTPUT_BYTES,
    cwd: Path | None = None,
) -> CommandCapture:
    """Run argv with a full execution allowance and a bounded overall deadline."""
    if output_limit < 0:
        raise ValueError("output_limit must not be negative")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must not be negative")
    if (
        overall_timeout_seconds is not None
        and overall_timeout_seconds < timeout_seconds
    ):
        raise ValueError("overall_timeout_seconds must cover timeout_seconds")
    normalized_argv = tuple(argv)
    started_at = time.monotonic()
    capture_deadline = started_at + timeout_seconds
    total_allowance = (
        timeout_seconds + TERMINATION_GRACE_SECONDS
        if overall_timeout_seconds is None
        else overall_timeout_seconds
    )
    hard_deadline = started_at + total_allowance
    try:
        process = subprocess.Popen(
            normalized_argv,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
            cwd=cwd,
        )
    except OSError as error:
        return CommandCapture(
            normalized_argv, None, "", "", False, f"{type(error).__name__}: {error}"
        )

    stdout = bytearray()
    stderr = bytearray()
    if os.name == "posix":
        returncode, direct_child_reaped, timed_out = _capture_posix_until_deadline(
            process, stdout, stderr, output_limit, capture_deadline
        )
    else:
        returncode, direct_child_reaped, timed_out = _capture_windows_until_deadline(
            process, stdout, stderr, output_limit, capture_deadline
        )

    if timed_out:
        _terminate_process_tree(process)
        if not direct_child_reaped:
            remaining = max(0.0, hard_deadline - time.monotonic())
            term_deadline = min(hard_deadline, time.monotonic() + (remaining / 2))
            direct_child_reaped = _wait_for_process_until(process, term_deadline)
        # The direct child can exit while a contained descendant holds a pipe.
        # Force the original POSIX group after the bounded TERM chance.
        if os.name == "posix" or not direct_child_reaped:
            _kill_process_tree(process)
        if not direct_child_reaped:
            direct_child_reaped = _wait_for_process_until(process, hard_deadline)
        if os.name != "posix":
            assert process.stdout is not None
            assert process.stderr is not None
            _close_stream(cast(BinaryIO, process.stdout))
            _close_stream(cast(BinaryIO, process.stderr))

    return CommandCapture(
        normalized_argv,
        returncode,
        _decode_bounded(bytes(stdout)),
        _decode_bounded(bytes(stderr)),
        timed_out,
        None,
    )


def _run_command_within_total_budget(
    argv: Sequence[str],
    *,
    total_timeout_seconds: float,
    output_limit: int = MAX_OUTPUT_BYTES,
    cwd: Path | None = None,
) -> CommandCapture:
    """Run under a hard total budget, explicitly reserving bounded teardown time."""
    if total_timeout_seconds < 0:
        raise ValueError("total_timeout_seconds must not be negative")
    teardown_reserve = min(TERMINATION_GRACE_SECONDS, total_timeout_seconds / 2)
    return _run_command(
        argv,
        timeout_seconds=total_timeout_seconds - teardown_reserve,
        overall_timeout_seconds=total_timeout_seconds,
        output_limit=output_limit,
        cwd=cwd,
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
    result = re.sub(
        r"(?<![A-Za-z0-9:/])/(?!/)(?:\\ |[^\s\"'<>])+",
        "<LOCAL_PATH>",
        result,
    )
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
        if (
            normalized_field
            in {
                "origin_cwd",
                "worktree_path",
                "effective_cwd",
            }
            and Path(value).is_absolute()
        ):
            return "<LOCAL_PATH>"
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


def _observer_program() -> str:
    """Return the deliberately silent, bounded, local-only hook observer program."""
    return f"""import json
import os
import signal
import sys
import tempfile
from pathlib import Path

LIMIT = {OBSERVER_INPUT_LIMIT}
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))
signal.alarm(5)
try:
    event = sys.argv[1]
    payload = sys.stdin.buffer.read(LIMIT + 1)
    parsed = json.loads(payload[:LIMIT].decode("utf-8", "ignore"))
    cwd = parsed.get("cwd") if isinstance(parsed, dict) else None
    record = {{
        "event": event,
        "input_bytes": min(len(payload), LIMIT),
        "input_truncated": len(payload) > LIMIT,
        "cwd": cwd if isinstance(cwd, str) and len(cwd) <= 4096 else None,
    }}
    destination = Path(os.environ["CLAUDE_OBSERVER_EVIDENCE_DIR"])
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination, prefix="event-",
        suffix=".json", delete=False,
    ) as output:
        json.dump(record, output, sort_keys=True, ensure_ascii=True)
        output.flush()
        os.fsync(output.fileno())
except Exception:
    pass
finally:
    signal.alarm(0)
sys.exit(0)
"""


def _create_observer_plugin(plugin_directory: Path, evidence_directory: Path) -> None:
    """Create a silent observer plugin, quoting every shell-interpreted path."""
    hooks_directory = plugin_directory / "hooks"
    hooks_directory.mkdir(parents=True)
    metadata_directory = plugin_directory / ".claude-plugin"
    metadata_directory.mkdir()
    (metadata_directory / "plugin.json").write_text(
        json.dumps({"name": "observer-probe", "version": "0.0.0"}), encoding="utf-8"
    )
    observer = hooks_directory / "observer.py"
    observer.write_text(_observer_program(), encoding="utf-8")
    observer.chmod(0o700)
    command_prefix = (
        f"CLAUDE_OBSERVER_EVIDENCE_DIR={shlex.quote(str(evidence_directory))} "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(observer))}"
    )
    hooks: dict[str, list[dict[str, Any]]] = {
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": f"{command_prefix} SessionStart"}
                ]
            }
        ],
        "SessionEnd": [
            {"hooks": [{"type": "command", "command": f"{command_prefix} SessionEnd"}]}
        ],
        "Notification": [
            {
                "matcher": matcher,
                "hooks": [
                    {"type": "command", "command": f"{command_prefix} {matcher}"}
                ],
            }
            for matcher in OBSERVER_EVENT_MATCHERS[2:]
        ],
    }
    (hooks_directory / "hooks.json").write_text(
        json.dumps({"hooks": hooks}, sort_keys=True), encoding="utf-8"
    )


@dataclasses.dataclass(frozen=True)
class _OwnedProbe:
    display_name: str
    matcher: str
    session_id: str | None = None
    display_id: str | None = None


def _owned_session_id() -> str:
    """Mint a UUID namespace used only to make the probe name unique."""
    return str(uuid.uuid4())


def _mint_owned_probe(matcher: str) -> _OwnedProbe:
    namespace = _owned_session_id()
    return _OwnedProbe(f"observer-{matcher}-{namespace}", matcher)


def _launch_display_id(output: str, owned: _OwnedProbe) -> str | None:
    """Extract Claude's exact short cleanup token from a successful bg launch."""
    pattern = re.compile(
        rf"^backgrounded · ([0-9a-f]{{8}}) · {re.escape(owned.display_name)}$",
        re.MULTILINE,
    )
    matches = pattern.findall(output)
    return matches[0] if len(matches) == 1 else None


def _agent_identity(item: Mapping[str, Any]) -> str | None:
    full = item.get("sessionId")
    short = item.get("id")
    return full if isinstance(full, str) else short if isinstance(short, str) else None


def _owned_rows(rows: object, owned: _OwnedProbe) -> list[Mapping[str, Any]]:
    if not isinstance(rows, list):
        return []
    matches: list[Mapping[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        if owned.session_id is not None:
            if (
                item.get("sessionId") == owned.session_id
                and item.get("id") == owned.display_id
                and item.get("name") == owned.display_name
            ):
                matches.append(item)
            continue
        if owned.display_id is not None:
            if (
                item.get("id") == owned.display_id
                and item.get("name") == owned.display_name
            ):
                matches.append(item)
            continue
        if item.get("name") == owned.display_name:
            matches.append(item)
    return matches


def _owned_row(rows: object, owned: _OwnedProbe) -> Mapping[str, Any] | None:
    matches = _owned_rows(rows, owned)
    return matches[0] if len(matches) == 1 else None


def _listing(
    binary: Path, owned: _OwnedProbe, cwd: Path, timeout: float
) -> CommandCapture:
    return _run_owned_observer_command(
        binary, owned, (str(binary), "agents", "--json", "--all"), cwd, timeout
    )


def _run_owned_observer_command(
    binary: Path, owned: _OwnedProbe, argv: tuple[str, ...], cwd: Path, timeout: float
) -> CommandCapture:
    """Execute only collector-minted ownership identities and exact private shapes."""
    if not argv or argv[0] != str(binary):
        raise CharacterizationError("observer command must use the validated binary")
    suffix = argv[1:]
    is_listing = suffix == ("agents", "--json", "--all")
    is_stop = owned.display_id is not None and suffix == ("stop", owned.display_id)
    expected_mode = "manual"
    is_launch = (
        len(suffix) == 8
        and suffix[:3] == ("--bg", "--name", owned.display_name)
        and suffix[3:5] == ("--permission-mode", expected_mode)
        and suffix[5] == "--plugin-dir"
        and suffix[7] == _probe_prompt(owned.matcher)
    )
    if not (is_listing or is_stop or is_launch):
        raise CharacterizationError("observer command shape is not probe-owned")
    return _run_command_within_total_budget(
        argv, total_timeout_seconds=timeout, cwd=cwd
    )


def _read_evidence(evidence_directory: Path) -> set[str]:
    observed: set[str] = set()
    for path in sorted(evidence_directory.glob("*.json"))[:OBSERVER_EVIDENCE_LIMIT]:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(candidate, Mapping)
            and candidate.get("event") in OBSERVER_EVENT_MATCHERS
        ):
            observed.add(cast(str, candidate["event"]))
    return observed


def _snapshot_agents(capture: CommandCapture) -> dict[str, str] | None:
    if capture.returncode != 0 or capture.timed_out:
        return None
    try:
        rows = json.loads(capture.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    snapshot: dict[str, str] = {}
    for item in rows:
        if isinstance(item, Mapping):
            identity = _agent_identity(item)
            state = item.get("state")
            if identity is not None and isinstance(state, str):
                snapshot[identity] = state
    return snapshot


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _observe_exact_owned(
    binary: Path, owned: _OwnedProbe, cwd: Path, evidence: Path, deadline: float
) -> tuple[bool, bool, set[str]]:
    """Read-only poll before cleanup and return identity, completion, evidence."""
    identity_seen = False
    completed = False
    observed: set[str] = set()
    observation_deadline = min(
        deadline, time.monotonic() + OBSERVER_OBSERVATION_SECONDS
    )
    while _remaining(observation_deadline) > 0:
        capture = _listing(
            binary,
            owned,
            cwd,
            min(OBSERVER_TIMEOUT_SECONDS, _remaining(observation_deadline)),
        )
        if capture.returncode == 0 and not capture.timed_out:
            try:
                row = _owned_row(json.loads(capture.stdout), owned)
            except json.JSONDecodeError:
                row = None
            if row is not None:
                identity_seen = True
                state = row.get("state")
                completed = isinstance(state, str) and state in _TERMINAL_STATES
        observed |= _read_evidence(evidence)
        time.sleep(min(0.2, _remaining(observation_deadline)))
    return identity_seen, completed, observed


def _discover_native_identity(
    binary: Path,
    owned: _OwnedProbe,
    cwd: Path,
    pre_existing: Mapping[str, str],
    deadline: float,
) -> _OwnedProbe | None:
    """Bind a unique test-owned name to Claude's generated short/full identities."""
    while _remaining(deadline) > 0:
        capture = _listing(
            binary, owned, cwd, min(OBSERVER_TIMEOUT_SECONDS, _remaining(deadline))
        )
        if capture.returncode == 0 and not capture.timed_out:
            try:
                rows = json.loads(capture.stdout)
            except json.JSONDecodeError:
                rows = None
            matches = _owned_rows(rows, owned)
            if len(matches) > 1:
                raise CharacterizationError(
                    "ambiguous native identity for exact owned name "
                    f"{owned.display_name}"
                )
            if matches:
                row = matches[0]
                full = row.get("sessionId")
                short = row.get("id")
                canonical_uuid = False
                if isinstance(full, str):
                    try:
                        canonical_uuid = str(uuid.UUID(full)) == full
                    except ValueError:
                        canonical_uuid = False
                if (
                    isinstance(full, str)
                    and isinstance(short, str)
                    and canonical_uuid
                    and full not in pre_existing
                    and full[:8] == short
                ):
                    return dataclasses.replace(owned, session_id=full, display_id=short)
        time.sleep(min(0.2, _remaining(deadline)))
    return None


def _cleanup_owned(
    binary: Path, owned: _OwnedProbe, cwd: Path, deadline: float
) -> tuple[bool, CommandCapture | None, CommandCapture | None]:
    """Stop only a read-back exact UUID and accept absence or terminal state."""
    if _remaining(deadline) <= 0:
        return False, None, None
    stop = _run_owned_observer_command(
        binary,
        owned,
        (str(binary), "stop", cast(str, owned.display_id)),
        cwd,
        min(OBSERVER_TIMEOUT_SECONDS, _remaining(deadline)),
    )
    latest: CommandCapture | None = None
    while _remaining(deadline) > 0:
        latest = _listing(
            binary, owned, cwd, min(OBSERVER_TIMEOUT_SECONDS, _remaining(deadline))
        )
        if latest.returncode == 0 and not latest.timed_out:
            try:
                matches = _owned_rows(json.loads(latest.stdout), owned)
            except json.JSONDecodeError:
                matches = None
            absent = matches is not None and len(matches) == 0
            terminal = (
                matches is not None
                and len(matches) == 1
                and (
                    isinstance(matches[0].get("state"), str)
                    and matches[0]["state"] in _TERMINAL_STATES
                )
            )
            if absent or terminal:
                return stop.returncode == 0 and not stop.timed_out, stop, latest
        time.sleep(min(0.2, _remaining(deadline)))
    return False, stop, latest


def _probe_prompt(matcher: str) -> str:
    prompts = {
        "permission_prompt": (
            "In this disposable repository, create only permission-probe.txt "
            "containing probe. Request permission if needed, then stop."
        ),
        "idle_prompt": (
            "Read only. Do not edit files. Wait for further input after stating "
            "you need it."
        ),
        "agent_needs_input": (
            "Read only. Ask one specific question needed before proceeding; "
            "make no edits."
        ),
        "agent_completed": (
            "Read only. Inspect the repository and respond with exactly one "
            "sentence that the bounded probe is complete."
        ),
    }
    return prompts[matcher]


def _event_rows(per_case: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    lifecycle = set().union(*(case["observed"] for case in per_case.values()))
    rows: list[dict[str, Any]] = []
    for event in OBSERVER_EVENT_MATCHERS:
        case = per_case.get(event)
        seen = event in (case["observed"] if case else lifecycle)
        clean = bool(
            case
            and case["identity_seen"]
            and case["completed"]
            and case["cleanup_confirmed"]
        )
        metadata_records = (
            int(case["metadata_records"])
            if case
            else sum(int(item["metadata_records"]) for item in per_case.values())
        )
        rows.append(
            {
                "event": event,
                "status": "observed"
                if seen
                else "not_observed"
                if clean
                else "probe_inconclusive",
                "evidence": {
                    "metadata_records": metadata_records,
                    "raw_retained": False,
                    "unattended_context": "unattended_bg",
                },
            }
        )
    return rows


def collect_unattended_observer_hook_characterization(
    capability: object, destination: Path
) -> dict[str, Any]:
    """Bounded isolated matcher probes, with exact UUID read-back and cleanup."""
    binary = _require_exact_observer_capability(capability)
    deadline = time.monotonic() + OBSERVER_SUITE_TIMEOUT_SECONDS
    cases = OBSERVER_EVENT_MATCHERS[2:]
    owned_cases = [(matcher, _mint_owned_probe(matcher)) for matcher in cases]
    per_case: dict[str, dict[str, Any]] = {}
    cleanup_failures: list[str] = []
    probe_failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="claude-observer-") as temporary_name:
        root = Path(temporary_name)
        repository = root / "repository"
        repository.mkdir()
        initialized = _run_command_within_total_budget(
            ("git", "init", "--quiet"),
            total_timeout_seconds=min(OBSERVER_TIMEOUT_SECONDS, _remaining(deadline)),
            cwd=repository,
        )
        if initialized.returncode != 0 or initialized.timed_out:
            raise CharacterizationError(
                "failed to initialize fresh probe Git repository"
            )
        before = _snapshot_agents(
            _listing(
                binary,
                owned_cases[0][1],
                repository,
                min(OBSERVER_TIMEOUT_SECONDS, _remaining(deadline)),
            )
        )
        if before is None:
            raise CharacterizationError("could not capture pre-probe agents listing")
        for case_index, (matcher, owned) in enumerate(owned_cases):
            if _remaining(deadline) <= 0:
                break
            remaining_cases = len(owned_cases) - case_index
            work_deadline = deadline - (remaining_cases * OBSERVER_CLEANUP_SECONDS)
            if _remaining(work_deadline) <= 0:
                break
            case_root = root / matcher
            evidence = case_root / "evidence"
            evidence.mkdir(parents=True)
            plugin = case_root / "plugin"
            _create_observer_plugin(plugin, evidence)
            mode = "manual"
            launch = _run_owned_observer_command(
                binary,
                owned,
                (
                    str(binary),
                    "--bg",
                    "--name",
                    owned.display_name,
                    "--permission-mode",
                    mode,
                    "--plugin-dir",
                    str(plugin),
                    _probe_prompt(matcher),
                ),
                repository,
                min(OBSERVER_TIMEOUT_SECONDS, _remaining(work_deadline)),
            )
            launch_succeeded = launch.returncode == 0 and not launch.timed_out
            launch_display_id = (
                _launch_display_id(launch.stdout, owned) if launch_succeeded else None
            )
            if launch_display_id is not None:
                owned = dataclasses.replace(owned, display_id=launch_display_id)
            identity_seen = False
            completed = False
            observed: set[str] = set()
            cleanup_confirmed = False
            stop: CommandCapture | None = None
            after: CommandCapture | None = None
            probe_error: str | None = None
            try:
                if not launch_succeeded:
                    probe_error = "background launch failed"
                elif launch_display_id is None:
                    probe_error = "successful launch omitted exact cleanup token"
                else:
                    discovered = _discover_native_identity(
                        binary, owned, repository, before, work_deadline
                    )
                    if discovered is None:
                        probe_error = "native identity read-back timed out"
                    else:
                        owned = discovered
                        identity_seen = True
                        observed_identity_seen, completed, observed = (
                            _observe_exact_owned(
                                binary, owned, repository, evidence, work_deadline
                            )
                        )
                        identity_seen = identity_seen or observed_identity_seen
            except Exception as error:  # cleanup is mandatory after accepted launch
                probe_error = f"post-launch {type(error).__name__}"
            finally:
                if owned.display_id is not None:
                    cleanup_deadline = min(
                        deadline, time.monotonic() + OBSERVER_CLEANUP_SECONDS
                    )
                    cleanup_confirmed, stop, after = _cleanup_owned(
                        binary, owned, repository, cleanup_deadline
                    )
                else:
                    # Without Claude's launch token, stop is not safely authorized.
                    after = _listing(
                        binary,
                        owned,
                        repository,
                        min(OBSERVER_TIMEOUT_SECONDS, _remaining(deadline)),
                    )
                    if after.returncode == 0 and not after.timed_out:
                        try:
                            cleanup_confirmed = (
                                len(_owned_rows(json.loads(after.stdout), owned)) == 0
                            )
                        except json.JSONDecodeError:
                            cleanup_confirmed = False
            observed |= _read_evidence(evidence)
            if probe_error is not None:
                probe_failures.append(f"{matcher}: {probe_error}")
            if not cleanup_confirmed:
                cleanup_failures.append(owned.session_id or owned.display_name)
            per_case[matcher] = {
                "owned": owned,
                "launch": launch,
                "identity_seen": identity_seen,
                "completed": completed,
                "observed": observed,
                "metadata_records": len(list(evidence.glob("*.json"))),
                "cleanup_confirmed": cleanup_confirmed,
                "stop": stop,
                "after": after,
                "probe_error": probe_error,
            }
        events = _event_rows(per_case)
        after_unrelated = _snapshot_agents(
            _listing(
                binary,
                owned_cases[0][1],
                repository,
                min(OBSERVER_TIMEOUT_SECONDS, _remaining(deadline)),
            )
        )
        unrelated_untouched = (
            after_unrelated is not None
            and {
                identity: state
                for identity, state in after_unrelated.items()
                if identity in before
            }
            == before
        )
        result: dict[str, Any] = {
            "schema_version": 2,
            "context": "unattended_bg",
            "fully_characterized": False,
            "manual_gates_deferred": ["permission", "lifecycle", "worktree"],
            "estimated_test_owned_session_count": len(cases),
            "actual_test_owned_session_count": sum(
                int(case["identity_seen"]) for case in per_case.values()
            ),
            "events": events,
            "probes": [
                {
                    "matcher": matcher,
                    "display_name": case["owned"].display_name,
                    "launch_token_reconciled": (
                        case["owned"].display_id is not None
                        and case["owned"].session_id is not None
                    ),
                    "identity_seen": case["identity_seen"],
                    "completed": case["completed"],
                    "cleanup_confirmed": case["cleanup_confirmed"],
                    "launch_returncode": case["launch"].returncode,
                    "launch_timed_out": case["launch"].timed_out,
                    "launch_error": case["launch"].error,
                    "launch_stderr_present": bool(case["launch"].stderr),
                    "launch_stderr_bytes": len(case["launch"].stderr.encode("utf-8")),
                    "metadata_records": case["metadata_records"],
                    "probe_error": case["probe_error"],
                }
                for matcher, case in per_case.items()
            ],
            "cleanup": {
                "confirmed": not cleanup_failures,
                "failed_owned_session_ids": cleanup_failures,
            },
            "suite_deadline_seconds": OBSERVER_SUITE_TIMEOUT_SECONDS,
            "unrelated_agents": {
                "pre_existing_count": len(before),
                "unchanged": unrelated_untouched,
            },
        }
        atomic_write_contract(destination, result)
        if result["actual_test_owned_session_count"] != len(cases):
            raise CharacterizationError(
                "one or more exact owned observer sessions failed identity read-back"
            )
        if cleanup_failures:
            raise CharacterizationError(
                "cleanup could not be confirmed for exact owned session "
                + ", ".join(cleanup_failures)
            )
        if probe_failures:
            raise CharacterizationError(
                "one or more observer probes failed: " + "; ".join(probe_failures)
            )
        if not unrelated_untouched:
            raise CharacterizationError(
                "pre-existing unrelated agent states changed during probe"
            )
        return sanitize_contract(result)


@dataclasses.dataclass(frozen=True)
class _WorktreeProbe:
    """One exact-owned disposable edit probe; never a public command target."""

    display_name: str
    session_id: str | None = None
    display_id: str | None = None


def _mint_worktree_probe() -> _WorktreeProbe:
    return _WorktreeProbe(f"worktree-edit-{uuid.uuid4()}")


def _worktree_prompt() -> str:
    return (
        "In this disposable Git repository, edit exactly one test-owned file named "
        "seed.txt by appending exactly the line CHARACTERIZATION_EDIT. Do no action "
        "elsewhere, do not run tests, do not create files, and then stop."
    )


def _worktree_launch_argv(
    binary: Path, probe: _WorktreeProbe, plugin_directory: Path
) -> tuple[str, ...]:
    return (
        str(binary),
        "--bg",
        "--name",
        probe.display_name,
        "--permission-mode",
        "acceptEdits",
        "--plugin-dir",
        str(plugin_directory),
        _worktree_prompt(),
    )


def _run_owned_worktree_command(
    binary: Path,
    probe: _WorktreeProbe,
    argv: tuple[str, ...],
    cwd: Path,
    timeout: float,
) -> CommandCapture:
    """Allow only the one owned launch, exact listing, and exact-token stop."""
    if not argv or argv[0] != str(binary):
        raise CharacterizationError("worktree command must use the validated binary")
    suffix = argv[1:]
    allowed = (
        suffix == ("agents", "--json", "--all")
        or (probe.display_id is not None and suffix == ("stop", probe.display_id))
        or suffix == _worktree_launch_argv(binary, probe, Path("<plugin>"))[1:-2]
    )
    # The plugin path is deliberately dynamic; compare fixed launch fields separately.
    launch = (
        len(suffix) == 8
        and suffix[:5]
        == ("--bg", "--name", probe.display_name, "--permission-mode", "acceptEdits")
        and suffix[5] == "--plugin-dir"
        and suffix[7] == _worktree_prompt()
    )
    if not (allowed or launch):
        raise CharacterizationError("worktree command shape is not probe-owned")
    return _run_command_within_total_budget(
        argv, total_timeout_seconds=timeout, cwd=cwd
    )


def _run_git(argv: tuple[str, ...], cwd: Path, deadline: float) -> CommandCapture:
    remaining = _remaining(deadline)
    if remaining <= 0:
        return CommandCapture(argv, None, "", "", True, "suite deadline exhausted")
    return _run_command_within_total_budget(
        argv, total_timeout_seconds=min(WORKTREE_TIMEOUT_SECONDS, remaining), cwd=cwd
    )


def _require_git_success(capture: CommandCapture, operation: str) -> None:
    if capture.returncode != 0 or capture.timed_out:
        raise CharacterizationError(f"failed disposable repository {operation}")


def _initialize_seed_repository(repository: Path, deadline: float) -> None:
    _require_git_success(
        _run_git(("git", "init", "--quiet"), repository, deadline), "init"
    )
    _require_git_success(
        _run_git(
            ("git", "config", "user.email", "probe@example.invalid"),
            repository,
            deadline,
        ),
        "configuration",
    )
    _require_git_success(
        _run_git(
            ("git", "config", "user.name", "Claude characterization"),
            repository,
            deadline,
        ),
        "configuration",
    )
    _require_git_success(
        _run_git(("git", "config", "commit.gpgsign", "false"), repository, deadline),
        "configuration",
    )
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    _require_git_success(
        _run_git(("git", "add", "seed.txt"), repository, deadline), "add"
    )
    _require_git_success(
        _run_git(("git", "commit", "--quiet", "-m", "seed"), repository, deadline),
        "commit",
    )


def _worktree_snapshot(repository: Path, deadline: float) -> str:
    capture = _run_git(("git", "worktree", "list", "--porcelain"), repository, deadline)
    _require_git_success(capture, "worktree listing")
    return capture.stdout


def _worktree_paths(porcelain: str) -> dict[str, str | None]:
    entries: dict[str, str | None] = {}
    current: str | None = None
    attributes_seen: set[str] = set()
    checkout_kind: str | None = None
    for line in porcelain.splitlines():
        if not line:
            current = None
            attributes_seen = set()
            checkout_kind = None
            continue
        if line.startswith("worktree "):
            if current is not None:
                raise CharacterizationError(
                    "malformed worktree porcelain record boundary"
                )
            current = line.removeprefix("worktree ")
            if not current or current in entries:
                raise CharacterizationError(
                    "duplicate or empty worktree porcelain path"
                )
            entries[current] = None
            attributes_seen = set()
            checkout_kind = None
            continue
        if current is None:
            raise CharacterizationError("malformed worktree porcelain record")

        attribute, separator, value = line.partition(" ")
        if attribute not in {
            "HEAD",
            "branch",
            "detached",
            "bare",
            "locked",
            "prunable",
        }:
            raise CharacterizationError("unknown worktree porcelain record attribute")
        if attribute in attributes_seen:
            raise CharacterizationError("duplicate worktree porcelain record attribute")
        attributes_seen.add(attribute)

        if attribute == "HEAD":
            if not separator or not value:
                raise CharacterizationError("malformed worktree porcelain HEAD record")
            continue
        if attribute == "branch":
            if not separator or not value or checkout_kind is not None:
                raise CharacterizationError(
                    "malformed worktree porcelain branch record"
                )
            entries[current] = value
            checkout_kind = attribute
            continue
        if attribute in {"detached", "bare"}:
            if separator or checkout_kind is not None:
                raise CharacterizationError(
                    "malformed worktree porcelain checkout record"
                )
            checkout_kind = attribute
            continue
        if separator and not value:
            raise CharacterizationError("malformed worktree porcelain status record")
    return entries


def _preserve_worktree_diagnostics(root: Path) -> Path:
    """Move one exact temporary probe root aside when safe cleanup is unproven."""
    directory = tempfile.mkdtemp(prefix="claude-worktree-diagnostics-")
    destination = Path(directory) / "probe"
    os.replace(root, destination)
    return destination


def _discover_worktree_identity(
    binary: Path,
    probe: _WorktreeProbe,
    repository: Path,
    preexisting: Mapping[str, str],
    deadline: float,
) -> _WorktreeProbe | None:
    while _remaining(deadline) > 0:
        listing = _run_owned_worktree_command(
            binary,
            probe,
            (str(binary), "agents", "--json", "--all"),
            repository,
            min(WORKTREE_TIMEOUT_SECONDS, _remaining(deadline)),
        )
        if listing.returncode == 0 and not listing.timed_out:
            try:
                matches = _owned_rows(
                    json.loads(listing.stdout),
                    _OwnedProbe(
                        probe.display_name,
                        "worktree",
                        display_id=probe.display_id,
                    ),
                )
            except json.JSONDecodeError:
                matches = []
            if len(matches) > 1:
                raise CharacterizationError("ambiguous worktree probe native identity")
            if len(matches) == 1:
                row = matches[0]
                full, short = row.get("sessionId"), row.get("id")
                try:
                    valid = isinstance(full, str) and str(uuid.UUID(full)) == full
                except ValueError:
                    valid = False
                if (
                    valid
                    and isinstance(full, str)
                    and isinstance(short, str)
                    and full not in preexisting
                    and short == probe.display_id
                    and full[:8] == short
                ):
                    return dataclasses.replace(probe, session_id=full, display_id=short)
        time.sleep(min(0.2, _remaining(deadline)))
    return None


def _worktree_cleanup(
    binary: Path, probe: _WorktreeProbe, repository: Path, deadline: float
) -> bool:
    if probe.display_id is None or _remaining(deadline) <= 0:
        return False
    stop = _run_owned_worktree_command(
        binary,
        probe,
        (str(binary), "stop", probe.display_id),
        repository,
        min(WORKTREE_TIMEOUT_SECONDS, _remaining(deadline)),
    )
    if stop.returncode != 0 or stop.timed_out:
        return False
    while _remaining(deadline) > 0:
        listing = _run_owned_worktree_command(
            binary,
            probe,
            (str(binary), "agents", "--json", "--all"),
            repository,
            min(WORKTREE_TIMEOUT_SECONDS, _remaining(deadline)),
        )
        if listing.returncode == 0 and not listing.timed_out:
            try:
                matches = _owned_rows(
                    json.loads(listing.stdout),
                    _OwnedProbe(
                        probe.display_name,
                        "worktree",
                        probe.session_id,
                        probe.display_id,
                    ),
                )
            except json.JSONDecodeError:
                return False
            if len(matches) == 0:
                return True
            if len(matches) == 1 and matches[0].get("state") in _TERMINAL_STATES:
                return True
            if len(matches) > 1:
                return False
        time.sleep(min(0.2, _remaining(deadline)))
    return False


def _evidence_cwds(evidence_directory: Path) -> set[str]:
    cwds: set[str] = set()
    for path in sorted(evidence_directory.glob("*.json"))[:OBSERVER_EVIDENCE_LIMIT]:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(candidate, Mapping) and isinstance(candidate.get("cwd"), str):
            cwds.add(cast(str, candidate["cwd"]))
    return cwds


def _observe_worktree_probe(
    binary: Path,
    probe: _WorktreeProbe,
    repository: Path,
    before_worktrees: str,
    evidence_directory: Path,
    deadline: float,
) -> dict[str, Any]:
    """Poll native completion and bounded artifacts; never remove a worktree."""
    deadline = min(deadline, time.monotonic() + WORKTREE_TIMEOUT_SECONDS)
    before_paths = _worktree_paths(before_worktrees)
    latest: dict[str, Any] = {}
    native_terminal = False
    while _remaining(deadline) > 0:
        listing = _run_owned_worktree_command(
            binary,
            probe,
            (str(binary), "agents", "--json", "--all"),
            repository,
            min(WORKTREE_TIMEOUT_SECONDS, _remaining(deadline)),
        )
        if listing.returncode != 0 or listing.timed_out:
            time.sleep(min(0.2, _remaining(deadline)))
            continue
        try:
            matches = _owned_rows(
                json.loads(listing.stdout),
                _OwnedProbe(
                    probe.display_name,
                    "worktree",
                    probe.session_id,
                    probe.display_id,
                ),
            )
        except json.JSONDecodeError:
            matches = []
        if len(matches) > 1:
            raise CharacterizationError("ambiguous worktree probe during observation")
        after_worktrees = _worktree_snapshot(repository, deadline)
        after_paths = _worktree_paths(after_worktrees)
        created = {
            path: branch
            for path, branch in after_paths.items()
            if path not in before_paths
        }
        if len(created) > 1:
            raise CharacterizationError("ambiguous created worktree paths")
        worktree_path, branch = next(iter(created.items()), (None, None))
        effective = Path(worktree_path) if worktree_path is not None else None
        requested_edit_landed = False
        diff = ""
        if effective is not None and effective.is_dir():
            seed = effective / "seed.txt"
            requested_edit_landed = (
                seed.read_text(encoding="utf-8") == "seed\nCHARACTERIZATION_EDIT\n"
                if seed.exists()
                else False
            )
            capture = _run_git(("git", "diff", "--", "seed.txt"), effective, deadline)
            _require_git_success(capture, "diff")
            diff = capture.stdout[:WORKTREE_DIFF_LIMIT]
        primary_unchanged = (repository / "seed.txt").read_text(
            encoding="utf-8"
        ) == "seed\n"
        hook_cwds = _evidence_cwds(evidence_directory)
        hook_effective = worktree_path if worktree_path in hook_cwds else None
        state = matches[0].get("state") if len(matches) == 1 else None
        native_terminal = state in _TERMINAL_STATES
        observed_events = sorted(_read_evidence(evidence_directory))
        if (
            state in _SUCCESSFUL_TERMINAL_STATES
            and worktree_path is not None
            and requested_edit_landed
        ):
            probe_status = "observed"
        elif state in _SUCCESSFUL_TERMINAL_STATES:
            probe_status = "not_observed"
        else:
            probe_status = "probe_inconclusive"
        latest = {
            "probe_status": probe_status,
            "separate_worktree_created": worktree_path is not None,
            "worktree_path": worktree_path,
            "git_branch": branch,
            "effective_cwd": hook_effective or worktree_path,
            "effective_cwd_source": "hook"
            if hook_effective is not None
            else "git_worktree"
            if worktree_path is not None
            else "inconclusive",
            "requested_edit_landed": requested_edit_landed,
            "primary_unchanged": primary_unchanged,
            "native_state_before_cleanup": state,
            "native_terminal_observed": native_terminal,
            "observer_events": observed_events,
            "blocked_before_cleanup": state == "blocked",
            "permission_notification_observed": "permission_prompt" in observed_events,
            "diff": diff,
        }
        if native_terminal:
            return latest
        time.sleep(min(0.2, _remaining(deadline)))
    if latest:
        return latest
    raise CharacterizationError(
        "worktree observation produced no valid native snapshot"
    )


def _require_worktree_work_budget(deadline: float, operation: str) -> None:
    if _remaining(deadline) <= 0:
        raise CharacterizationError(f"worktree deadline exhausted before {operation}")


def _worktree_artifact(
    *,
    phase: str,
    probe: _WorktreeProbe,
    before_agents: Mapping[str, str],
    repository: Path,
    observation: Mapping[str, Any],
    cleanup_confirmed: bool,
    cleanup_attempted: bool,
    diagnostics_preserved: bool,
    unrelated_unchanged: bool | None,
    stop_preserved_artifact: bool | None,
    stop_preserved_worktree: bool | None,
    primary_unchanged_after_stop: bool | None,
    error: Exception | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_phase": phase,
        "context": "unattended_bg_disposable_worktree",
        "fully_characterized": False,
        "estimated_test_owned_session_count": 1,
        "actual_test_owned_session_count": int(
            probe.display_id is not None and probe.session_id is not None
        ),
        "launch_token_reconciled": probe.display_id is not None
        and probe.session_id is not None
        and probe.session_id not in before_agents,
        "launch_invoked_from_origin_cwd": True,
        "origin_cwd": str(repository),
        "cleanup": {
            "attempted": cleanup_attempted,
            "confirmed": cleanup_confirmed,
        },
        "diagnostics_preserved": diagnostics_preserved,
        "probe_error": type(error).__name__ if error is not None else None,
        "unrelated_agents": {
            "pre_existing_count": len(before_agents),
            "unchanged": unrelated_unchanged,
        },
        "stop_preserved_artifact": stop_preserved_artifact,
        "stop_preserved_worktree": stop_preserved_worktree,
        "worktree": {
            **observation,
            "primary_unchanged_after_stop": primary_unchanged_after_stop,
        },
    }


def collect_disposable_worktree_edit_characterization(
    capability: object, destination: Path
) -> dict[str, Any]:
    """Run one exact-owned edit session and retain only sanitized characterization."""
    binary = _require_exact_observer_capability(capability)
    deadline = time.monotonic() + WORKTREE_SUITE_TIMEOUT_SECONDS
    work_deadline = deadline - WORKTREE_CLEANUP_SECONDS
    probe = _mint_worktree_probe()
    with tempfile.TemporaryDirectory(prefix="claude-worktree-") as temporary_name:
        root, repository = Path(temporary_name), Path(temporary_name) / "repository"
        repository.mkdir()
        _require_worktree_work_budget(work_deadline, "repository initialization")
        _initialize_seed_repository(repository, work_deadline)
        _require_worktree_work_budget(work_deadline, "pre-probe agent snapshot")
        before_agents_capture = _run_owned_worktree_command(
            binary,
            probe,
            (str(binary), "agents", "--json", "--all"),
            repository,
            min(WORKTREE_TIMEOUT_SECONDS, _remaining(work_deadline)),
        )
        before_agents = _snapshot_agents(before_agents_capture)
        if before_agents is None:
            raise CharacterizationError("could not capture pre-probe agents listing")
        _require_worktree_work_budget(work_deadline, "pre-probe worktree snapshot")
        before_worktrees = _worktree_snapshot(repository, work_deadline)
        plugin = root / "plugin"
        evidence = root / "evidence"
        _create_observer_plugin(plugin, evidence)
        _require_worktree_work_budget(work_deadline, "background launch")
        launch = _run_owned_worktree_command(
            binary,
            probe,
            _worktree_launch_argv(binary, probe, plugin),
            repository,
            min(WORKTREE_TIMEOUT_SECONDS, _remaining(work_deadline)),
        )
        if launch.returncode != 0 or launch.timed_out:
            raise CharacterizationError("worktree background launch failed")
        token = _launch_display_id(
            launch.stdout, _OwnedProbe(probe.display_name, "worktree")
        )
        if token is None:
            diagnostics = _preserve_worktree_diagnostics(root)
            raise CharacterizationError(
                "worktree launch omitted exact cleanup token; diagnostics preserved at "
                f"{diagnostics}"
            )
        probe = dataclasses.replace(probe, display_id=token)
        observation: dict[str, Any] = {}
        cleanup_confirmed = False
        error: Exception | None = None
        try:
            discovered = _discover_worktree_identity(
                binary,
                probe,
                repository,
                before_agents,
                work_deadline,
            )
            if discovered is None:
                raise CharacterizationError("worktree probe failed identity read-back")
            probe = discovered
            observation = _observe_worktree_probe(
                binary,
                probe,
                repository,
                before_worktrees,
                evidence,
                work_deadline,
            )
        except Exception as caught:
            error = caught
        pre_cleanup_result = _worktree_artifact(
            phase="pre_cleanup",
            probe=probe,
            before_agents=before_agents,
            repository=repository,
            observation=observation,
            cleanup_confirmed=False,
            cleanup_attempted=False,
            diagnostics_preserved=False,
            unrelated_unchanged=None,
            stop_preserved_artifact=None,
            stop_preserved_worktree=None,
            primary_unchanged_after_stop=None,
            error=error,
        )
        archive_error: Exception | None = None
        try:
            atomic_write_contract(destination, pre_cleanup_result)
        except Exception as caught:
            archive_error = caught
        finally:
            cleanup_confirmed = _worktree_cleanup(
                binary,
                probe,
                repository,
                min(deadline, time.monotonic() + WORKTREE_CLEANUP_SECONDS),
            )
        if error is None and archive_error is not None:
            error = archive_error
        stop_preserved_artifact: bool | None = None
        stop_preserved_worktree: bool | None = None
        primary_unchanged_after_stop: bool | None = None
        if cleanup_confirmed:
            try:
                post_stop_worktrees = _worktree_paths(
                    _worktree_snapshot(repository, deadline)
                )
                created_path = observation.get("worktree_path")
                if (
                    isinstance(created_path, str)
                    and created_path in post_stop_worktrees
                ):
                    stop_preserved_worktree = True
                    seed = Path(created_path) / "seed.txt"
                    stop_preserved_artifact = (
                        seed.exists()
                        and seed.read_text(encoding="utf-8")
                        == "seed\nCHARACTERIZATION_EDIT\n"
                    )
                else:
                    stop_preserved_worktree = False
                    stop_preserved_artifact = False
                primary_unchanged_after_stop = (repository / "seed.txt").read_text(
                    encoding="utf-8"
                ) == "seed\n"
            except Exception as caught:
                if error is None:
                    error = caught
        after_agents = _snapshot_agents(
            _run_owned_worktree_command(
                binary,
                probe,
                (str(binary), "agents", "--json", "--all"),
                repository,
                min(WORKTREE_TIMEOUT_SECONDS, _remaining(deadline)),
            )
        )
        unrelated_unchanged = (
            after_agents is not None
            and {
                identity: state
                for identity, state in after_agents.items()
                if identity in before_agents
            }
            == before_agents
        )
        diagnostics_preserved = False
        diagnostics_path: Path | None = None
        if not cleanup_confirmed:
            diagnostics_path = _preserve_worktree_diagnostics(root)
            diagnostics_preserved = True
        result = _worktree_artifact(
            phase="post_cleanup",
            probe=probe,
            before_agents=before_agents,
            repository=repository,
            observation=observation,
            cleanup_confirmed=cleanup_confirmed,
            cleanup_attempted=True,
            diagnostics_preserved=diagnostics_preserved,
            unrelated_unchanged=unrelated_unchanged,
            stop_preserved_artifact=stop_preserved_artifact,
            stop_preserved_worktree=stop_preserved_worktree,
            primary_unchanged_after_stop=primary_unchanged_after_stop,
            error=error,
        )
        atomic_write_contract(destination, result)
        if error is not None:
            if isinstance(error, CharacterizationError):
                raise error
            raise CharacterizationError(
                f"worktree observation failed: {type(error).__name__}"
            ) from error
        if not cleanup_confirmed:
            raise CharacterizationError(
                "worktree cleanup could not be confirmed; diagnostics preserved at "
                f"{diagnostics_path}"
            )
        if not unrelated_unchanged:
            raise CharacterizationError(
                "pre-existing unrelated agent states changed during worktree probe"
            )
        return sanitize_contract(result)


# Task 0C-B keeps lifecycle mutation private to this collector.
LIFECYCLE_TIMEOUT_SECONDS = 10.0
LIFECYCLE_SUITE_TIMEOUT_SECONDS = 70.0
LIFECYCLE_CLEANUP_SECONDS = 10.0
LIFECYCLE_POLL_SECONDS = 0.2


@dataclasses.dataclass(frozen=True)
class _LifecycleProbe:
    """The only lifecycle target this module can ever mutate."""

    display_name: str
    display_id: str | None = None
    session_id: str | None = None


def _mint_lifecycle_probe() -> _LifecycleProbe:
    return _LifecycleProbe(f"lifecycle-{uuid.uuid4()}")


def _lifecycle_prompt() -> str:
    return (
        "Read only. Do not edit files, run commands, create files, or request "
        "permissions. Reply with exactly: lifecycle characterization ready."
    )


def _lifecycle_launch_argv(binary: Path, probe: _LifecycleProbe) -> tuple[str, ...]:
    return (
        str(binary),
        "--bg",
        "--name",
        probe.display_name,
        "--permission-mode",
        "manual",
        _lifecycle_prompt(),
    )


def _lifecycle_owned(probe: _LifecycleProbe) -> _OwnedProbe:
    return _OwnedProbe(
        probe.display_name, "lifecycle", probe.session_id, probe.display_id
    )


def _run_owned_lifecycle_command(
    binary: Path,
    probe: _LifecycleProbe,
    argv: tuple[str, ...],
    cwd: Path,
    timeout: float,
) -> CommandCapture:
    """Run an exact, collector-minted lifecycle command and nothing else."""
    if not argv or argv[0] != str(binary):
        raise CharacterizationError("lifecycle command must use the validated binary")
    suffix = argv[1:]
    identifiers = {value for value in (probe.display_id, probe.session_id) if value}
    allowed = suffix in {
        ("--version",),
        ("agents", "--json", "--all"),
        ("daemon", "status"),
        _lifecycle_launch_argv(binary, probe)[1:],
    }
    if len(suffix) == 2 and suffix[0] in {"logs", "stop", "respawn"}:
        allowed = suffix[1] in identifiers
    if not allowed:
        raise CharacterizationError("lifecycle command shape is not probe-owned")
    return _run_command_within_total_budget(
        argv, total_timeout_seconds=timeout, cwd=cwd
    )


def _lifecycle_listing(
    binary: Path, probe: _LifecycleProbe, cwd: Path, timeout: float
) -> CommandCapture:
    return _run_owned_lifecycle_command(
        binary, probe, (str(binary), "agents", "--json", "--all"), cwd, timeout
    )


def _classify_daemon(capture: CommandCapture) -> str:
    """Classify only explicit daemon text; absence of evidence remains unknown."""
    if capture.timed_out or capture.error:
        return "unparseable"
    text = f"{capture.stdout}\n{capture.stderr}".lower()
    if re.search(r"\b(not running|stopped|not started)\b", text):
        return "not_running"
    if capture.returncode == 0 and re.search(r"\b(running|started|active)\b", text):
        return "running"
    return "unparseable"


def _classify_lifecycle_roster(rows: list[Mapping[str, Any]]) -> str:
    """Classify an exact-owned roster match without normalizing ambiguity."""
    if len(rows) == 0:
        return "stale"
    if len(rows) != 1:
        return "ambiguous"
    state = rows[0].get("state")
    if not isinstance(state, str):
        return "ambiguous"
    return "recoverable" if state in _TERMINAL_STATES else "active"


def _lifecycle_roster_snapshot(
    binary: Path, probe: _LifecycleProbe, cwd: Path, deadline: float
) -> str:
    if _remaining(deadline) <= 0:
        return "ambiguous"
    capture = _lifecycle_listing(
        binary, probe, cwd, min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline))
    )
    if capture.returncode != 0 or capture.timed_out or capture.error:
        return "ambiguous"
    try:
        payload = json.loads(capture.stdout)
    except json.JSONDecodeError:
        return "ambiguous"
    if not isinstance(payload, list) or not all(
        isinstance(row, Mapping) for row in payload
    ):
        return "ambiguous"
    rows = _owned_rows(payload, _lifecycle_owned(probe))
    return _classify_lifecycle_roster(rows)


def _archive_lifecycle_checkpoint(
    destination: Path,
    *,
    pending_mutation: str,
    version: str,
    lifecycle: Mapping[str, str],
    acceptance: Mapping[str, Mapping[str, str]],
    daemon: Mapping[str, str],
    roster: Mapping[str, str],
) -> None:
    """Persist bounded sanitized evidence before an exploratory mutation."""
    atomic_write_contract(
        destination,
        {
            "schema_version": 1,
            "artifact_phase": "pre_mutation",
            "pending_mutation": pending_mutation,
            "context": "unattended_bg",
            "attended": False,
            "fully_characterized": False,
            "version": version,
            "lifecycle": dict(lifecycle),
            "identifier_acceptance": {
                command: dict(spellings) for command, spellings in acceptance.items()
            },
            "daemon": dict(daemon),
            "roster": dict(roster),
            "cleanup": {"attempted": False, "confirmed": False},
            "manual_gates_deferred": [
                "attach",
                "permission_denial",
                "sleep",
                "shutdown_restart",
            ],
        },
    )


def _daemon_snapshot(
    binary: Path, probe: _LifecycleProbe, cwd: Path, deadline: float
) -> str:
    if _remaining(deadline) <= 0:
        return "unparseable"
    return _classify_daemon(
        _run_owned_lifecycle_command(
            binary,
            probe,
            (str(binary), "daemon", "status"),
            cwd,
            min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline)),
        )
    )


def _reconcile_lifecycle_identity(
    binary: Path, probe: _LifecycleProbe, cwd: Path, excluded: set[str], deadline: float
) -> _LifecycleProbe | None:
    """Bind exact name + launch token to one canonical UUID, never a prefix guess."""
    while _remaining(deadline) > 0:
        capture = _lifecycle_listing(
            binary, probe, cwd, min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline))
        )
        if capture.returncode == 0 and not capture.timed_out:
            try:
                matches = _owned_rows(
                    json.loads(capture.stdout), _lifecycle_owned(probe)
                )
            except json.JSONDecodeError:
                matches = []
            if len(matches) > 1:
                raise CharacterizationError("ambiguous lifecycle native identity")
            if len(matches) == 1:
                row = matches[0]
                full, short = row.get("sessionId"), row.get("id")
                try:
                    canonical = isinstance(full, str) and str(uuid.UUID(full)) == full
                except ValueError:
                    canonical = False
                if (
                    canonical
                    and isinstance(full, str)
                    and isinstance(short, str)
                    and short == probe.display_id
                    and full[:8] == short
                    and full not in excluded
                ):
                    return dataclasses.replace(probe, session_id=full)
        time.sleep(min(LIFECYCLE_POLL_SECONDS, _remaining(deadline)))
    return None


def _recover_lifecycle_identity_by_name(
    binary: Path,
    probe: _LifecycleProbe,
    cwd: Path,
    excluded: set[str],
    deadline: float,
) -> _LifecycleProbe | None:
    """Recover one canonical exact-name identity after tokenless respawn output."""
    while _remaining(deadline) > 0:
        capture = _lifecycle_listing(
            binary, probe, cwd, min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline))
        )
        if capture.returncode == 0 and not capture.timed_out and not capture.error:
            try:
                payload = json.loads(capture.stdout)
            except json.JSONDecodeError as error:
                raise CharacterizationError(
                    "malformed lifecycle roster after tokenless respawn"
                ) from error
            if not isinstance(payload, list) or not all(
                isinstance(row, Mapping) for row in payload
            ):
                raise CharacterizationError(
                    "malformed lifecycle roster after tokenless respawn"
                )
            matches = [row for row in payload if row.get("name") == probe.display_name]
            if len(matches) > 1:
                raise CharacterizationError(
                    "ambiguous lifecycle identity after tokenless respawn"
                )
            if len(matches) == 1:
                full, short = matches[0].get("sessionId"), matches[0].get("id")
                try:
                    canonical = isinstance(full, str) and str(uuid.UUID(full)) == full
                except ValueError:
                    canonical = False
                if not (
                    canonical
                    and isinstance(full, str)
                    and isinstance(short, str)
                    and re.fullmatch(r"[0-9a-f]{8}", short)
                    and full[:8] == short
                    and full not in excluded
                ):
                    raise CharacterizationError(
                        "malformed lifecycle identity after tokenless respawn"
                    )
                return dataclasses.replace(probe, display_id=short, session_id=full)
        time.sleep(min(LIFECYCLE_POLL_SECONDS, _remaining(deadline)))
    return None


def _transition_to_terminal(
    binary: Path, probe: _LifecycleProbe, cwd: Path, deadline: float
) -> str:
    valid_nonterminal_seen = False
    while _remaining(deadline) > 0:
        capture = _lifecycle_listing(
            binary, probe, cwd, min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline))
        )
        if capture.returncode == 0 and not capture.timed_out:
            try:
                matches = _owned_rows(
                    json.loads(capture.stdout), _lifecycle_owned(probe)
                )
            except json.JSONDecodeError:
                return "probe_inconclusive"
            if len(matches) != 1:
                return "probe_inconclusive"
            if matches[0].get("state") in _TERMINAL_STATES:
                return "observed"
            valid_nonterminal_seen = True
        time.sleep(min(LIFECYCLE_POLL_SECONDS, _remaining(deadline)))
    return "not_observed" if valid_nonterminal_seen else "probe_inconclusive"


def _lifecycle_operation(
    binary: Path,
    probe: _LifecycleProbe,
    command: str,
    identifier: str,
    cwd: Path,
    deadline: float,
) -> str:
    if _remaining(deadline) <= 0:
        return "probe_inconclusive"
    capture = _run_owned_lifecycle_command(
        binary,
        probe,
        (str(binary), command, identifier),
        cwd,
        min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline)),
    )
    if capture.timed_out or capture.error:
        return "probe_inconclusive"
    return "accepted" if capture.returncode == 0 else "rejected"


def _lifecycle_cleanup(
    binary: Path, probe: _LifecycleProbe, cwd: Path, deadline: float
) -> bool:
    if (
        probe.display_id is None
        or _lifecycle_operation(binary, probe, "stop", probe.display_id, cwd, deadline)
        != "accepted"
    ):
        return False
    while _remaining(deadline) > 0:
        capture = _lifecycle_listing(
            binary, probe, cwd, min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline))
        )
        if capture.returncode == 0 and not capture.timed_out:
            try:
                rows = json.loads(capture.stdout)
            except json.JSONDecodeError:
                return False
            matches = _strict_lifecycle_owned_candidates(rows, probe)
            if matches is None:
                return False
            if not matches or (
                len(matches) == 1 and matches[0].get("state") in _TERMINAL_STATES
            ):
                return True
            if len(matches) > 1:
                return False
        time.sleep(min(LIFECYCLE_POLL_SECONDS, _remaining(deadline)))
    return False


def _strict_lifecycle_owned_candidates(
    rows: object, probe: _LifecycleProbe
) -> list[Mapping[str, Any]] | None:
    """Return exact owned rows, distinguishing absence from malformed ownership."""
    if not isinstance(rows, list) or probe.display_id is None:
        return None
    matches: list[Mapping[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        full = item.get("sessionId")
        candidate = (
            item.get("name") == probe.display_name
            or item.get("id") == probe.display_id
            or (probe.session_id is not None and full == probe.session_id)
        )
        if not candidate:
            continue
        try:
            canonical = isinstance(full, str) and str(uuid.UUID(full)) == full
        except ValueError:
            canonical = False
        exact = (
            canonical
            and item.get("name") == probe.display_name
            and item.get("id") == probe.display_id
            and isinstance(full, str)
            and full[:8] == probe.display_id
            and (probe.session_id is None or full == probe.session_id)
        )
        if not exact:
            return None
        matches.append(item)
    return matches


def collect_unattended_lifecycle_characterization(
    capability: object, destination: Path
) -> dict[str, Any]:
    """Safely characterize stop/respawn and target spelling for one owned session."""
    binary = _require_exact_observer_capability(capability)
    deadline = time.monotonic() + LIFECYCLE_SUITE_TIMEOUT_SECONDS
    work_deadline = deadline - LIFECYCLE_CLEANUP_SECONDS
    probe = _mint_lifecycle_probe()
    with tempfile.TemporaryDirectory(prefix="claude-lifecycle-") as temporary_name:
        repository = Path(temporary_name) / "repository"
        repository.mkdir()
        _initialize_seed_repository(repository, work_deadline)
        before = _snapshot_agents(
            _lifecycle_listing(
                binary,
                probe,
                repository,
                min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(work_deadline)),
            )
        )
        if before is None:
            raise CharacterizationError(
                "could not capture pre-lifecycle agents listing"
            )
        version_capture = _run_owned_lifecycle_command(
            binary,
            probe,
            (str(binary), "--version"),
            repository,
            min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(work_deadline)),
        )
        version = (version_capture.stdout or version_capture.stderr).strip()
        if version_capture.returncode != 0 or version_capture.timed_out:
            version = "unparseable"
        daemon = {
            "before_launch": _daemon_snapshot(binary, probe, repository, work_deadline)
        }
        roster = {
            "before_launch": _lifecycle_roster_snapshot(
                binary, probe, repository, work_deadline
            )
        }
        acceptance = {
            command: {"short": "probe_inconclusive", "full_uuid": "probe_inconclusive"}
            for command in ("logs", "stop", "respawn")
        }
        lifecycle = {
            "stop_transition": "probe_inconclusive",
            "respawn_identity": "probe_inconclusive",
        }

        def archive_before(command: str, spelling: str) -> None:
            _archive_lifecycle_checkpoint(
                destination,
                pending_mutation=f"{command}:{spelling}",
                version=version,
                lifecycle=lifecycle,
                acceptance=acceptance,
                daemon=daemon,
                roster=roster,
            )

        launched = identity_reconciled = cleanup_confirmed = False
        cleanup_probes: dict[str, _LifecycleProbe] = {}
        error: Exception | None = None
        try:
            archive_before("launch", "bg")
            launch = _run_owned_lifecycle_command(
                binary,
                probe,
                _lifecycle_launch_argv(binary, probe),
                repository,
                min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(work_deadline)),
            )
            if launch.returncode != 0 or launch.timed_out:
                raise CharacterizationError("lifecycle background launch failed")
            token = _launch_display_id(launch.stdout, _lifecycle_owned(probe))
            if token is None:
                raise CharacterizationError(
                    "lifecycle launch omitted exact cleanup token"
                )
            launched, probe = True, dataclasses.replace(probe, display_id=token)
            cleanup_probes[token] = probe
            reconciled = _reconcile_lifecycle_identity(
                binary, probe, repository, set(before), work_deadline
            )
            if reconciled is None:
                raise CharacterizationError("lifecycle identity read-back failed")
            probe = reconciled
            cleanup_probes[token] = probe
            identity_reconciled = True
            daemon["after_launch"] = _daemon_snapshot(
                binary, probe, repository, work_deadline
            )
            roster["after_launch"] = _lifecycle_roster_snapshot(
                binary, probe, repository, work_deadline
            )
            assert probe.display_id is not None and probe.session_id is not None
            for spelling, identifier in (
                ("short", probe.display_id),
                ("full_uuid", probe.session_id),
            ):
                acceptance["logs"][spelling] = _lifecycle_operation(
                    binary, probe, "logs", identifier, repository, work_deadline
                )
            archive_before("stop", "short")
            acceptance["stop"]["short"] = _lifecycle_operation(
                binary, probe, "stop", probe.display_id, repository, work_deadline
            )
            lifecycle["stop_transition"] = (
                _transition_to_terminal(binary, probe, repository, work_deadline)
                if acceptance["stop"]["short"] == "accepted"
                else "probe_inconclusive"
                if acceptance["stop"]["short"] == "probe_inconclusive"
                else "not_observed"
            )
            roster["after_stop"] = _lifecycle_roster_snapshot(
                binary, probe, repository, work_deadline
            )
            archive_before("respawn", "short")
            respawn = _run_owned_lifecycle_command(
                binary,
                probe,
                (str(binary), "respawn", probe.display_id),
                repository,
                min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(work_deadline)),
            )
            acceptance["respawn"]["short"] = (
                "probe_inconclusive"
                if respawn.timed_out or respawn.error
                else "accepted"
                if respawn.returncode == 0
                else "rejected"
            )
            token = _launch_display_id(respawn.stdout, _lifecycle_owned(probe))
            if acceptance["respawn"]["short"] == "accepted":
                previous_session_id = probe.session_id
                candidate = (
                    dataclasses.replace(probe, display_id=token, session_id=None)
                    if token is not None
                    else _recover_lifecycle_identity_by_name(
                        binary, probe, repository, set(before), work_deadline
                    )
                )
                if candidate is None:
                    acceptance["respawn"]["short"] = "probe_inconclusive"
                    raise CharacterizationError(
                        "tokenless respawn identity recovery failed"
                    )
                probe = candidate
                assert probe.display_id is not None
                cleanup_probes[probe.display_id] = probe
                respawned = _reconcile_lifecycle_identity(
                    binary, candidate, repository, set(), work_deadline
                )
                if respawned is not None:
                    lifecycle["respawn_identity"] = (
                        "preserved"
                        if respawned.session_id == previous_session_id
                        else "replaced"
                    )
                    probe = respawned
                else:
                    acceptance["respawn"]["short"] = "probe_inconclusive"
                    raise CharacterizationError(
                        "respawned lifecycle identity read-back failed"
                    )
            daemon["after_respawn"] = _daemon_snapshot(
                binary, probe, repository, work_deadline
            )
            roster["after_respawn"] = _lifecycle_roster_snapshot(
                binary, probe, repository, work_deadline
            )
            assert probe.display_id is not None and probe.session_id is not None
            archive_before("stop", "full_uuid")
            acceptance["stop"]["full_uuid"] = _lifecycle_operation(
                binary, probe, "stop", probe.session_id, repository, work_deadline
            )
            archive_before("respawn", "full_uuid")
            respawn = _run_owned_lifecycle_command(
                binary,
                probe,
                (str(binary), "respawn", probe.session_id),
                repository,
                min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(work_deadline)),
            )
            acceptance["respawn"]["full_uuid"] = (
                "probe_inconclusive"
                if respawn.timed_out or respawn.error
                else "accepted"
                if respawn.returncode == 0
                else "rejected"
            )
            token = _launch_display_id(respawn.stdout, _lifecycle_owned(probe))
            if acceptance["respawn"]["full_uuid"] == "accepted":
                candidate = (
                    dataclasses.replace(probe, display_id=token, session_id=None)
                    if token is not None
                    else _recover_lifecycle_identity_by_name(
                        binary, probe, repository, set(before), work_deadline
                    )
                )
                if candidate is None:
                    acceptance["respawn"]["full_uuid"] = "probe_inconclusive"
                    raise CharacterizationError(
                        "tokenless respawn identity recovery failed"
                    )
                probe = candidate
                assert probe.display_id is not None
                cleanup_probes[probe.display_id] = probe
                respawned = _reconcile_lifecycle_identity(
                    binary, candidate, repository, set(), work_deadline
                )
                if respawned is not None:
                    probe = respawned
                else:
                    acceptance["respawn"]["full_uuid"] = "probe_inconclusive"
                    raise CharacterizationError(
                        "respawned lifecycle identity read-back failed"
                    )
        except Exception as caught:
            error = caught
        pre_cleanup = {
            "schema_version": 1,
            "artifact_phase": "pre_cleanup",
            "context": "unattended_bg",
            "attended": False,
            "fully_characterized": False,
            "live_evidence": True,
            "estimated_test_owned_session_count": 1,
            "actual_test_owned_session_count": 1 if identity_reconciled else 0,
            "launch_token_reconciled": identity_reconciled,
            "probe_status": "probe_inconclusive",
            "version": version,
            "lifecycle": lifecycle,
            "identifier_acceptance": acceptance,
            "daemon": daemon,
            "roster": roster,
            "cleanup": {"attempted": launched, "confirmed": False},
            "manual_gates_deferred": [
                "attach",
                "permission_denial",
                "sleep",
                "shutdown_restart",
            ],
        }
        archive_error: Exception | None = None
        try:
            atomic_write_contract(destination, pre_cleanup)
        except Exception as caught:
            archive_error = caught
        finally:
            if launched:
                cleanup_deadline = min(
                    deadline, time.monotonic() + LIFECYCLE_CLEANUP_SECONDS
                )
                cleanup_confirmed = _lifecycle_cleanup(
                    binary, probe, repository, cleanup_deadline
                )
                for cleanup_probe in cleanup_probes.values():
                    if cleanup_probe.display_id == probe.display_id:
                        continue
                    if _remaining(cleanup_deadline) <= 0:
                        break
                    assert cleanup_probe.display_id is not None
                    _run_owned_lifecycle_command(
                        binary,
                        cleanup_probe,
                        (str(binary), "stop", cleanup_probe.display_id),
                        repository,
                        min(
                            LIFECYCLE_TIMEOUT_SECONDS,
                            _remaining(cleanup_deadline),
                        ),
                    )
            daemon["after_final_stop"] = _daemon_snapshot(
                binary, probe, repository, deadline
            )
            roster["after_final_stop"] = _lifecycle_roster_snapshot(
                binary, probe, repository, deadline
            )
        after = _snapshot_agents(
            _lifecycle_listing(
                binary,
                probe,
                repository,
                min(LIFECYCLE_TIMEOUT_SECONDS, _remaining(deadline)),
            )
        )
        unrelated_unchanged = (
            after is not None and {key: after.get(key) for key in before} == before
        )
        result = {
            **pre_cleanup,
            "artifact_phase": "post_cleanup",
            "daemon": daemon,
            "roster": roster,
            "cleanup": {"attempted": launched, "confirmed": cleanup_confirmed},
            "unrelated_agents": {
                "pre_existing_count": len(before),
                "unchanged": unrelated_unchanged,
            },
            "probe_error": type(error).__name__
            if error
            else type(archive_error).__name__
            if archive_error
            else None,
        }
        result["probe_status"] = (
            "observed"
            if error is None
            and cleanup_confirmed
            and lifecycle["stop_transition"] == "observed"
            and lifecycle["respawn_identity"] in {"preserved", "replaced"}
            else "probe_inconclusive"
        )
        atomic_write_contract(destination, result)
        if error is not None:
            raise error
        if archive_error is not None:
            raise CharacterizationError(
                "lifecycle artifact archival failed"
            ) from archive_error
        if not cleanup_confirmed:
            raise CharacterizationError("lifecycle cleanup could not be confirmed")
        if not unrelated_unchanged:
            raise CharacterizationError(
                "pre-existing unrelated agent states changed during lifecycle probe"
            )
        return sanitize_contract(result)
