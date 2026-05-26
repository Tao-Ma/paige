"""StatusService — periodic spinner detection + handler broadcast.

The service no longer owns a status card surface. It scrapes each
tracked pane via `parse_status` and fans `(binding,
status_text_or_None)` out to registered async handlers. Dedup is
per-binding so a stable spinner doesn't refire on every tick.
Tests drive `tick()` manually; the periodic loop is exercised in
`test_periodic_loop_runs_ticks`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from paige.application.run_registry import RunRegistry
from paige.application.status_service import StatusService
from paige.domain.conversation import Conversation
from paige.domain.pane import Binding
from paige.domain.person import Person
from paige.testing.fakes import FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
BOB = Person(user_id="u-bob")
CONV_A = Conversation(chat_id="-100", thread_id="42")
CONV_B = Conversation(chat_id="-100", thread_id="43")

PANE_THINKING = "\n".join(
    [
        "✻ Thinking… (12s · 4k tokens)",
        "─────────────────────────────────",
        "> ",
    ]
)

PANE_THINKING_LONGER = "\n".join(
    [
        "✻ Thinking… (45s · 12k tokens)",
        "─────────────────────────────────",
        "> ",
    ]
)

PANE_IDLE = "\n".join(
    [
        "Done. Final answer here.",
        "─────────────────────────────────",
        "> ",
    ]
)


async def _build(
    *, mux_text: str = PANE_THINKING
) -> tuple[StatusService, FakeMultiplexer, list[tuple[Binding, str | None]]]:
    """A service + a list a registered handler will append to.
    Standard fixture used by most tests below."""
    mux = FakeMultiplexer()
    mux.add_pane("@1", "p", Path("/p"))
    mux.set_capture("@1", mux_text)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.register_run("@1", "sid", Path("/p"))
    svc = StatusService(multiplexer=mux, registry=registry, poll_interval=0.02)
    received: list[tuple[Binding, str | None]] = []

    async def handler(binding: Binding, text: str | None) -> None:
        received.append((binding, text))

    svc.on_change(handler)
    return svc, mux, received


# ── single-tick emission ─────────────────────────────────────────


async def test_spinner_emits_status_text() -> None:
    svc, _mux, received = await _build(mux_text=PANE_THINKING)
    await svc.tick()
    assert len(received) == 1
    binding, text = received[0]
    assert binding.person == ALICE
    assert text is not None
    assert "12s" in text


async def test_idle_emits_none() -> None:
    svc, _mux, received = await _build(mux_text=PANE_IDLE)
    await svc.tick()
    assert len(received) == 1
    assert received[0][1] is None


# ── dedup + change detection ────────────────────────────────────


async def test_dedup_skips_unchanged_emits() -> None:
    """Steady spinner text across ticks fires the handler exactly
    once (the first time)."""
    svc, _mux, received = await _build(mux_text=PANE_THINKING)
    await svc.tick()
    await svc.tick()
    await svc.tick()
    assert len(received) == 1


async def test_text_change_fires_again() -> None:
    svc, mux, received = await _build(mux_text=PANE_THINKING)
    await svc.tick()
    mux.set_capture("@1", PANE_THINKING_LONGER)
    await svc.tick()
    assert len(received) == 2
    assert "12s" in (received[0][1] or "")
    assert "45s" in (received[1][1] or "")


async def test_spinner_to_idle_emits_none() -> None:
    svc, mux, received = await _build(mux_text=PANE_THINKING)
    await svc.tick()
    mux.set_capture("@1", PANE_IDLE)
    await svc.tick()
    assert len(received) == 2
    assert received[0][1] is not None
    assert received[1][1] is None


async def test_idle_to_spinner_re_fires() -> None:
    svc, mux, received = await _build(mux_text=PANE_IDLE)
    await svc.tick()
    mux.set_capture("@1", PANE_THINKING)
    await svc.tick()
    assert received[0][1] is None
    assert received[1][1] is not None


# ── binding fan-out ─────────────────────────────────────────────


async def test_no_bindings_means_no_emits() -> None:
    """Panes without bindings are skipped — there's nobody to
    notify."""
    mux = FakeMultiplexer()
    mux.add_pane("@1", "p", Path("/p"))
    mux.set_capture("@1", PANE_THINKING)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    # register_run without bind = pane is known, no binding.
    await registry.register_run("@1", "sid", Path("/p"))
    svc = StatusService(multiplexer=mux, registry=registry, poll_interval=0.02)
    received: list[tuple[Binding, str | None]] = []

    async def handler(binding: Binding, text: str | None) -> None:
        received.append((binding, text))

    svc.on_change(handler)
    await svc.tick()
    assert received == []


async def test_two_bindings_each_receive_their_own_emit() -> None:
    """One pane, two bindings — both get notified per spinner
    change."""
    mux = FakeMultiplexer()
    mux.add_pane("@1", "p", Path("/p"))
    mux.set_capture("@1", PANE_THINKING)
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(BOB, CONV_B, "@1")
    await registry.register_run("@1", "sid", Path("/p"))
    svc = StatusService(multiplexer=mux, registry=registry, poll_interval=0.02)
    received: list[tuple[str, str | None]] = []

    async def handler(binding: Binding, text: str | None) -> None:
        received.append((binding.person.user_id, text))

    svc.on_change(handler)
    await svc.tick()
    user_ids = {uid for uid, _t in received}
    assert user_ids == {ALICE.user_id, BOB.user_id}


# ── handler error isolation ─────────────────────────────────────


async def test_handler_exception_does_not_break_loop() -> None:
    """A broken handler shouldn't stop other handlers or future
    ticks."""
    svc, _mux, received = await _build(mux_text=PANE_THINKING)

    async def broken(_b: Binding, _t: str | None) -> None:
        raise RuntimeError("boom")

    svc.on_change(broken)
    await svc.tick()
    # The good handler still fired.
    assert len(received) == 1


# ── periodic loop lifecycle ─────────────────────────────────────


async def test_periodic_loop_runs_ticks() -> None:
    svc, _mux, received = await _build(mux_text=PANE_THINKING)
    await svc.start()
    # Two ticks worth at poll_interval=0.02s; the loop fires at
    # least once. Dedup means subsequent identical reads don't
    # produce new emissions, so we just confirm the first tick ran.
    await asyncio.sleep(0.1)
    await svc.stop()
    assert len(received) >= 1


async def test_double_start_is_idempotent() -> None:
    svc, _mux, _r = await _build()
    await svc.start()
    await svc.start()
    await svc.stop()


async def test_stop_without_start_is_noop() -> None:
    svc, _mux, _r = await _build()
    await svc.stop()
