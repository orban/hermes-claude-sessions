"""In-process dispatcher from durable Claude hook events to Hermes wake events."""

from __future__ import annotations

import hashlib
import os
import select
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import bridge

CaptureRoute = Callable[..., Mapping[str, Any]]
PublishEvent = Callable[..., Mapping[str, Any]]
ReconcileLifecycle = Callable[[], object]


class EventDispatcher:
    """Own one low-latency socket and reconcile the durable event spool."""

    def __init__(
        self,
        *,
        capture_route: CaptureRoute | None = None,
        publish: PublishEvent | None = None,
        reconcile_lifecycle: ReconcileLifecycle | None = None,
        reconcile_seconds: float = 30.0,
    ) -> None:
        self._capture_route = capture_route
        self._publish = publish
        self._reconcile_lifecycle = reconcile_lifecycle
        self._reconcile_seconds = max(1.0, reconcile_seconds)
        self._socket: socket.socket | None = None
        self._socket_path = ""
        self._thread: threading.Thread | None = None
        self._state_thread: threading.Thread | None = None
        self._lifecycle_thread: threading.Thread | None = None
        self._state_reader: socket.socket | None = None
        self._state_writer: socket.socket | None = None
        self._stop = threading.Event()
        self._lifecycle_wakeup = threading.Event()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._waiters: dict[str, int] = {}
        self._watched_state_paths: set[Path] = set()

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def configure(
        self,
        *,
        capture_route: CaptureRoute,
        publish: PublishEvent,
    ) -> None:
        with self._lock:
            self._capture_route = capture_route
            self._publish = publish

    def configure_lifecycle(self, reconcile: ReconcileLifecycle) -> None:
        with self._lock:
            self._reconcile_lifecycle = reconcile
            if self._thread is not None and self._thread.is_alive():
                self._start_lifecycle_thread_locked()

    def _start_lifecycle_thread_locked(self) -> None:
        if self._reconcile_lifecycle is None:
            return
        if self._lifecycle_thread is not None and self._lifecycle_thread.is_alive():
            return
        self._lifecycle_thread = threading.Thread(
            target=self._run_lifecycle,
            name="claude-session-lifecycle-reconciler",
            daemon=True,
        )
        self._lifecycle_thread.start()

    def _run_lifecycle_once(self) -> None:
        reconcile = self._reconcile_lifecycle
        if reconcile is None:
            return
        try:
            reconcile()
        except Exception:
            return

    def _run_lifecycle(self) -> None:
        while not self._stop.is_set():
            self._lifecycle_wakeup.wait(timeout=self._reconcile_seconds)
            self._lifecycle_wakeup.clear()
            if self._stop.is_set():
                return
            self._run_lifecycle_once()

    def _reconcile_native_state(self) -> None:
        bridge.enqueue_native_terminal_events()
        self._lifecycle_wakeup.set()

    def capture_route(self, *, parent_session_id: str = "") -> dict[str, Any]:
        callback = self._capture_route
        if callback is None:
            return {}
        try:
            route = callback(parent_session_id=parent_session_id)
        except Exception:
            return {}
        return dict(route) if isinstance(route, Mapping) else {}

    def _candidate_socket_path(self) -> Path:
        root = bridge.state_root().parent / "dispatch"
        token = hashlib.sha256(
            f"{bridge.state_root()}:{os.getpid()}".encode()
        ).hexdigest()[:12]
        candidate = root / f"{os.getpid()}-{token}.sock"
        if len(str(candidate).encode("utf-8")) < 96:
            return candidate
        return Path(tempfile.gettempdir()) / f"hermes-claude-{os.getpid()}-{token}.sock"

    def start(self) -> str:
        """Start one supervised daemon thread; return the hook socket path."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._socket_path
            if self._publish is None or not hasattr(socket, "AF_UNIX"):
                return ""
            socket_path = self._candidate_socket_path()
            socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            socket_path.unlink(missing_ok=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            listener.settimeout(self._reconcile_seconds)
            self._socket = listener
            self._socket_path = str(socket_path)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="claude-session-event-dispatcher",
                daemon=True,
            )
            self._thread.start()
            if hasattr(select, "kqueue"):
                state_reader, state_writer = socket.socketpair()
                state_reader.setblocking(False)
                state_writer.setblocking(False)
                self._state_reader = state_reader
                self._state_writer = state_writer
                self._state_thread = threading.Thread(
                    target=self._run_state_watcher,
                    name="claude-session-native-state-watcher",
                    daemon=True,
                )
                self._state_thread.start()
            self._start_lifecycle_thread_locked()
        self._reconcile_native_state()
        self.drain_once()
        return self._socket_path

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._condition.notify_all()
            listener = self._socket
            thread = self._thread
            state_thread = self._state_thread
            lifecycle_thread = self._lifecycle_thread
            state_reader = self._state_reader
            state_writer = self._state_writer
            self._socket = None
            self._thread = None
            self._state_thread = None
            self._lifecycle_thread = None
            self._state_reader = None
            self._state_writer = None
            socket_path = self._socket_path
            self._socket_path = ""
        if socket_path and listener is not None:
            try:
                wake = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    wake.sendto(b"stop", socket_path)
                finally:
                    wake.close()
            except OSError:
                pass
        if state_writer is not None:
            with suppress(OSError):
                state_writer.send(b"stop")
        self._lifecycle_wakeup.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if state_thread is not None and state_thread is not threading.current_thread():
            state_thread.join(timeout=1.0)
        if (
            lifecycle_thread is not None
            and lifecycle_thread is not threading.current_thread()
        ):
            lifecycle_thread.join(timeout=1.0)
        if listener is not None:
            with suppress(OSError):
                listener.close()
        if socket_path:
            Path(socket_path).unlink(missing_ok=True)
        for state_socket in (state_reader, state_writer):
            if state_socket is not None:
                with suppress(OSError):
                    state_socket.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            listener = self._socket
            if listener is None:
                return
            try:
                listener.recv(64)
            except TimeoutError:
                pass
            except OSError:
                if self._stop.is_set():
                    return
            if self._stop.is_set():
                return
            self._reconcile_native_state()
            with self._condition:
                self._condition.notify_all()
            self.drain_once()

    def watch_session(self, session: Path) -> None:
        """Arm native-state delivery after Claude returns the managed short ID."""
        del session
        self._reconcile_native_state()
        writer = self._state_writer
        if writer is not None:
            with suppress(OSError):
                writer.send(b"refresh")

    def _refresh_state_watches(
        self,
        watcher: Any,
        watched: dict[Path, int],
    ) -> None:
        desired = bridge.native_state_watch_directories()
        for path in tuple(watched):
            if path in desired:
                continue
            descriptor = watched.pop(path)
            with suppress(OSError):
                watcher.control(
                    [
                        select.kevent(
                            descriptor,
                            filter=select.KQ_FILTER_VNODE,
                            flags=select.KQ_EV_DELETE,
                        )
                    ],
                    0,
                    0,
                )
            os.close(descriptor)
        for path in sorted(desired - set(watched)):
            descriptor = -1
            try:
                descriptor = os.open(path, os.O_RDONLY)
                watcher.control(
                    [
                        select.kevent(
                            descriptor,
                            filter=select.KQ_FILTER_VNODE,
                            flags=(
                                select.KQ_EV_ADD
                                | select.KQ_EV_ENABLE
                                | select.KQ_EV_CLEAR
                            ),
                            fflags=(
                                select.KQ_NOTE_WRITE
                                | select.KQ_NOTE_EXTEND
                                | select.KQ_NOTE_ATTRIB
                                | select.KQ_NOTE_RENAME
                                | select.KQ_NOTE_DELETE
                            ),
                        )
                    ],
                    0,
                    0,
                )
            except OSError:
                if descriptor >= 0:
                    with suppress(OSError):
                        os.close(descriptor)
                continue
            watched[path] = descriptor
        with self._lock:
            self._watched_state_paths = set(watched)

    def state_watch_count(self) -> int:
        with self._lock:
            return len(self._watched_state_paths)

    def _run_state_watcher(self) -> None:
        reader = self._state_reader
        if reader is None or not hasattr(select, "kqueue"):
            return
        watcher = select.kqueue()
        watched: dict[Path, int] = {}
        try:
            watcher.control(
                [
                    select.kevent(
                        reader.fileno(),
                        filter=select.KQ_FILTER_READ,
                        flags=(
                            select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
                        ),
                    )
                ],
                0,
                0,
            )
            while not self._stop.is_set():
                self._refresh_state_watches(watcher, watched)
                self._reconcile_native_state()
                native_events = watcher.control(
                    None,
                    max(1, len(watched) + 1),
                    self._reconcile_seconds,
                )
                if self._stop.is_set():
                    return
                if any(event.ident == reader.fileno() for event in native_events):
                    with suppress(BlockingIOError, OSError):
                        reader.recv(4096)
                self._reconcile_native_state()
        finally:
            for descriptor in watched.values():
                with suppress(OSError):
                    os.close(descriptor)
            with self._lock:
                self._watched_state_paths.clear()
            watcher.close()

    @staticmethod
    def _session_key(session: Path) -> str:
        return str(session.resolve())

    def waiter_count(self, session: Path) -> int:
        with self._lock:
            return self._waiters.get(self._session_key(session), 0)

    def wait_for_event(
        self,
        session: Path,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Return one event through the tool path without also publishing a wake."""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        key = self._session_key(session)
        timed_out = False
        marker = bridge.register_event_waiter(session)
        with self._condition:
            self._waiters[key] = self._waiters.get(key, 0) + 1
            try:
                while not self._stop.is_set():
                    claim = bridge.claim_next_event(session, consumer="wait")
                    if claim is None:
                        # A dead-lettered (unroutable) event is still this session's own
                        # outcome; the tool path is its only consumer.
                        claim = bridge.claim_next_event(
                            session, consumer="wait", source="undeliverable"
                        )
                    if claim is not None:
                        if bridge.complete_event_claim(claim, disposition="consumed"):
                            return claim.event
                        bridge.release_event_claim(claim)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    self._condition.wait(
                        timeout=min(remaining, self._reconcile_seconds)
                    )
            finally:
                count = self._waiters.get(key, 1) - 1
                if count > 0:
                    self._waiters[key] = count
                else:
                    self._waiters.pop(key, None)
                bridge.unregister_event_waiter(marker)
        if timed_out:
            self.drain_once()
        return None

    @staticmethod
    def _settle_publish_failure(claim: bridge.EventClaim, error: str) -> None:
        """Retry a failed publish a bounded number of times, then dead-letter it."""
        attempts = bridge.note_publish_failure(claim, error)
        if attempts >= bridge.MAX_PUBLISH_ATTEMPTS:
            bridge.dead_letter_event_claim(
                claim,
                reason=f"publish failed {attempts} times; last error: {error}",
            )
            return
        bridge.release_event_claim(claim)

    def drain_once(self) -> int:
        """Transfer pending spool records into Hermes's durable wake ledger."""
        publisher = self._publish
        if publisher is None:
            return 0
        bridge.recover_abandoned_event_claims()
        published = 0
        for session, event in bridge.list_pending_events():
            if self.waiter_count(session) or bridge.has_active_event_waiter(session):
                continue
            event_id = str(event.get("event_id") or "")
            claim = bridge.claim_event(session, event_id, consumer="dispatcher")
            if claim is None:
                continue
            if self.waiter_count(session) or bridge.has_active_event_waiter(session):
                bridge.release_event_claim(claim)
                continue
            event = claim.event
            route = dict(event.get("route") or {})
            if not bridge.route_is_deliverable(route):
                # No return address (CLI / kanban / pre-API launch): the host would
                # reject it on every retry. Dead-letter now; a same-session waiter can
                # still consume it.
                bridge.dead_letter_event_claim(
                    claim, reason="route has no routable session identity"
                )
                continue
            try:
                result = publisher(
                    event_id=event_id,
                    kind=str(event.get("kind") or "event"),
                    message=str(event.get("message") or ""),
                    route=route,
                    producer_id=str(event.get("producer_id") or ""),
                    payload=dict(event.get("payload") or {}),
                )
            except Exception as exc:
                self._settle_publish_failure(claim, f"{type(exc).__name__}: {exc}")
                continue
            if not isinstance(result, Mapping) or not result.get("ok"):
                error = result.get("error") if isinstance(result, Mapping) else None
                self._settle_publish_failure(
                    claim, str(error or "host rejected the event")
                )
                continue
            if bridge.complete_event_claim(claim, disposition="published"):
                published += 1
            else:
                bridge.release_event_claim(claim)
        return published


_dispatcher = EventDispatcher()


def configure_lifecycle(reconcile: ReconcileLifecycle) -> None:
    _dispatcher.configure_lifecycle(reconcile)


def configure_context(context: object) -> bool:
    """Bind optional Hermes background-event APIs without breaking old cores."""
    capture = getattr(context, "capture_background_event_route", None)
    publish = getattr(context, "publish_background_event", None)
    if not callable(capture) or not callable(publish):
        return False
    _dispatcher.configure(capture_route=capture, publish=publish)
    _dispatcher.start()
    on_unload = getattr(context, "on_unload", None)
    if callable(on_unload):
        on_unload(_dispatcher.stop)
    return True


def capture_route(*, parent_session_id: str = "") -> dict[str, Any]:
    return _dispatcher.capture_route(parent_session_id=parent_session_id)


def socket_path() -> str:
    return _dispatcher.socket_path


def watch_session(session: Path) -> None:
    _dispatcher.watch_session(session)


def wait_for_event(native_id: str, *, timeout_seconds: float) -> dict[str, Any] | None:
    session = bridge.find_session(native_id)
    if session is None:
        return None
    return _dispatcher.wait_for_event(session, timeout_seconds=timeout_seconds)
