"""StatusService — periodic pane scrape; fan out spinner state to handlers.

This service NO LONGER owns its own status card surface. Phase 1
of the status-on-panel migration: the live spinner text is folded
into the `EndTurnPanelService` panel anchor so the user has one
canonical "where in the loop are we" surface, always findable as
part of the turn-boundary card. Whatever earlier slices called
"the spinner card" is gone — see commit history pre-this-change.

What this service does now:
  - poll every `poll_interval` seconds,
  - scrape each tracked pane via `parse_status`,
  - call every registered handler with `(binding, status_text)`
    where `status_text` is the spinner body (e.g. `"Worked for 12s
    (4k tokens)"`) or None for idle.
  - dedup per binding so a steady spinner doesn't fire a handler
    every tick.

Handlers (today: `EndTurnPanelService.update_working_status`)
decide what to do with the signal — PATCH the panel header, log
it, ignore it pre-first-panel, etc. This service no longer
touches `Outbox` or `Channel`.

Interactive UI suppression (AskUser / ExitPlan / Permission card
detection from v1) is still out of scope here — when that lands
it'll be another handler subscribing to the same stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..domain.pane import Binding
from ..infrastructure.terminal_parser import parse_status
from ..ports.multiplexer import Multiplexer
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

_BindingKey = tuple[str, str, str]  # (user_id, chat_id, thread_id_or_empty)

# Handlers receive `(binding, status_text_or_None)`. None means
# claude is idle (no spinner). Async so handlers can do I/O
# (e.g. PATCH a card) without blocking the scrape loop.
StatusHandler = Callable[[Binding, str | None], Awaitable[None]]


class StatusService:
    """Periodic pane scrape; broadcasts spinner state to handlers."""

    def __init__(
        self,
        *,
        multiplexer: Multiplexer,
        registry: RunRegistry,
        poll_interval: float = 1.0,
    ) -> None:
        self._multiplexer = multiplexer
        self._registry = registry
        self._poll_interval = poll_interval
        # Per-binding last-emitted status text, for dedup. Absent
        # key means "we've never emitted for this binding"; explicit
        # None means "last emit was idle." Distinguishing the two
        # matters so the first tick on a fresh binding fires the
        # handler (callers may want to seed UI state).
        self._last_emitted: dict[_BindingKey, str | None] = {}
        self._handlers: list[StatusHandler] = []
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # ── handler registration ─────────────────────────────────────

    def on_change(self, handler: StatusHandler) -> None:
        """Register `handler` for every (binding, status_text) change
        we detect. Dedup is per-binding — calling twice doesn't
        register twice; idempotent only on identity."""
        self._handlers.append(handler)

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="status-service")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as e:
                logger.warning("StatusService tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    # ── per-tick logic (public for tests) ────────────────────────

    async def tick(self) -> None:
        """One scrape cycle. Public so tests can drive it
        deterministically without spinning the asyncio loop."""
        for pane_id in self._registry.list_panes():
            bindings = self._registry.find_bindings_for_pane(pane_id)
            if not bindings:
                continue
            text = await self._multiplexer.capture(pane_id)
            status = parse_status(text or "")
            new_text = status.text if status.spinner else None
            for binding in bindings:
                await self._maybe_notify(binding, new_text)

    async def _maybe_notify(self, binding: Binding, new_text: str | None) -> None:
        key = self._key(binding)
        # Dedup: skip the handler chain when the value hasn't moved
        # since the last emit. First-tick always fires (key absent).
        if key in self._last_emitted and self._last_emitted[key] == new_text:
            return
        self._last_emitted[key] = new_text
        for handler in self._handlers:
            try:
                await handler(binding, new_text)
            except Exception as e:
                logger.warning("status handler failed: %s", e)

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _key(binding: Binding) -> _BindingKey:
        return (
            binding.person.user_id,
            binding.conversation.chat_id,
            binding.conversation.thread_id or "",
        )


__all__ = ["StatusHandler", "StatusService"]
