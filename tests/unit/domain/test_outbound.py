"""Outbound + content union."""

from paige.domain.card import Action, Card
from paige.domain.conversation import Anchor, Conversation
from paige.domain.outbound import (
    CardContent,
    DocumentContent,
    Outbound,
    TextContent,
    TypingContent,
)


def _conv() -> Conversation:
    return Conversation(chat_id="oc", thread_id="om")


def test_text_outbound() -> None:
    out = Outbound(
        conversation=_conv(),
        content=TextContent(text="hello"),
    )
    assert isinstance(out.content, TextContent)
    assert out.content.text == "hello"
    assert out.reply_to is None


def test_card_outbound_with_reply_to() -> None:
    conv = _conv()
    anchor = Anchor(conversation=conv, message_id="om_prev")
    card = Card(text="pick", rows=((Action("ok", "cb:ok"),),))
    out = Outbound(
        conversation=conv,
        content=CardContent(card=card),
        reply_to=anchor,
    )
    assert isinstance(out.content, CardContent)
    assert out.content.card.text == "pick"
    assert out.reply_to is anchor


def test_document_outbound_image_with_keyboard() -> None:
    out = Outbound(
        conversation=_conv(),
        content=DocumentContent(
            data=b"\x89PNG...",
            filename="screenshot.png",
            as_image=True,
            rows=((Action("🔄", "cb:refresh"),),),
        ),
    )
    assert isinstance(out.content, DocumentContent)
    assert out.content.as_image is True
    assert len(out.content.rows) == 1


def test_typing_indicator_has_no_body() -> None:
    out = Outbound(conversation=_conv(), content=TypingContent())
    assert isinstance(out.content, TypingContent)
