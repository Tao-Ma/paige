"""FakeWatcher — observable in-memory transcript watcher for tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.domain.transcript import Block, BlockKind, Role, TranscriptEvent
from paige.ports.watcher import Watcher
from paige.testing.fakes import FakeWatcher


def _ev(text: str = "hi") -> TranscriptEvent:
    return TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text=text),),
    )


def test_satisfies_watcher_protocol() -> None:
    assert isinstance(FakeWatcher(), Watcher)


async def test_start_stop_flags() -> None:
    w = FakeWatcher()
    assert not w.started and not w.stopped
    await w.start()
    assert w.started
    await w.stop()
    assert w.stopped


async def test_track_then_feed_invokes_handlers() -> None:
    w = FakeWatcher()
    received: list[tuple[str, TranscriptEvent]] = []

    async def handler(run_id: str, ev: TranscriptEvent) -> None:
        received.append((run_id, ev))

    w.on_event(handler)
    w.track("run-1", Path("/tmp/run-1.jsonl"))
    ev = _ev("hello")
    await w.feed("run-1", ev)
    assert received == [("run-1", ev)]
    assert w.fed == [("run-1", ev)]


async def test_feed_to_untracked_run_raises() -> None:
    w = FakeWatcher()
    with pytest.raises(KeyError):
        await w.feed("run-x", _ev())


async def test_untrack_then_feed_raises() -> None:
    w = FakeWatcher()
    w.track("run-1", Path("/tmp/r1.jsonl"))
    w.untrack("run-1")
    with pytest.raises(KeyError):
        await w.feed("run-1", _ev())


async def test_multiple_handlers_all_invoked() -> None:
    w = FakeWatcher()
    a: list[TranscriptEvent] = []
    b: list[TranscriptEvent] = []

    async def ha(_rid: str, ev: TranscriptEvent) -> None:
        a.append(ev)

    async def hb(_rid: str, ev: TranscriptEvent) -> None:
        b.append(ev)

    w.on_event(ha)
    w.on_event(hb)
    w.track("r", Path("/tmp/r.jsonl"))
    ev = _ev()
    await w.feed("r", ev)
    assert a == [ev] and b == [ev]


async def test_flush_returns_zero() -> None:
    w = FakeWatcher()
    assert await w.flush() == 0


def test_is_tracked_diagnostic() -> None:
    w = FakeWatcher()
    w.track("a", Path("/tmp/a.jsonl"))
    assert w.is_tracked("a") and not w.is_tracked("b")
