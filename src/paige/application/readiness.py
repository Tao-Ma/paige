"""ReadinessService — per-run "is claude waiting for input?" state.

Claude Code's JSONL carries a `stop_reason` on every assistant
record. `end_turn` is the authoritative "agent loop is done, next
user input will start a fresh turn" signal — every other value
(`tool_use`, `max_tokens`, `stop_sequence`) means claude is mid-
agent-loop and the next event will land without needing a fresh
prompt.

This service consumes the watcher's `TranscriptEvent` stream and
maintains a single bit per run:

  READY      — last seen event for this run was an `assistant` event
               with `stop_reason=end_turn` and nothing has happened
               since.
  NOT_READY  — anything else (mid agent loop, just got user input,
               just emitted a tool_use, etc.).

On every transition, the service emits an INFO log line and
notifies any subscribers (Step B will add an EndTurnPanelService
that hangs off this signal). Step A is detection + logging only —
no UI changes.

Why not just look at the spinner glyph from `parse_status`? The
spinner stays on screen as a frozen `✻ Worked for Ns` line *after*
end_turn — single-frame inspection can't distinguish "actively
animating" from "frozen on the final glyph". JSONL stop_reason is
unambiguous.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..domain.transcript import Role, StopReason, TranscriptEvent
from ..ports.watcher import Watcher

logger = logging.getLogger(__name__)


# Subscribers receive `(run_id, ready, event)` on every state change.
# `event` is the TranscriptEvent that caused the transition — handlers
# need it to know whether the NOT_READY transition was triggered by a
# user-typed message (USER role) or a mid-agent-loop tool_use (so the
# end-turn panel can morph differently in each case). Async so
# subscribers can do I/O without blocking the watcher event loop.
ReadinessHandler = Callable[[str, bool, TranscriptEvent], Awaitable[None]]


class ReadinessService:
    """Tracks per-run readiness from the JSONL `stop_reason` stream.

    Wire it once at app startup with `install(watcher)`; it
    subscribes to the watcher and updates internal state as events
    land. Other services read state via `is_ready(run_id)` or
    subscribe to transitions via `on_change(handler)`.
    """

    def __init__(self) -> None:
        # run_id → current bit. Absent run_ids are implicitly
        # NOT_READY (we have no evidence they're idle yet).
        self._ready: dict[str, bool] = {}
        self._handlers: list[ReadinessHandler] = []

    def install(self, watcher: Watcher) -> None:
        """Subscribe to the watcher. Idempotent — calling twice
        registers the handler twice, so call exactly once."""
        watcher.on_event(self._handle_event)

    def on_change(self, handler: ReadinessHandler) -> None:
        """Register `handler` to be awaited on every READY/NOT_READY
        transition. Subscribers fire AFTER the internal state has
        been updated — they can read `is_ready` consistently."""
        self._handlers.append(handler)

    def is_ready(self, run_id: str) -> bool:
        return self._ready.get(run_id, False)

    async def _handle_event(self, run_id: str, event: TranscriptEvent) -> None:
        """Drive state from one watcher event.

        Logic:
          - assistant event with stop_reason=end_turn → READY.
          - any other event (user message, assistant w/ tool_use,
            assistant w/ max_tokens, etc.) → NOT_READY.

        No-op when the bit doesn't change (avoids spurious log noise
        and subscriber wakeups when a multi-block assistant turn
        emits several events with the same stop_reason — though in
        practice Claude Code emits one event per API call).
        """
        new_state = event.role is Role.ASSISTANT and event.stop_reason is StopReason.END_TURN
        await self._set(run_id, new_state, event)

    async def _set(self, run_id: str, new_state: bool, event: TranscriptEvent) -> None:
        prior = self._ready.get(run_id)
        if prior == new_state:
            return
        self._ready[run_id] = new_state
        if new_state:
            logger.info(
                "readiness: run %s → READY (stop_reason=%s)",
                run_id,
                event.stop_reason.value if event.stop_reason else "?",
            )
        else:
            reason = (
                event.stop_reason.value
                if event.stop_reason is not None
                else f"{event.role.value}-event"
            )
            logger.info("readiness: run %s → NOT_READY (%s)", run_id, reason)
        for handler in self._handlers:
            try:
                await handler(run_id, new_state, event)
            except Exception as e:
                logger.warning("readiness handler failed: %s", e)


__all__ = ["ReadinessHandler", "ReadinessService"]
