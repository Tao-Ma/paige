"""Fake port implementations for tests.

These are deliberately minimal — they implement just enough of each
port's surface to support unit testing application services. Real
behavior (atomicity, concurrency, retry) lives in the production
adapters; fakes prioritize observability + determinism.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.card import ActionEvent
from ..domain.conversation import Anchor, Conversation
from ..domain.host import LOCAL_HOST_ID
from ..domain.inbound import Attachment, Inbound
from ..domain.outbound import Outbound, TypingContent
from ..domain.pane import Pane
from ..domain.transcript import TranscriptEvent
from ..ports.channel import ActionHandler, CommandHandler, InboundHandler
from ..ports.watcher import EventHandler


class FakeStorage:
    """In-memory `Storage` for tests.

    Records every save / delete so tests can assert on call order.
    `peek()` returns the raw internal dict (not a copy) for
    diagnostic asserts; use `load()` for the real API.
    """

    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}
        self.saves: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[str] = []

    async def load(self, key: str) -> dict[str, Any] | None:
        v = self._d.get(key)
        return dict(v) if v is not None else None

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self._d[key] = dict(value)
        self.saves.append((key, dict(value)))

    async def delete(self, key: str) -> None:
        self._d.pop(key, None)
        self.deletes.append(key)

    def peek(self) -> dict[str, dict[str, Any]]:
        """Diagnostic accessor — internal state. Don't mutate."""
        return self._d


@dataclass
class _SendKeysCall:
    """Diagnostic record of one `FakeMultiplexer.send_keys` call."""

    pane_id: str
    text: str
    enter: bool
    literal: bool


class FakeMultiplexer:
    """In-memory `Multiplexer` for tests.

    Pane content is whatever you `set_capture()` it to be (so
    pane-scrape tests can prime the fake with a snapshot of a TUI
    state). All operations are recorded — `send_keys_calls` /
    `killed` / `created` / `renamed` — so tests assert on call
    sequences.
    """

    def __init__(self) -> None:
        self._panes: dict[str, Pane] = {}
        self._captures: dict[str, str] = {}
        self._foreground_pids: dict[str, int] = {}
        self._next_id = 0
        self.send_keys_calls: list[_SendKeysCall] = []
        self.killed: list[str] = []
        self.created: list[Pane] = []
        self.renamed: list[tuple[str, str]] = []

    # ── seed helpers (test-only, not in the Protocol) ─────────────

    def add_pane(
        self,
        pane_id: str,
        pane_name: str,
        cwd: Path,
        multiplexer_session: str = "",
    ) -> Pane:
        """Insert a pre-existing pane (e.g. simulating something
        started outside paige's control)."""
        pane = Pane(
            pane_id=pane_id,
            pane_name=pane_name,
            cwd=cwd,
            multiplexer_session=multiplexer_session,
        )
        self._panes[pane_id] = pane
        return pane

    def set_capture(self, pane_id: str, content: str) -> None:
        self._captures[pane_id] = content

    def set_foreground_pid(self, pane_id: str, pid: int) -> None:
        self._foreground_pids[pane_id] = pid

    # ── Multiplexer protocol ─────────────────────────────────────
    #
    # `host_id` is part of the Multiplexer Protocol so the
    # MultiplexerRouter can dispatch by host. FakeMultiplexer
    # ignores it — tests are single-host by construction.

    async def list_panes(self, *, host_id: str = LOCAL_HOST_ID) -> list[Pane]:
        del host_id
        return list(self._panes.values())

    async def find_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> Pane | None:
        del host_id
        return self._panes.get(pane_id)

    async def create_pane(
        self,
        name: str,
        cwd: Path,
        command: str = "",
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> Pane:
        del host_id
        pane_id = f"@{self._next_id}"
        self._next_id += 1
        pane = Pane(
            pane_id=pane_id,
            pane_name=name,
            cwd=cwd,
            multiplexer_session="",
        )
        self._panes[pane_id] = pane
        self.created.append(pane)
        # Match TmuxMultiplexer: when command is non-empty, the
        # adapter sends it as keystrokes to the new pane. Recording
        # it here keeps `send_keys_calls`-based tests faithful.
        if command:
            self.send_keys_calls.append(
                _SendKeysCall(pane_id=pane_id, text=command, enter=True, literal=True)
            )
        return pane

    async def kill_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> bool:
        del host_id
        if pane_id not in self._panes:
            return False
        del self._panes[pane_id]
        self._captures.pop(pane_id, None)
        self._foreground_pids.pop(pane_id, None)
        self.killed.append(pane_id)
        return True

    async def rename_pane(
        self, pane_id: str, new_name: str, *, host_id: str = LOCAL_HOST_ID
    ) -> bool:
        del host_id
        old = self._panes.get(pane_id)
        if old is None:
            return False
        self._panes[pane_id] = Pane(
            pane_id=old.pane_id,
            pane_name=new_name,
            cwd=old.cwd,
            multiplexer_session=old.multiplexer_session,
        )
        self.renamed.append((pane_id, new_name))
        return True

    async def send_keys(
        self,
        pane_id: str,
        text: str,
        *,
        enter: bool = True,
        literal: bool = True,
        host_id: str = LOCAL_HOST_ID,
    ) -> bool:
        del host_id
        if pane_id not in self._panes:
            return False
        self.send_keys_calls.append(
            _SendKeysCall(pane_id=pane_id, text=text, enter=enter, literal=literal)
        )
        return True

    async def capture(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        del host_id
        if pane_id not in self._panes:
            return None
        return self._captures.get(pane_id, "")

    async def capture_with_ansi(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        # Fake returns the same capture for both calls; tests that
        # exercise ANSI parsing seed the capture with raw escape
        # sequences via `set_capture(pane_id, text_with_ansi)`.
        return await self.capture(pane_id, host_id=host_id)

    async def get_foreground_pid(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> int | None:
        del host_id
        if pane_id not in self._panes:
            return None
        return self._foreground_pids.get(pane_id)


# Re-export the dataclass so test files can match against it.
SendKeysCall = _SendKeysCall


class FakeWatcher:
    """In-memory `Watcher` for tests.

    No file polling. Tests call `feed(run_id, event)` to synthesize
    a TranscriptEvent for a tracked run; the call awaits all
    subscribed handlers before returning, so assertions on
    downstream effects can run synchronously after `feed`.

    `flush()` returns 0 — events are dispatched eagerly by `feed`,
    nothing is buffered.
    """

    def __init__(self) -> None:
        self._tracked: set[str] = set()
        self._handlers: list[EventHandler] = []
        self.fed: list[tuple[str, TranscriptEvent]] = []
        self.started = False
        self.stopped = False

    # ── Watcher protocol ─────────────────────────────────────────

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def flush(self) -> int:
        return 0

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def track(
        self,
        run_id: str,
        transcript_path: Path,
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> None:
        del host_id  # single-host fake; see Multiplexer fakes.
        del transcript_path  # we don't read files; just remember the run_id.
        self._tracked.add(run_id)

    def untrack(self, run_id: str) -> None:
        self._tracked.discard(run_id)

    # ── seed helpers (test-only, not in the Protocol) ────────────

    def is_tracked(self, run_id: str) -> bool:
        return run_id in self._tracked

    async def feed(self, run_id: str, event: TranscriptEvent) -> None:
        """Synthesize an event for `run_id`. The run must be tracked
        — feeding an unknown run is a programmer error, since real
        watchers can only emit for files they're watching."""
        if run_id not in self._tracked:
            raise KeyError(f"run_id not tracked: {run_id!r}")
        self.fed.append((run_id, event))
        for handler in self._handlers:
            await handler(run_id, event)


class FakeChannel:
    """In-memory `Channel` for tests.

    Records every outbound op (`sent` / `edits` / `deleted` / `acks`
    / `probes` / `downloaded`) so tests assert on the wire shape.
    Inbound events are pushed via `deliver_inbound` /
    `deliver_command` / `deliver_action` and immediately await all
    registered handlers, so test assertions can run synchronously
    after delivery.

    `fail_send_once` / `fail_edit_once` / `fail_delete_once` inject
    a one-shot exception on the matching method.
    """

    def __init__(self) -> None:
        self.sent: list[Outbound] = []
        self.edits: list[tuple[Anchor, Outbound]] = []
        self.deleted: list[Anchor] = []
        self.acks: list[tuple[ActionEvent, str | None]] = []
        self.probes: list[Conversation] = []
        self.downloaded: list[Attachment] = []
        # Synthetic `dispatch_command` calls — (inbound, name, arg).
        # Recorded whether or not a handler was registered.
        self.synthetic_commands: list[tuple[Inbound, str, str]] = []
        # `dead_chats` lets tests pre-mark conversations as deleted
        # so `probe` returns False for them.
        self.dead_chats: set[Conversation] = set()
        self.download_data: bytes = b""
        self.started = False
        self.stopped = False

        self._inbound_handlers: list[InboundHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._action_handlers: list[ActionHandler] = []

        self._next_message_id = 1000
        self._fail_send_once: Exception | None = None
        self._fail_edit_once: Exception | None = None
        self._fail_delete_once: Exception | None = None
        # Edit cross-type fallback simulation — when set, the next
        # edit() returns this Anchor (and clears).
        self._edit_returns_once: Anchor | None = None

    # ── Channel protocol ─────────────────────────────────────────

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, outbound: Outbound) -> Anchor | None:
        self.sent.append(outbound)
        if self._fail_send_once is not None:
            exc = self._fail_send_once
            self._fail_send_once = None
            raise exc
        if isinstance(outbound.content, TypingContent):
            return None
        self._next_message_id += 1
        return Anchor(
            conversation=outbound.conversation,
            message_id=str(self._next_message_id),
        )

    async def edit(self, anchor: Anchor, outbound: Outbound) -> Anchor | None:
        self.edits.append((anchor, outbound))
        if self._fail_edit_once is not None:
            exc = self._fail_edit_once
            self._fail_edit_once = None
            raise exc
        if self._edit_returns_once is not None:
            replacement = self._edit_returns_once
            self._edit_returns_once = None
            return replacement
        return None

    async def delete(self, anchor: Anchor) -> None:
        self.deleted.append(anchor)
        if self._fail_delete_once is not None:
            exc = self._fail_delete_once
            self._fail_delete_once = None
            raise exc

    async def download(self, attachment: Attachment) -> bytes:
        self.downloaded.append(attachment)
        return self.download_data

    async def ack(self, event: ActionEvent, text: str | None = None) -> None:
        self.acks.append((event, text))

    async def probe(self, conversation: Conversation) -> bool:
        self.probes.append(conversation)
        return conversation not in self.dead_chats

    def on_inbound(self, handler: InboundHandler) -> None:
        self._inbound_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_action(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    async def dispatch_command(self, inbound: Inbound, name: str, arg: str) -> bool:
        self.synthetic_commands.append((inbound, name, arg))
        handler = self._command_handlers.get(name)
        if handler is None:
            return False
        await handler(inbound, arg)
        return True

    # ── seed helpers (test-only, not in the Protocol) ────────────

    async def deliver_inbound(self, inbound: Inbound) -> None:
        for handler in list(self._inbound_handlers):
            await handler(inbound)

    async def deliver_command(self, name: str, inbound: Inbound, arg: str = "") -> None:
        handler = self._command_handlers.get(name)
        if handler is None:
            raise KeyError(f"No /{name} handler registered")
        await handler(inbound, arg)

    async def deliver_action(self, event: ActionEvent) -> None:
        for handler in list(self._action_handlers):
            await handler(event)

    def fail_send_once(self, exc: Exception) -> None:
        self._fail_send_once = exc

    def fail_edit_once(self, exc: Exception) -> None:
        self._fail_edit_once = exc

    def fail_delete_once(self, exc: Exception) -> None:
        self._fail_delete_once = exc

    def edit_returns_once(self, anchor: Anchor) -> None:
        """Simulate cross-type fallback (delete + resend → new anchor)."""
        self._edit_returns_once = anchor
