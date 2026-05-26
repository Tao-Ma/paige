"""AgentBatchService — coalesce parallel Agent/Task launches into one card."""

from __future__ import annotations

import json

from paige.application.agent_batch import AgentBatchService
from paige.application.outbox import Outbox
from paige.domain.conversation import Conversation
from paige.domain.outbound import CardContent
from paige.domain.pane import Binding
from paige.domain.person import Person
from paige.domain.transcript import Block, BlockKind
from paige.testing.fakes import FakeChannel

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="t1")
BINDING = Binding(person=ALICE, conversation=CONV, pane_id="@1")


def _use(tool_id: str, subagent: str, desc: str) -> Block:
    return Block(
        kind=BlockKind.TOOL_USE,
        tool_id=tool_id,
        tool_name="Agent",
        text=json.dumps({"subagent_type": subagent, "description": desc, "prompt": "go"}),
    )


def _result(tool_id: str) -> Block:
    return Block(kind=BlockKind.TOOL_RESULT, tool_id=tool_id, text="done")


async def _svc() -> tuple[FakeChannel, Outbox, AgentBatchService]:
    channel = FakeChannel()
    outbox = Outbox(channel)
    return channel, outbox, AgentBatchService(outbox=outbox)


def _last_card_text(channel: FakeChannel) -> str:
    """Most recent card body across sends + edits."""
    events: list[object] = [o for o in channel.sent]
    events += [ob for _anchor, ob in channel.edits]
    last = events[-1]
    assert isinstance(last.content, CardContent)  # type: ignore[attr-defined]
    return last.content.card.text  # type: ignore[attr-defined]


async def test_fanout_is_one_send_then_edits() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _use("t1", "Explore", "find auth"))
    await svc.on_use([BINDING], _use("t2", "general-purpose", "refactor"))
    await svc.on_use([BINDING], _use("t3", "Plan", "design"))
    await outbox.stop()

    # One card created, the rest are in-place edits — not 3 cards.
    assert len(channel.sent) == 1
    assert len(channel.edits) == 2
    body = _last_card_text(channel)
    assert body.count("⏳") == 3
    assert "Explore" in body and "general-purpose" in body and "Plan" in body


async def test_result_ticks_its_line() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _use("t1", "Explore", "find auth"))
    await svc.on_use([BINDING], _use("t2", "Plan", "design"))
    await svc.on_result([BINDING], _result("t1"))
    await outbox.stop()

    body = _last_card_text(channel)
    assert body.count("✓") == 1
    assert body.count("⏳") == 1
    # Header counts completion.
    last = channel.edits[-1][1]
    assert isinstance(last.content, CardContent)
    assert last.content.card.header_title == "🤖 Agents · 1/2 done"


async def test_close_starts_a_fresh_card() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _use("t1", "Explore", "a"))
    svc.close([BINDING])  # a non-agent block arrived
    await svc.on_use([BINDING], _use("t2", "Plan", "b"))
    await outbox.stop()

    # Two separate batch cards (two sends), no cross-batch edit.
    assert len(channel.sent) == 2


async def test_late_result_after_close_still_patches() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _use("t1", "Explore", "a"))
    await svc.on_use([BINDING], _use("t2", "Plan", "b"))
    svc.close([BINDING])  # batch closed while agents still running
    await svc.on_result([BINDING], _result("t2"))
    await outbox.stop()

    body = _last_card_text(channel)
    assert body.count("✓") == 1
    assert body.count("⏳") == 1


async def test_owns_tracks_agent_tool_ids() -> None:
    _channel, outbox, svc = await _svc()
    assert not svc.owns("t1")
    await svc.on_use([BINDING], _use("t1", "Explore", "a"))
    await outbox.stop()
    assert svc.owns("t1")
    assert not svc.owns("nope")


async def test_result_for_unknown_tool_is_noop() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_result([BINDING], _result("ghost"))
    await outbox.stop()
    assert channel.sent == []
    assert channel.edits == []


async def test_missing_description_renders_subagent_only() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _use("t1", "Explore", ""))
    await outbox.stop()
    body = _last_card_text(channel)
    assert "**Explore**" in body
    assert "—" not in body  # no dangling separator when description empty


async def test_description_with_newline_and_backtick_is_neutralised() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _use("t1", "Explore", "do it\n# big `oops"))
    await outbox.stop()
    body = _last_card_text(channel)
    assert "\n# big" not in body  # newline collapsed → no leaked heading
    assert "`" not in body  # backtick dropped → no bleeding code span
