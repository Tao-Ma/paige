"""FakeChannel — observable in-memory Channel for tests."""

from __future__ import annotations

from paige.domain.card import Action, ActionEvent, Card
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Attachment, AttachmentKind, Inbound
from paige.domain.outbound import (
    CardContent,
    Outbound,
    TextContent,
    TypingContent,
)
from paige.domain.person import Person
from paige.ports.channel import Channel
from paige.testing.fakes import FakeChannel

CONV = Conversation(chat_id="-100", thread_id="42")
ALICE = Person(user_id="u1", display_name="Alice")


def test_satisfies_channel_protocol() -> None:
    assert isinstance(FakeChannel(), Channel)


async def test_start_stop_flags() -> None:
    ch = FakeChannel()
    await ch.start()
    await ch.stop()
    assert ch.started and ch.stopped


async def test_send_text_returns_unique_anchors() -> None:
    ch = FakeChannel()
    a1 = await ch.send(Outbound(conversation=CONV, content=TextContent("hi")))
    a2 = await ch.send(Outbound(conversation=CONV, content=TextContent("there")))
    assert a1 is not None and a2 is not None
    assert a1.message_id != a2.message_id
    assert len(ch.sent) == 2


async def test_send_typing_returns_none() -> None:
    ch = FakeChannel()
    anchor = await ch.send(Outbound(conversation=CONV, content=TypingContent()))
    assert anchor is None
    assert len(ch.sent) == 1


async def test_edit_records_anchor_outbound_pairs() -> None:
    ch = FakeChannel()
    anchor = Anchor(conversation=CONV, message_id="9")
    out = Outbound(conversation=CONV, content=TextContent("new"))
    result = await ch.edit(anchor, out)
    assert result is None
    assert ch.edits == [(anchor, out)]


async def test_edit_returns_once_simulates_cross_type_fallback() -> None:
    ch = FakeChannel()
    new_anchor = Anchor(conversation=CONV, message_id="999")
    ch.edit_returns_once(new_anchor)
    result = await ch.edit(
        Anchor(conversation=CONV, message_id="1"),
        Outbound(conversation=CONV, content=TextContent("x")),
    )
    assert result is new_anchor
    # next edit returns None again (one-shot).
    result2 = await ch.edit(
        Anchor(conversation=CONV, message_id="1"),
        Outbound(conversation=CONV, content=TextContent("y")),
    )
    assert result2 is None


async def test_delete_records() -> None:
    ch = FakeChannel()
    anchor = Anchor(conversation=CONV, message_id="42")
    await ch.delete(anchor)
    assert ch.deleted == [anchor]


async def test_probe_returns_false_for_dead_chats() -> None:
    ch = FakeChannel()
    ch.dead_chats.add(CONV)
    assert await ch.probe(CONV) is False
    other = Conversation(chat_id="-100", thread_id="other")
    assert await ch.probe(other) is True


async def test_download_returns_seeded_bytes() -> None:
    ch = FakeChannel()
    ch.download_data = b"hello"
    att = Attachment(kind=AttachmentKind.IMAGE, fetch_id="f-1")
    assert await ch.download(att) == b"hello"
    assert ch.downloaded == [att]


async def test_ack_records() -> None:
    ch = FakeChannel()
    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="9"),
        action_id="ok",
        value={},
        ack_token="t-1",
    )
    await ch.ack(event, text="done")
    assert ch.acks == [(event, "done")]


async def test_fail_send_once_raises_then_clears() -> None:
    ch = FakeChannel()
    ch.fail_send_once(RuntimeError("boom"))
    try:
        await ch.send(Outbound(conversation=CONV, content=TextContent("a")))
    except RuntimeError as e:
        assert str(e) == "boom"
    # Second call succeeds
    a = await ch.send(Outbound(conversation=CONV, content=TextContent("b")))
    assert a is not None


async def test_deliver_inbound_invokes_registered_handlers() -> None:
    ch = FakeChannel()
    received: list[Inbound] = []

    async def handler(inb: Inbound) -> None:
        received.append(inb)

    ch.on_inbound(handler)
    inbound = Inbound(
        sender=ALICE,
        conversation=CONV,
        text="hi",
        message_id="1",
    )
    await ch.deliver_inbound(inbound)
    assert received == [inbound]


async def test_deliver_command_dispatches_by_name() -> None:
    ch = FakeChannel()
    seen: list[tuple[str, str]] = []

    async def help_handler(_inb: Inbound, arg: str) -> None:
        seen.append(("help", arg))

    ch.on_command("help", help_handler)
    inbound = Inbound(
        sender=ALICE,
        conversation=CONV,
        text="/help me",
        message_id="1",
    )
    await ch.deliver_command("help", inbound, "me")
    assert seen == [("help", "me")]


async def test_deliver_action_invokes_handlers() -> None:
    ch = FakeChannel()
    received: list[ActionEvent] = []

    async def handler(ev: ActionEvent) -> None:
        received.append(ev)

    ch.on_action(handler)
    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="9"),
        action_id="x",
        value={"k": "v"},
        ack_token="t",
    )
    await ch.deliver_action(event)
    assert received == [event]


async def test_send_card_records_full_outbound() -> None:
    ch = FakeChannel()
    card = Card(
        text="Pick:",
        rows=((Action(label="Yes", action_id="y"),),),
    )
    await ch.send(Outbound(conversation=CONV, content=CardContent(card=card)))
    assert isinstance(ch.sent[0].content, CardContent)
    assert ch.sent[0].content.card.text == "Pick:"
