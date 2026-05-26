"""ReadinessService — drive state from a synthetic event stream."""

from __future__ import annotations

import logging

import pytest

from paige.application.readiness import ReadinessService
from paige.domain.transcript import (  # noqa: F401
    Block,
    BlockKind,
    Role,
    StopReason,
    TranscriptEvent,
)


def _assistant(stop_reason: StopReason | None, *, text: str = "ok") -> TranscriptEvent:
    return TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text=text),),
        stop_reason=stop_reason,
    )


def _user(text: str = "hi") -> TranscriptEvent:
    return TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TEXT, text=text),),
    )


async def test_unknown_run_is_not_ready_by_default() -> None:
    """Before any events land, every run is implicitly NOT_READY —
    we have no evidence claude has finished."""
    svc = ReadinessService()
    assert svc.is_ready("run-x") is False


async def test_end_turn_flips_to_ready() -> None:
    svc = ReadinessService()
    await svc._handle_event("run-x", _assistant(StopReason.END_TURN))
    assert svc.is_ready("run-x") is True


async def test_tool_use_stays_not_ready() -> None:
    """`tool_use` means claude is mid agent loop — still NOT_READY."""
    svc = ReadinessService()
    await svc._handle_event("run-x", _assistant(StopReason.TOOL_USE))
    assert svc.is_ready("run-x") is False


async def test_user_event_flips_back_to_not_ready() -> None:
    """A user message (the user typed something / a tool_result came
    back) means the agent loop has restarted — NOT_READY."""
    svc = ReadinessService()
    await svc._handle_event("run-x", _assistant(StopReason.END_TURN))
    assert svc.is_ready("run-x") is True
    await svc._handle_event("run-x", _user())
    assert svc.is_ready("run-x") is False


async def test_multiple_tool_uses_then_end_turn_only_flips_at_the_end() -> None:
    """The typical agent-loop shape: N tool_use events then one
    end_turn. We only cross to READY on the final end_turn."""
    svc = ReadinessService()
    for _ in range(3):
        await svc._handle_event("run-x", _assistant(StopReason.TOOL_USE))
        assert svc.is_ready("run-x") is False
    await svc._handle_event("run-x", _assistant(StopReason.END_TURN))
    assert svc.is_ready("run-x") is True


async def test_runs_are_isolated() -> None:
    """One run's state doesn't leak into another."""
    svc = ReadinessService()
    await svc._handle_event("a", _assistant(StopReason.END_TURN))
    await svc._handle_event("b", _assistant(StopReason.TOOL_USE))
    assert svc.is_ready("a") is True
    assert svc.is_ready("b") is False


async def test_max_tokens_does_not_count_as_ready() -> None:
    """`max_tokens` is an unhealthy stop — claude is technically
    waiting for the user but the context is full. We treat it as
    NOT_READY so we don't fire a "ready" panel into a broken state."""
    svc = ReadinessService()
    await svc._handle_event("run-x", _assistant(StopReason.MAX_TOKENS))
    assert svc.is_ready("run-x") is False


async def test_subscribers_fire_on_transition() -> None:
    svc = ReadinessService()
    received: list[tuple[str, bool, StopReason | None]] = []

    async def handler(run_id: str, ready: bool, event: TranscriptEvent) -> None:
        received.append((run_id, ready, event.stop_reason))

    svc.on_change(handler)
    await svc._handle_event("run-x", _assistant(StopReason.END_TURN))
    await svc._handle_event("run-x", _assistant(StopReason.TOOL_USE))
    assert received == [
        ("run-x", True, StopReason.END_TURN),
        ("run-x", False, StopReason.TOOL_USE),
    ]


async def test_subscribers_do_not_fire_on_no_change() -> None:
    """If the bit doesn't flip, no subscriber wakeups — avoids
    log/UI noise during a long agent loop where every event has
    stop_reason=tool_use."""
    svc = ReadinessService()
    received: list[tuple[str, bool]] = []

    async def handler(run_id: str, ready: bool, _event: TranscriptEvent) -> None:
        received.append((run_id, ready))

    svc.on_change(handler)
    for _ in range(5):
        await svc._handle_event("run-x", _assistant(StopReason.TOOL_USE))
    # First call sets NOT_READY (from implicit-False); subsequent
    # tool_use events don't transition. But the very first call DOES
    # cross from "unobserved" to NOT_READY, so we get one notification.
    assert received == [("run-x", False)]


async def test_subscriber_exception_does_not_break_state() -> None:
    """A broken handler shouldn't poison the state machine for the
    rest of the system."""
    svc = ReadinessService()

    async def broken(_run_id: str, _ready: bool, _event: TranscriptEvent) -> None:
        raise RuntimeError("boom")

    svc.on_change(broken)
    await svc._handle_event("run-x", _assistant(StopReason.END_TURN))
    assert svc.is_ready("run-x") is True


async def test_subscriber_receives_event_payload() -> None:
    """Handlers need the event itself to distinguish USER-text
    NOT_READY (panel-morph-to-receipt) from tool_use NOT_READY
    (panel-morph-to-working)."""
    svc = ReadinessService()
    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, _ready: bool, event: TranscriptEvent) -> None:
        received.append(event)

    svc.on_change(handler)
    user_ev = _user("hello from tmux")
    await svc._handle_event("run-x", user_ev)
    assert received == [user_ev]


async def test_logs_on_transition(caplog: pytest.LogCaptureFixture) -> None:
    svc = ReadinessService()
    with caplog.at_level(logging.INFO, logger="paige.application.readiness"):
        await svc._handle_event("run-x", _assistant(StopReason.END_TURN))
        await svc._handle_event("run-x", _assistant(StopReason.TOOL_USE))
    msgs = [r.message for r in caplog.records]
    assert any("READY" in m and "end_turn" in m for m in msgs)
    assert any("NOT_READY" in m and "tool_use" in m for m in msgs)
