"""WatcherRouter — host_id → Watcher adapter dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.watcher_router import WatcherRouter
from paige.domain.host import LOCAL_HOST_ID
from paige.domain.transcript import Block, BlockKind, Role, TranscriptEvent
from paige.testing.fakes import FakeWatcher


def _make(local_only: bool = False) -> tuple[WatcherRouter, FakeWatcher, FakeWatcher | None]:
    local = FakeWatcher()
    if local_only:
        return WatcherRouter({LOCAL_HOST_ID: local}), local, None
    dev1 = FakeWatcher()
    return WatcherRouter({LOCAL_HOST_ID: local, "dev-1": dev1}), local, dev1


# ── constructor ─────────────────────────────────────────────────


def test_constructor_requires_local_entry() -> None:
    """Without a `local` entry there's nowhere to land
    paige's own transcripts; reject upfront rather than crash on
    the first track call."""
    with pytest.raises(ValueError, match="local"):
        WatcherRouter({"dev-1": FakeWatcher()})


def test_for_host_returns_local_by_default() -> None:
    router, local, _ = _make(local_only=True)
    assert router.for_host(LOCAL_HOST_ID) is local


def test_for_host_unknown_falls_back_to_local() -> None:
    router, local, _ = _make(local_only=True)
    assert router.for_host("never-configured") is local


# ── track / untrack dispatch ────────────────────────────────────


def test_track_default_host_routes_to_local() -> None:
    router, local, dev1 = _make()
    router.track("sid-a", Path("/p/a.jsonl"))
    assert local.is_tracked("sid-a")
    assert dev1 is not None
    assert not dev1.is_tracked("sid-a")


def test_track_explicit_host_routes_to_remote() -> None:
    router, local, dev1 = _make()
    router.track("sid-b", Path("/p/b.jsonl"), host_id="dev-1")
    assert dev1 is not None
    assert dev1.is_tracked("sid-b")
    assert not local.is_tracked("sid-b")


def test_untrack_fans_out_to_every_backend() -> None:
    """We don't keep a router-level run_id → host_id map; untrack
    fan-out keeps that simple. Backends with no record of the
    run handle it as a no-op."""
    router, local, dev1 = _make()
    router.track("sid-z", Path("/p/z.jsonl"), host_id="dev-1")
    assert dev1 is not None
    assert dev1.is_tracked("sid-z")
    router.untrack("sid-z")
    assert not dev1.is_tracked("sid-z")
    # Also a no-op against `local` (run was never tracked there).
    assert not local.is_tracked("sid-z")


# ── lifecycle fan-out ───────────────────────────────────────────


async def test_start_starts_every_backend() -> None:
    router, local, dev1 = _make()
    await router.start()
    assert local.started is True
    assert dev1 is not None and dev1.started is True


async def test_stop_stops_every_backend() -> None:
    router, local, dev1 = _make()
    await router.stop()
    assert local.stopped is True
    assert dev1 is not None and dev1.stopped is True


async def test_flush_sums_event_counts_across_backends() -> None:
    router, local, dev1 = _make()
    router.track("sid-a", Path("/p/a.jsonl"))
    router.track("sid-b", Path("/p/b.jsonl"), host_id="dev-1")
    # FakeWatcher.flush() returns 0 by default — events arrive via
    # `feed`. Just confirm flush returns an int that's the sum.
    n = await router.flush()
    assert n == 0


async def test_on_event_handler_fires_for_every_backend() -> None:
    """The handler should see events from any host, not just local."""
    router, local, dev1 = _make()
    seen: list[tuple[str, str]] = []

    async def handler(run_id: str, event: TranscriptEvent) -> None:
        text = event.blocks[0].text if event.blocks else ""
        seen.append((run_id, text))

    router.on_event(handler)
    router.track("sid-a", Path("/p/a.jsonl"))
    router.track("sid-b", Path("/p/b.jsonl"), host_id="dev-1")

    ev_a = TranscriptEvent(
        role=Role.ASSISTANT, blocks=(Block(kind=BlockKind.TEXT, text="from-local"),)
    )
    ev_b = TranscriptEvent(
        role=Role.ASSISTANT, blocks=(Block(kind=BlockKind.TEXT, text="from-dev1"),)
    )
    await local.feed("sid-a", ev_a)
    assert dev1 is not None
    await dev1.feed("sid-b", ev_b)

    assert seen == [("sid-a", "from-local"), ("sid-b", "from-dev1")]
