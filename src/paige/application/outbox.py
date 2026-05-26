"""Outbox — per-person serialized outbound queue over a `Channel`.

One asyncio.Queue + worker task per Person. Messages enqueued for the
same person process in FIFO order through a single Channel call at a
time, so the backend never sees concurrent writes for a single user.
Different persons run their workers concurrently.

Each enqueue returns an `asyncio.Future` resolving when the work
completes — Anchor for `send` (None for fire-and-forget content like
typing, or whatever the Channel returned), Anchor-or-None for `edit`
(non-None signals the channel had to delete+resend, so the caller's
tracking should adopt the new anchor), None for `delete`.

Errors from the Channel propagate via the Future. Rate-limit handling
lives at the Channel layer (TokenBucket inside the Feishu adapter);
the Outbox is a serializer + dispatch + futures, no retry.

`stop()` drains all queues with a per-person timeout, then cancels
workers. Enqueues after `stop()` immediately reject the returned
Future — callers see the failure rather than a silently lost task.

Merging consecutive sends, tool_use ↔ tool_result pairing, and
status-card lifecycle are explicitly NOT this layer's job — they
belong in the Dispatcher / StatusService that build Outbound
requests above the Outbox.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..domain.card import Card
from ..domain.conversation import Anchor, Conversation
from ..domain.outbound import CardContent, Outbound
from ..domain.person import Person
from ..ports.channel import Channel
from .collapse_pref import CollapsePrefService
from .message_seq import MessageSeqService

logger = logging.getLogger(__name__)


@dataclass
class _SendTask:
    person: Person
    outbound: Outbound
    future: asyncio.Future[Anchor | None]
    # When `seq is not None`, the outbound was stamped at enqueue
    # time; the worker calls `MessageSeqService.record_send_anchor`
    # after the channel returns so subsequent edits can chain.
    seq: int | None = None
    # `StatusCarrierService` uses this to PATCH carriers without
    # re-triggering its own on_send_complete handler.
    suppress_hooks: bool = False


@dataclass
class _EditTask:
    person: Person
    anchor: Anchor
    outbound: Outbound
    future: asyncio.Future[Anchor | None]
    suppress_hooks: bool = False


@dataclass
class _DeleteTask:
    person: Person
    anchor: Anchor
    future: asyncio.Future[None]


_Task = _SendTask | _EditTask | _DeleteTask


SendCompleteHandler = Callable[
    [Person, Conversation, Anchor, Card],
    Awaitable[None],
]


class Outbox:
    """Per-person serialized dispatcher over a `Channel`."""

    def __init__(
        self,
        channel: Channel,
        *,
        drain_timeout_per_person: float = 10.0,
        message_seq: MessageSeqService | None = None,
        collapse_pref: CollapsePrefService | None = None,
    ) -> None:
        self._channel = channel
        self._drain_timeout = drain_timeout_per_person
        # Per-send handlers called after a successful send/edit.
        # Used by `StatusCarrierService` to migrate the status badge
        # to the most recent outbound card. Handlers fire on both
        # new sends AND edits — the carrier service distinguishes by
        # comparing the anchor against its stored current carrier.
        self._send_complete_handlers: list[SendCompleteHandler] = []
        # Optional seq stamper. When wired in, every enqueue gets
        # the seq footer appended at enqueue time (so the worker
        # sees a pre-stamped Outbound). Tests that don't care about
        # stamping pass None and pay no cost.
        self._seq = message_seq
        # Optional per-(person, conversation) collapse-threshold
        # stamper. When wired, every enqueue carries the user's
        # current threshold via `Outbound.collapse_threshold_lines`;
        # the Feishu adapter reads it at render time. Tests that
        # don't care leave it None and Outbound stays at 0 = flat.
        self._collapse_pref = collapse_pref
        self._queues: dict[str, asyncio.Queue[_Task]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    # ── enqueue ──────────────────────────────────────────────────

    def enqueue_send(
        self, person: Person, outbound: Outbound, *, suppress_hooks: bool = False
    ) -> asyncio.Future[Anchor | None]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Anchor | None] = loop.create_future()
        if self._collapse_pref is not None:
            outbound = self._collapse_pref.stamp(person, outbound.conversation, outbound)
        seq: int | None = None
        if self._seq is not None:
            outbound, seq = self._seq.stamp_send(person, outbound.conversation, outbound)
        self._enqueue(
            person,
            _SendTask(
                person=person,
                outbound=outbound,
                future=future,
                seq=seq,
                suppress_hooks=suppress_hooks,
            ),
        )
        return future

    def enqueue_edit(
        self,
        person: Person,
        anchor: Anchor,
        outbound: Outbound,
        *,
        suppress_hooks: bool = False,
    ) -> asyncio.Future[Anchor | None]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Anchor | None] = loop.create_future()
        if self._collapse_pref is not None:
            outbound = self._collapse_pref.stamp(person, outbound.conversation, outbound)
        if self._seq is not None:
            outbound, _ = self._seq.stamp_edit(person, outbound.conversation, anchor, outbound)
        self._enqueue(
            person,
            _EditTask(
                person=person,
                anchor=anchor,
                outbound=outbound,
                future=future,
                suppress_hooks=suppress_hooks,
            ),
        )
        return future

    def enqueue_delete(self, person: Person, anchor: Anchor) -> asyncio.Future[None]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._enqueue(person, _DeleteTask(person=person, anchor=anchor, future=future))
        return future

    def on_send_complete(self, handler: SendCompleteHandler) -> None:
        """Register a handler called after every successful send/edit
        with the resulting anchor + the Card that was sent. Used by
        `StatusCarrierService` to track the most recent outbound
        per (person, conversation) and migrate the live status
        badge. Handlers must be idempotent — order across multiple
        registered handlers is registration order."""
        self._send_complete_handlers.append(handler)

    def _enqueue(self, person: Person, task: _Task) -> None:
        if self._stopping:
            task.future.set_exception(RuntimeError("Outbox is stopping; new enqueues are rejected"))
            return
        uid = person.user_id
        queue = self._queues.get(uid)
        if queue is None:
            queue = asyncio.Queue[_Task]()
            self._queues[uid] = queue
            self._workers[uid] = asyncio.create_task(self._worker(uid), name=f"outbox-worker-{uid}")
        queue.put_nowait(task)

    # ── worker ───────────────────────────────────────────────────

    async def _worker(self, user_id: str) -> None:
        queue = self._queues[user_id]
        while True:
            task = await queue.get()
            try:
                await self._process(task)
            finally:
                queue.task_done()

    async def _process(self, task: _Task) -> None:
        try:
            if isinstance(task, _SendTask):
                anchor = await self._channel.send(task.outbound)
                # Bind the allocated seq to the message_id the
                # channel returned, so future edits on this anchor
                # can extend the chain (rather than starting a new
                # one rooted at the edit).
                if anchor is not None and task.seq is not None and self._seq is not None:
                    self._seq.record_send_anchor(anchor, task.seq)
                task.future.set_result(anchor)
                await self._maybe_fire_send_complete(task, anchor)
            elif isinstance(task, _EditTask):
                anchor = await self._channel.edit(task.anchor, task.outbound)
                task.future.set_result(anchor)
                # Fire with the ORIGINAL anchor we patched, not the
                # channel's returned value — some adapters return
                # None on a successful edit.
                await self._maybe_fire_send_complete(task, task.anchor)
            else:  # _DeleteTask
                await self._channel.delete(task.anchor)
                task.future.set_result(None)
        except Exception as e:
            logger.warning("Outbox task failed: %s", e)
            if not task.future.done():
                task.future.set_exception(e)

    async def _maybe_fire_send_complete(
        self, task: _SendTask | _EditTask, anchor: Anchor | None
    ) -> None:
        """Fire on_send_complete handlers when an outbound CardContent
        send/edit succeeded with a known anchor. `suppress_hooks` lets
        the StatusCarrierService PATCH a carrier (or an old carrier)
        to update/strip its badge without re-triggering itself in a
        loop. Non-card outbounds (TextContent, DocumentContent) skip
        the hook — the carrier tracks card surfaces only."""
        if task.suppress_hooks or anchor is None:
            return
        if not isinstance(task.outbound.content, CardContent):
            return
        for handler in self._send_complete_handlers:
            try:
                await handler(
                    task.person, task.outbound.conversation, anchor, task.outbound.content.card
                )
            except Exception as e:
                logger.warning("send_complete handler failed: %s", e)

    # ── lifecycle ────────────────────────────────────────────────

    async def stop(self) -> None:
        """Drain queues with `drain_timeout_per_person`, then cancel
        workers. After stop, new enqueues immediately reject."""
        self._stopping = True

        await asyncio.gather(
            *(self._drain_one(uid) for uid in self._queues),
            return_exceptions=True,
        )

        for worker in self._workers.values():
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)

    async def _drain_one(self, user_id: str) -> None:
        try:
            await asyncio.wait_for(
                self._queues[user_id].join(),
                timeout=self._drain_timeout,
            )
        except TimeoutError:
            logger.warning(
                "Outbox drain timed out for user %s; %d task(s) abandoned",
                user_id,
                self._queues[user_id].qsize(),
            )
