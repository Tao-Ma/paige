"""RunDiscovery — periodic pane → /proc → registry sweep."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from paige.application.run_discovery import RunDiscovery
from paige.application.run_registry import RunRegistry
from paige.testing.fakes import FakeMultiplexer, FakeStorage, FakeWatcher


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    watcher = FakeWatcher()

    discoveries: dict[int, tuple[str, Path, Path]] = {}

    def fake_discover(
        pid: int, _exclude: frozenset[str] = frozenset()
    ) -> tuple[str, Path, Path] | None:
        return discoveries.get(pid)

    discovery = RunDiscovery(
        multiplexer=mux,
        registry=registry,
        watcher=watcher,
        poll_interval=0.05,
        discover=fake_discover,
    )

    class Harness:
        pass

    h = Harness()
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.watcher = watcher  # type: ignore[attr-defined]
    h.discoveries = discoveries  # type: ignore[attr-defined]
    h.discovery = discovery  # type: ignore[attr-defined]
    yield h
    await discovery.stop()


# ── tick basic behavior ──────────────────────────────────────────


async def test_no_panes_means_no_registrations(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    n = await h.discovery.tick()
    assert n == 0
    assert h.registry.list_panes() == []


async def test_pane_with_no_pid_skipped(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    # No foreground pid set → multiplexer.get_foreground_pid returns None
    n = await h.discovery.tick()
    assert n == 0


async def test_pane_with_no_jsonl_skipped(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    h.mux.set_foreground_pid("@1", 1234)
    # `discoveries` empty → fake_discover returns None
    n = await h.discovery.tick()
    assert n == 0


async def test_discovered_run_is_registered(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    h.mux.set_foreground_pid("@1", 1234)
    jsonl = Path("/p/.claude/projects/encoded/sid-abc.jsonl")
    h.discoveries[1234] = ("sid-abc", Path("/proj"), jsonl)

    n = await h.discovery.tick()
    assert n == 1
    ptr = h.registry.get_run_pointer("@1")
    assert ptr is not None
    assert ptr.run_id == "sid-abc"
    assert ptr.cwd == Path("/proj")
    # Watcher was told to track this run.
    assert h.watcher.is_tracked("sid-abc")


async def test_two_panes_each_registered(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p1", Path("/p1"))
    h.mux.add_pane("@2", "p2", Path("/p2"))
    h.mux.set_foreground_pid("@1", 100)
    h.mux.set_foreground_pid("@2", 200)
    h.discoveries[100] = ("sid-1", Path("/p1"), Path("/x/sid-1.jsonl"))
    h.discoveries[200] = ("sid-2", Path("/p2"), Path("/x/sid-2.jsonl"))

    n = await h.discovery.tick()
    assert n == 2
    assert h.registry.get_run_pointer("@1") is not None
    assert h.registry.get_run_pointer("@2") is not None
    assert h.watcher.is_tracked("sid-1")
    assert h.watcher.is_tracked("sid-2")


async def test_re_register_after_clear_picks_up_new_sid(harness) -> None:  # type: ignore[no-untyped-def]
    """`/clear` rotates the JSONL → new sid for the same pane.
    Next discovery tick adopts the new pointer AND stops the watcher
    tailing the abandoned JSONL — without that step, every /clear
    leaves a dead tracker behind."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    h.mux.set_foreground_pid("@1", 100)
    h.discoveries[100] = ("sid-old", Path("/p"), Path("/x/sid-old.jsonl"))
    await h.discovery.tick()
    assert h.watcher.is_tracked("sid-old")

    h.discoveries[100] = ("sid-new", Path("/p"), Path("/x/sid-new.jsonl"))
    await h.discovery.tick()

    ptr = h.registry.get_run_pointer("@1")
    assert ptr is not None and ptr.run_id == "sid-new"
    assert h.watcher.is_tracked("sid-new")
    # The abandoned sid is no longer tracked by the watcher.
    assert not h.watcher.is_tracked("sid-old")


async def test_no_untrack_when_sid_unchanged(harness) -> None:  # type: ignore[no-untyped-def]
    """Re-discovering the same sid (every steady-state tick) must
    not flap the watcher's tracking — track is idempotent and we
    never untrack the same id we just tracked."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    h.mux.set_foreground_pid("@1", 100)
    h.discoveries[100] = ("sid-stable", Path("/p"), Path("/x/sid-stable.jsonl"))

    await h.discovery.tick()
    await h.discovery.tick()
    await h.discovery.tick()

    assert h.watcher.is_tracked("sid-stable")


# ── periodic loop ───────────────────────────────────────────────


async def test_periodic_loop_runs_ticks() -> None:
    mux = FakeMultiplexer()
    mux.add_pane("@1", "p", Path("/p"))
    mux.set_foreground_pid("@1", 999)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    watcher = FakeWatcher()

    def fake_discover(
        pid: int, _exclude: frozenset[str] = frozenset()
    ) -> tuple[str, Path, Path] | None:
        if pid == 999:
            return ("sid-x", Path("/p"), Path("/p/.claude/sid-x.jsonl"))
        return None

    discovery = RunDiscovery(
        multiplexer=mux,
        registry=registry,
        watcher=watcher,
        poll_interval=0.02,
        discover=fake_discover,
    )

    await discovery.start()
    try:
        for _ in range(50):
            if registry.get_run_pointer("@1") is not None:
                break
            await asyncio.sleep(0.02)
    finally:
        await discovery.stop()

    assert registry.get_run_pointer("@1") is not None


async def test_double_start_is_idempotent(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.discovery.start()
    await h.discovery.start()
    await h.discovery.stop()


async def test_stop_without_start_is_clean(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.discovery.stop()


# ── cleanup: claude exited, pane still exists ───────────────────


async def test_run_pointer_cleared_after_miss_threshold() -> None:
    """When claude exits, the pane's foreground reverts to the
    shell. discover_run returns None for the shell pid; after
    `miss_threshold` consecutive misses, the run pointer drops so
    the pane stops appearing in /sessions → Active."""
    mux = FakeMultiplexer()
    mux.add_pane("@1", "alpha", Path("/p"))
    mux.set_foreground_pid("@1", 4242)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    await registry.register_run("@1", "old-sid", Path("/p"))
    watcher = FakeWatcher()
    watcher.track("old-sid", Path("/p/old-sid.jsonl"))

    def never_discover(
        _pid: int, _exclude: frozenset[str] = frozenset()
    ) -> tuple[str, Path, Path] | None:
        return None

    d = RunDiscovery(
        multiplexer=mux,
        registry=registry,
        watcher=watcher,
        poll_interval=0.05,
        discover=never_discover,
        miss_threshold=3,
    )
    # Two misses: pointer still there, threshold not yet hit.
    await d.tick()
    await d.tick()
    assert registry.get_run_pointer("@1") is not None
    # Third miss → cleared.
    await d.tick()
    assert registry.get_run_pointer("@1") is None
    assert not watcher.is_tracked("old-sid")


async def test_successful_discovery_resets_miss_counter() -> None:
    """A flapping discovery (1 miss → success → 1 miss → success)
    must not eventually drop the pointer; only consecutive misses
    count toward the threshold."""
    mux = FakeMultiplexer()
    mux.add_pane("@1", "alpha", Path("/p"))
    mux.set_foreground_pid("@1", 4242)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    await registry.register_run("@1", "sid-a", Path("/p"))
    watcher = FakeWatcher()

    state = {"misses_so_far": 0}

    def flap_discover(
        _pid: int, _exclude: frozenset[str] = frozenset()
    ) -> tuple[str, Path, Path] | None:
        # Alternate miss / success so we never accumulate threshold.
        state["misses_so_far"] += 1
        if state["misses_so_far"] % 2 == 1:
            return None
        return ("sid-a", Path("/p"), Path("/p/sid-a.jsonl"))

    d = RunDiscovery(
        multiplexer=mux,
        registry=registry,
        watcher=watcher,
        poll_interval=0.05,
        discover=flap_discover,
        miss_threshold=3,
    )
    for _ in range(10):
        await d.tick()
    assert registry.get_run_pointer("@1") is not None


async def test_no_run_pointer_no_miss_tracking() -> None:
    """A pane with no registered run pointer to begin with shouldn't
    accrue misses — the cleanup path is "drop a stale pointer", not
    "track every pane forever"."""
    mux = FakeMultiplexer()
    mux.add_pane("@1", "alpha", Path("/p"))
    mux.set_foreground_pid("@1", 4242)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    watcher = FakeWatcher()

    def never_discover(
        _pid: int, _exclude: frozenset[str] = frozenset()
    ) -> tuple[str, Path, Path] | None:
        return None

    d = RunDiscovery(
        multiplexer=mux,
        registry=registry,
        watcher=watcher,
        poll_interval=0.05,
        discover=never_discover,
        miss_threshold=3,
    )
    for _ in range(5):
        await d.tick()
    assert d._misses == {}  # noqa: SLF001


# ── cleanup: pane gone entirely ─────────────────────────────────


async def test_vanished_pane_clears_pointer_and_cascade_unbinds() -> None:
    """When a pane disappears from the multiplexer (tmux window
    killed / host rebooted), the registry's run pointer + any
    bindings for it must drop. Otherwise /sessions Active keeps
    showing a phantom row pointing nowhere."""
    from paige.domain.conversation import Conversation
    from paige.domain.person import Person

    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    # Pane was registered and bound, then vanished from tmux.
    person = Person(user_id="u-alice")
    conv = Conversation(chat_id="-100", thread_id="42")
    await registry.register_run("@gone", "ghost-sid", Path("/p"))
    await registry.bind(person, conv, "@gone")
    watcher = FakeWatcher()
    watcher.track("ghost-sid", Path("/p/ghost.jsonl"))

    d = RunDiscovery(
        multiplexer=mux,
        registry=registry,
        watcher=watcher,
        poll_interval=0.05,
    )
    await d.tick()
    # Run pointer + binding both gone (cascade via remove_pane).
    assert registry.get_run_pointer("@gone") is None
    assert registry.get_pane(person, conv) is None
    assert not watcher.is_tracked("ghost-sid")


async def test_live_pane_with_pointer_is_left_alone() -> None:
    """A pane that's both in the multiplexer AND has a current
    pointer must not be touched by the cleanup paths — basic
    no-regression check that bracketed the broader cleanup logic."""
    mux = FakeMultiplexer()
    mux.add_pane("@1", "alpha", Path("/p"))
    mux.set_foreground_pid("@1", 4242)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    await registry.register_run("@1", "sid-a", Path("/p"))
    watcher = FakeWatcher()

    def discover_a(
        _pid: int, _exclude: frozenset[str] = frozenset()
    ) -> tuple[str, Path, Path] | None:
        return ("sid-a", Path("/p"), Path("/p/sid-a.jsonl"))

    d = RunDiscovery(
        multiplexer=mux,
        registry=registry,
        watcher=watcher,
        poll_interval=0.05,
        discover=discover_a,
    )
    await d.tick()
    assert registry.get_run_pointer("@1") is not None
