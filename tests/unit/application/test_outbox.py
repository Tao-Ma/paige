"""Outbox — per-person serialized dispatcher over a Channel."""

from __future__ import annotations

import asyncio

import pytest

from paige.application.outbox import Outbox
from paige.domain.conversation import Anchor, Conversation
from paige.domain.outbound import Outbound, TextContent, TypingContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel

ALICE = Person(user_id="u-alice", display_name="Alice")
BOB = Person(user_id="u-bob", display_name="Bob")
CONV = Conversation(chat_id="-100", thread_id="42")


def _text_outbound(t: str) -> Outbound:
    return Outbound(conversation=CONV, content=TextContent(t))


# ── basic enqueue ────────────────────────────────────────────────


async def test_enqueue_send_calls_channel_and_resolves_future() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    fut = outbox.enqueue_send(ALICE, _text_outbound("hi"))
    anchor = await fut
    assert anchor is not None
    assert anchor.conversation == CONV
    assert len(ch.sent) == 1
    await outbox.stop()


async def test_enqueue_send_typing_returns_none() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    fut = outbox.enqueue_send(ALICE, Outbound(conversation=CONV, content=TypingContent()))
    anchor = await fut
    assert anchor is None
    await outbox.stop()


async def test_enqueue_edit_calls_channel() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    anchor = Anchor(conversation=CONV, message_id="9")
    out = _text_outbound("new")
    fut = outbox.enqueue_edit(ALICE, anchor, out)
    result = await fut
    assert result is None  # patched in place
    assert ch.edits == [(anchor, out)]
    await outbox.stop()


async def test_enqueue_edit_returns_replacement_anchor_on_fallback() -> None:
    ch = FakeChannel()
    new_anchor = Anchor(conversation=CONV, message_id="999")
    ch.edit_returns_once(new_anchor)
    outbox = Outbox(ch)
    fut = outbox.enqueue_edit(ALICE, Anchor(conversation=CONV, message_id="1"), _text_outbound("x"))
    assert await fut is new_anchor
    await outbox.stop()


async def test_enqueue_delete_calls_channel() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    anchor = Anchor(conversation=CONV, message_id="42")
    fut = outbox.enqueue_delete(ALICE, anchor)
    assert await fut is None
    assert ch.deleted == [anchor]
    await outbox.stop()


# ── ordering ─────────────────────────────────────────────────────


async def test_per_user_fifo_ordering() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    futs = [outbox.enqueue_send(ALICE, _text_outbound(str(i))) for i in range(10)]
    await asyncio.gather(*futs)
    sent_texts = [out.content.text for out in ch.sent if isinstance(out.content, TextContent)]
    assert sent_texts == [str(i) for i in range(10)]
    await outbox.stop()


async def test_two_users_have_independent_workers() -> None:
    """Each user's queue runs in its own worker — Bob shouldn't be
    blocked behind Alice's pending tasks."""
    ch = FakeChannel()
    outbox = Outbox(ch)
    alice_fut = outbox.enqueue_send(ALICE, _text_outbound("a-1"))
    bob_fut = outbox.enqueue_send(BOB, _text_outbound("b-1"))
    # Both resolve.
    await asyncio.gather(alice_fut, bob_fut)
    assert {(o.content.text if isinstance(o.content, TextContent) else None) for o in ch.sent} == {
        "a-1",
        "b-1",
    }
    await outbox.stop()


async def test_queue_serializes_concurrent_per_user_calls() -> None:
    """When channel.send is slow, the SECOND enqueued task must wait
    until the first finishes — never two concurrent sends for one
    user."""
    ch = FakeChannel()
    in_flight = 0
    max_in_flight = 0
    sent_count = 0
    sem = asyncio.Lock()

    original_send = ch.send

    async def slow_send(outbound: Outbound):  # type: ignore[no-untyped-def]
        nonlocal in_flight, max_in_flight, sent_count
        async with sem:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with sem:
            in_flight -= 1
            sent_count += 1
        return await original_send(outbound)

    ch.send = slow_send  # type: ignore[method-assign]

    outbox = Outbox(ch)
    futs = [outbox.enqueue_send(ALICE, _text_outbound(str(i))) for i in range(5)]
    await asyncio.gather(*futs)
    assert max_in_flight == 1  # never two concurrent
    assert sent_count == 5
    await outbox.stop()


# ── error handling ──────────────────────────────────────────────


async def test_send_error_propagates_via_future() -> None:
    ch = FakeChannel()
    ch.fail_send_once(RuntimeError("boom"))
    outbox = Outbox(ch)
    fut = outbox.enqueue_send(ALICE, _text_outbound("doomed"))
    with pytest.raises(RuntimeError, match="boom"):
        await fut
    await outbox.stop()


async def test_one_send_error_does_not_block_subsequent_sends() -> None:
    ch = FakeChannel()
    ch.fail_send_once(RuntimeError("boom"))
    outbox = Outbox(ch)
    bad = outbox.enqueue_send(ALICE, _text_outbound("first"))
    good = outbox.enqueue_send(ALICE, _text_outbound("second"))
    with pytest.raises(RuntimeError):
        await bad
    anchor = await good
    assert anchor is not None
    await outbox.stop()


# ── stop / drain ─────────────────────────────────────────────────


async def test_stop_drains_pending_tasks() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)

    # Enqueue several without awaiting — stop should drain them all.
    futs = [outbox.enqueue_send(ALICE, _text_outbound(str(i))) for i in range(5)]
    await outbox.stop()
    # All futures should have resolved.
    assert all(f.done() and f.exception() is None for f in futs)
    assert len(ch.sent) == 5


async def test_enqueue_after_stop_rejects_future() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    await outbox.stop()
    fut = outbox.enqueue_send(ALICE, _text_outbound("late"))
    with pytest.raises(RuntimeError, match="stopping"):
        await fut


async def test_stop_with_hung_send_hits_timeout() -> None:
    """A channel.send that never returns must not block stop()
    indefinitely — the per-person drain timeout cancels."""
    ch = FakeChannel()

    async def hang(_outbound: Outbound):  # type: ignore[no-untyped-def]
        await asyncio.sleep(60)

    ch.send = hang  # type: ignore[method-assign]
    outbox = Outbox(ch, drain_timeout_per_person=0.05)
    outbox.enqueue_send(ALICE, _text_outbound("hung"))

    # stop() must complete despite the hung send.
    await asyncio.wait_for(outbox.stop(), timeout=2.0)


async def test_stop_with_no_active_users_is_noop() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    await outbox.stop()  # no enqueues; stop returns cleanly


async def test_stop_is_idempotent() -> None:
    ch = FakeChannel()
    outbox = Outbox(ch)
    outbox.enqueue_send(ALICE, _text_outbound("x"))
    await outbox.stop()
    await outbox.stop()  # second stop is harmless
