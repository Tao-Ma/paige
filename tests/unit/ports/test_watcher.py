"""Watcher Protocol — compliance + minimal stub."""

from __future__ import annotations

from pathlib import Path

from paige.domain.transcript import Block, BlockKind, Role, TranscriptEvent
from paige.ports.watcher import EventHandler, Watcher


class _StubWatcher:
    def __init__(self) -> None:
        self._tracked: set[str] = set()
        self._handlers: list[EventHandler] = []
        self.flush_calls = 0

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def flush(self) -> int:
        self.flush_calls += 1
        return 0

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def track(self, run_id: str, transcript_path: Path) -> None:
        self._tracked.add(run_id)

    def untrack(self, run_id: str) -> None:
        self._tracked.discard(run_id)

    async def emit(self, run_id: str, event: TranscriptEvent) -> None:
        for h in self._handlers:
            await h(run_id, event)


def test_stub_satisfies_watcher_protocol() -> None:
    assert isinstance(_StubWatcher(), Watcher)


async def test_subscribed_handlers_receive_events() -> None:
    w = _StubWatcher()
    received: list[tuple[str, TranscriptEvent]] = []

    async def handler(run_id: str, ev: TranscriptEvent) -> None:
        received.append((run_id, ev))

    w.on_event(handler)
    ev = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text="hi"),),
    )
    await w.emit("run-1", ev)
    assert received == [("run-1", ev)]


async def test_track_untrack_idempotent() -> None:
    w = _StubWatcher()
    w.track("run-1", Path("/tmp/r1.jsonl"))
    w.track("run-1", Path("/tmp/r1.jsonl"))  # no-op
    assert w._tracked == {"run-1"}
    w.untrack("run-1")
    w.untrack("run-1")  # no-op
    assert w._tracked == set()


async def test_flush_returns_count() -> None:
    w = _StubWatcher()
    n = await w.flush()
    assert n == 0
    assert w.flush_calls == 1
