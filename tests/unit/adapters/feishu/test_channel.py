"""FeishuChannel — outbound + inbound dispatch + probe."""

from __future__ import annotations

import json
from typing import Any

import pytest

from paige.adapters.feishu.channel import FeishuChannel
from paige.adapters.feishu.client import FeishuClient, FeishuResponse
from paige.domain.card import ActionEvent, Card
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Attachment, AttachmentKind, Inbound
from paige.domain.outbound import (
    CardContent,
    DocumentContent,
    Outbound,
    TextContent,
    TypingContent,
)
from paige.domain.person import Person
from paige.ports.channel import Channel
from paige.testing.fake_feishu import FakeFeishuClient

ALICE = Person(user_id="ou_alice", display_name="Alice")
CONV = Conversation(chat_id="oc_chat", thread_id="om_root")


def _msg_event(
    *, text: str = "hi", sender: str = "ou_alice", root: str | None = "om_root"
) -> dict[str, Any]:
    return {
        "message": {
            "message_id": "om_inbound",
            "root_id": root,
            "chat_id": "oc_chat",
            "chat_type": "group",
            "message_type": "text",
            "content": json.dumps({"text": text}),
            "create_time": "1700000000000",
        },
        "sender": {
            "sender_id": {"open_id": sender},
            "name": "Sender Name",
        },
    }


# ── Protocol satisfaction ────────────────────────────────────────


def test_satisfies_channel_protocol() -> None:
    channel = FeishuChannel(client=FakeFeishuClient())
    assert isinstance(channel, Channel)


def test_fake_satisfies_feishu_client_protocol() -> None:
    assert isinstance(FakeFeishuClient(), FeishuClient)


# ── lifecycle ────────────────────────────────────────────────────


async def test_start_stop_propagate_to_client() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    await channel.start()
    assert client.started is True
    await channel.stop()
    assert client.stopped is True


# ── send: TextContent ────────────────────────────────────────────


async def test_send_text_creates_post_with_thread_anchor() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    out = Outbound(conversation=CONV, content=TextContent("hello"))
    anchor = await channel.send(out)
    assert anchor is not None
    assert anchor.message_id.startswith("om_fake_")
    [call] = client.created
    assert call.chat_id == "oc_chat"
    # Thread anchor pulls from conversation.thread_id when no
    # explicit reply_to.
    assert call.reply_to_message_id == "om_root"
    # Post envelope was rendered.
    assert "zh_cn" in call.post_content


async def test_send_text_explicit_reply_to_overrides_thread() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    quoted = Anchor(conversation=CONV, message_id="om_quoted")
    out = Outbound(conversation=CONV, content=TextContent("hi"), reply_to=quoted)
    await channel.send(out)
    [call] = client.created
    assert call.reply_to_message_id == "om_quoted"


async def test_send_text_no_thread_no_reply_to_anchors_to_none() -> None:
    """A bare conversation (no thread_id) sends without a reply
    anchor — the message becomes the chain root itself."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    bare = Conversation(chat_id="oc_chat")  # thread_id=None
    out = Outbound(conversation=bare, content=TextContent("hi"))
    await channel.send(out)
    [call] = client.created
    assert call.reply_to_message_id is None


async def test_send_text_failure_returns_none() -> None:
    client = FakeFeishuClient()
    client.code_next["create_text_message"] = 99991400  # rate-limit
    channel = FeishuChannel(client=client)
    anchor = await channel.send(Outbound(conversation=CONV, content=TextContent("x")))
    assert anchor is None


async def test_send_card_failure_posts_error_card() -> None:
    """A rejected card send must not vanish silently — the channel
    posts a red error card carrying the Feishu code + raw message so
    the failure is visible and debuggable from the chat itself."""
    client = FakeFeishuClient()
    client.code_next["create_card_message"] = 230099  # card-parse failure
    channel = FeishuChannel(client=client)
    anchor = await channel.send(
        Outbound(conversation=CONV, content=CardContent(card=Card(text="hi")))
    )
    assert anchor is None
    # Two card calls: the original (failed) + the error notification.
    assert len(client.created_cards) == 2
    err = client.created_cards[1].card_content
    assert "Delivery failed" in err["header"]["title"]["content"]
    # The Feishu code rides in the body for debugging.
    assert "230099" in json.dumps(err)


async def test_error_card_failure_does_not_recurse() -> None:
    """If the error card *also* fails to send we only log — it must
    not trigger another error card (the notifier sends directly, not
    through `_anchor_from_response`), so no infinite recursion."""

    class AlwaysFailCards(FakeFeishuClient):
        async def create_card_message(self, **kw: Any) -> FeishuResponse:
            await super().create_card_message(**kw)  # record the call
            return FeishuResponse(code=230099, msg="boom", data={})

    client = AlwaysFailCards()
    channel = FeishuChannel(client=client)
    await channel.send(Outbound(conversation=CONV, content=CardContent(card=Card(text="hi"))))
    # Exactly two: the original failed send + one error card. The
    # error card's own failure stops at the log — no third attempt.
    assert len(client.created_cards) == 2


async def test_send_typing_returns_none_no_call() -> None:
    """Feishu has no typing-indicator API; TypingContent is a no-op."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    anchor = await channel.send(Outbound(conversation=CONV, content=TypingContent()))
    assert anchor is None
    assert client.created == []


# ── send: DocumentContent (images) ───────────────────────────────


async def test_send_image_uploads_then_creates_card() -> None:
    """Feishu image messages can't carry buttons, so a
    DocumentContent(as_image=True, ...) round-trips through
    upload_image → create_card_message with an `img + action`
    card."""
    from paige.domain.card import Action

    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    rows = ((Action(label="↑", action_id="ss:key", value={"k": "up"}),),)
    out = Outbound(
        conversation=CONV,
        content=DocumentContent(
            data=b"PNGBYTES",
            filename="screenshot.png",
            as_image=True,
            rows=rows,
        ),
    )

    anchor = await channel.send(out)

    assert anchor is not None
    assert anchor.message_id.startswith("om_fake_")
    [upload] = client.uploaded_images
    assert upload.image_data == b"PNGBYTES"
    [card_call] = client.created_cards
    assert card_call.chat_id == "oc_chat"
    assert card_call.reply_to_message_id == "om_root"
    elements = card_call.card_content["body"]["elements"]
    img = elements[0]
    assert img["tag"] == "img"
    assert img["img_key"].startswith("img_fake_")
    # Button row rides as a column_set sibling, one column per button.
    assert elements[1]["tag"] == "column_set"
    [column] = elements[1]["columns"]
    [button] = column["elements"]
    assert button["text"]["content"] == "↑"
    # No text was sent — DocumentContent → card path doesn't go
    # through the text path.
    assert client.created == []


async def test_send_image_upload_failure_returns_none_no_card() -> None:
    client = FakeFeishuClient()
    client.code_next["upload_image"] = 99991400  # rate-limit
    channel = FeishuChannel(client=client)
    out = Outbound(
        conversation=CONV,
        content=DocumentContent(data=b"x", filename="x.png", as_image=True),
    )
    anchor = await channel.send(out)
    assert anchor is None
    # We never tried to send a card without a key.
    assert client.created_cards == []


async def test_send_non_image_document_raises_not_implemented() -> None:
    """Plain file uploads (as_image=False) aren't wired yet — paige's
    only DocumentContent producer today is /screenshot. Guarding
    catches accidental new uses without silently dropping bytes."""
    channel = FeishuChannel(client=FakeFeishuClient())
    with pytest.raises(NotImplementedError):
        await channel.send(
            Outbound(
                conversation=CONV,
                content=DocumentContent(data=b"x", filename="x.txt"),
            )
        )


# ── edit ─────────────────────────────────────────────────────────


async def test_edit_text_calls_patch() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    anchor = Anchor(conversation=CONV, message_id="om_target")
    await channel.edit(
        anchor,
        Outbound(conversation=CONV, content=TextContent("updated")),
    )
    [call] = client.patched
    assert call.message_id == "om_target"
    assert "zh_cn" in call.post_content


async def test_edit_text_returns_none_even_on_api_failure() -> None:
    """Failure mode is "log and continue", same as v1's behavior."""
    client = FakeFeishuClient()
    client.code_next["patch_text_message"] = 230099  # cross-type mismatch
    channel = FeishuChannel(client=client)
    result = await channel.edit(
        Anchor(conversation=CONV, message_id="om_x"),
        Outbound(conversation=CONV, content=TextContent("x")),
    )
    assert result is None


async def test_edit_image_out_of_band_uploads_and_patches_card() -> None:
    """Editing a DocumentContent message *outside* a click handler
    (no inline-refresh slot) re-uploads + patches the card."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    out = Outbound(
        conversation=CONV,
        content=DocumentContent(
            data=b"NEWPNG",
            filename="screenshot.png",
            as_image=True,
        ),
    )
    await channel.edit(Anchor(conversation=CONV, message_id="om_card"), out)

    [upload] = client.uploaded_images
    assert upload.image_data == b"NEWPNG"
    [patch] = client.patched_cards
    assert patch.message_id == "om_card"
    assert patch.card_content["body"]["elements"][0]["tag"] == "img"


# ── delete ───────────────────────────────────────────────────────


async def test_delete_calls_client() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    await channel.delete(Anchor(conversation=CONV, message_id="om_target"))
    [call] = client.deleted
    assert call.message_id == "om_target"


# ── probe ────────────────────────────────────────────────────────


async def test_probe_alive_returns_true() -> None:
    channel = FeishuChannel(client=FakeFeishuClient())
    assert await channel.probe(CONV) is True


async def test_probe_disbanded_chat_returns_false() -> None:
    client = FakeFeishuClient()
    client.dead_chats["oc_dead"] = 230002
    channel = FeishuChannel(client=client)
    assert await channel.probe(Conversation(chat_id="oc_dead")) is False


async def test_probe_bot_not_in_chat_returns_false() -> None:
    client = FakeFeishuClient()
    client.dead_chats["oc_chat"] = 230003
    channel = FeishuChannel(client=client)
    assert await channel.probe(CONV) is False


async def test_probe_chat_not_found_returns_false() -> None:
    client = FakeFeishuClient()
    client.dead_chats["oc_chat"] = 230020
    channel = FeishuChannel(client=client)
    assert await channel.probe(CONV) is False


async def test_probe_unknown_error_fails_open() -> None:
    """Transient backend errors must NOT nuke a live binding."""
    client = FakeFeishuClient()
    client.code_next["get_chat"] = 99991400  # rate-limit
    channel = FeishuChannel(client=client)
    assert await channel.probe(CONV) is True


# ── inbound dispatch ─────────────────────────────────────────────


async def test_inbound_text_calls_on_inbound_handler() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    received: list[Inbound] = []

    async def handler(inb: Inbound) -> None:
        received.append(inb)

    channel.on_inbound(handler)
    await client.deliver_message(_msg_event(text="hello"))
    assert len(received) == 1
    assert received[0].text == "hello"


async def test_command_routes_to_command_handler() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    inbound_seen: list[Inbound] = []
    cmd_seen: list[tuple[Inbound, str]] = []

    async def on_inbound(inb: Inbound) -> None:
        inbound_seen.append(inb)

    async def on_help(inb: Inbound, arg: str) -> None:
        cmd_seen.append((inb, arg))

    channel.on_inbound(on_inbound)
    channel.on_command("help", on_help)

    await client.deliver_message(_msg_event(text="/help me"))

    # Routed to command, NOT to on_inbound.
    assert len(cmd_seen) == 1
    assert cmd_seen[0][1] == "me"
    assert inbound_seen == []


async def test_unregistered_command_is_dropped() -> None:
    """A `/cmd` with no registered handler isn't fanned out as
    inbound text — that would pollute the on_inbound stream."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    received: list[Inbound] = []

    async def handler(inb: Inbound) -> None:
        received.append(inb)

    channel.on_inbound(handler)
    await client.deliver_message(_msg_event(text="/unknown"))
    assert received == []


async def test_handler_exception_does_not_break_dispatch() -> None:
    """One handler raising must not stop subsequent handlers."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    seen: list[Inbound] = []

    async def bad(_inb: Inbound) -> None:
        raise RuntimeError("boom")

    async def good(inb: Inbound) -> None:
        seen.append(inb)

    channel.on_inbound(bad)
    channel.on_inbound(good)
    await client.deliver_message(_msg_event(text="hi"))
    assert len(seen) == 1


async def test_non_text_inbound_is_dropped_silently() -> None:
    """Image/post/file events return None from to_inbound and don't
    produce any handler call."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    seen: list[Inbound] = []

    async def handler(inb: Inbound) -> None:
        seen.append(inb)

    channel.on_inbound(handler)
    img_event = _msg_event(text="caption")
    img_event["message"]["message_type"] = "image"
    await client.deliver_message(img_event)
    assert seen == []


# (action dispatch + ack tests live near the end of this file —
#  kept there because they share the _action_event helper.)


# ── download stub ───────────────────────────────────────────────


async def test_download_image_calls_client() -> None:
    client = FakeFeishuClient()
    client.download_bytes_next["img_abc"] = b"PNG-bytes"
    channel = FeishuChannel(client=client)

    att = Attachment(
        kind=AttachmentKind.IMAGE,
        fetch_id="img_abc",
        containing_message_id="om_msg",
    )
    data = await channel.download(att)
    assert data == b"PNG-bytes"

    [call] = client.downloaded
    assert call.message_id == "om_msg"
    assert call.file_key == "img_abc"
    assert call.resource_type == "image"


async def test_download_audio_uses_file_resource_type() -> None:
    """Feishu's resource endpoint takes type=image only for images;
    audio + file both go through type=file."""
    client = FakeFeishuClient()
    client.download_bytes_next["file_voice"] = b"opus-bytes"
    channel = FeishuChannel(client=client)

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        fetch_id="file_voice",
        containing_message_id="om_msg",
    )
    data = await channel.download(att)
    assert data == b"opus-bytes"
    [call] = client.downloaded
    assert call.resource_type == "file"


async def test_download_without_message_id_raises() -> None:
    """Feishu's endpoint requires the source message id; without
    it we can't fetch."""
    channel = FeishuChannel(client=FakeFeishuClient())
    bare_att = Attachment(kind=AttachmentKind.IMAGE, fetch_id="img_x")
    with pytest.raises(ValueError, match="containing_message_id"):
        await channel.download(bare_att)


async def test_download_propagates_client_error() -> None:
    client = FakeFeishuClient()
    client.fail_next["download_resource"] = RuntimeError("network down")
    channel = FeishuChannel(client=client)

    att = Attachment(
        kind=AttachmentKind.IMAGE,
        fetch_id="img_abc",
        containing_message_id="om_msg",
    )
    with pytest.raises(RuntimeError, match="network down"):
        await channel.download(att)


# ── send/edit: CardContent ──────────────────────────────────────


async def test_send_card_calls_create_card_message() -> None:
    from paige.domain.card import Action, Card

    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    card = Card(
        text="Pick:",
        rows=((Action(label="Yes", action_id="y"),),),
    )
    anchor = await channel.send(Outbound(conversation=CONV, content=CardContent(card=card)))
    assert anchor is not None
    [call] = client.created_cards
    assert call.chat_id == "oc_chat"
    assert call.reply_to_message_id == "om_root"
    assert "elements" in call.card_content["body"]
    # No text-message create.
    assert client.created == []


async def test_edit_card_calls_patch_card_message() -> None:
    from paige.domain.card import Card

    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    anchor = Anchor(conversation=CONV, message_id="om_card")
    await channel.edit(
        anchor,
        Outbound(conversation=CONV, content=CardContent(card=Card(text="updated"))),
    )
    [call] = client.patched_cards
    assert call.message_id == "om_card"
    assert "elements" in call.card_content["body"]
    # No text-patch.
    assert client.patched == []


# ── action dispatch ─────────────────────────────────────────────


def _action_event(
    *,
    open_id: str = "ou_alice",
    user_name: str = "Alice",
    chat_id: str = "oc_chat",
    thread_id: str | None = "om_root",
    message_id: str = "om_card",
    action_id: str = "ses:bind",
    value: dict[str, Any] | None = None,
    token: str = "tg-token",
) -> dict[str, Any]:
    return {
        "operator": {"open_id": open_id, "user_name": user_name},
        "action": {
            "tag": "button",
            "value": {"action_id": action_id, **(value or {})},
        },
        "context": {
            "open_message_id": message_id,
            "open_chat_id": chat_id,
            "thread_id": thread_id,
        },
        "token": token,
    }


async def test_action_event_routed_to_handler() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    seen: list[ActionEvent] = []

    async def handler(ev: ActionEvent) -> None:
        seen.append(ev)

    channel.on_action(handler)
    await client.deliver_action(_action_event(action_id="ses:bind", value={"pane_id": "@7"}))
    assert len(seen) == 1
    ev = seen[0]
    assert ev.sender.user_id == "ou_alice"
    assert ev.sender.display_name == "Alice"
    assert ev.action_id == "ses:bind"
    assert ev.value == {"pane_id": "@7"}
    assert ev.card_anchor.message_id == "om_card"
    assert ev.conversation.chat_id == "oc_chat"
    assert ev.conversation.thread_id == "om_root"


async def test_action_handler_exception_does_not_break_dispatch() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    saw_good: list[ActionEvent] = []

    async def bad(_ev: ActionEvent) -> None:
        raise RuntimeError("boom")

    async def good(ev: ActionEvent) -> None:
        saw_good.append(ev)

    channel.on_action(bad)
    channel.on_action(good)
    await client.deliver_action(_action_event())
    assert len(saw_good) == 1


async def test_malformed_action_event_dropped_silently() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)
    seen: list[ActionEvent] = []

    async def handler(ev: ActionEvent) -> None:
        seen.append(ev)

    channel.on_action(handler)
    # Missing context → to_action_event returns None.
    bad = {"operator": {"open_id": "ou_x"}, "action": {"value": {"action_id": "x"}}}
    await client.deliver_action(bad)
    assert seen == []


# ── ack: response toast ─────────────────────────────────────────


async def test_ack_populates_response_toast() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    async def handler(ev: ActionEvent) -> None:
        await channel.ack(ev, "Bound!")

    channel.on_action(handler)
    [response] = await client.deliver_action(_action_event(token="t1"))
    assert response is not None
    assert response["toast"] == {"type": "info", "content": "Bound!"}


async def test_no_ack_means_no_toast_in_response() -> None:
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    async def handler(_ev: ActionEvent) -> None:
        pass  # no ack

    channel.on_action(handler)
    [response] = await client.deliver_action(_action_event())
    assert response is None


async def test_inline_refresh_when_editing_clicked_card() -> None:
    """When a click handler edits the clicked card, the new card
    rides the click response. patch_card_message is NOT called —
    Feishu's PATCH repaint on the clicker is unreliable; the
    response-body shape is atomic."""
    from paige.domain.card import Card

    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    new_card = Card(text="updated body")

    async def handler(ev: ActionEvent) -> None:
        await channel.edit(
            ev.card_anchor,
            Outbound(conversation=CONV, content=CardContent(card=new_card)),
        )

    channel.on_action(handler)
    [response] = await client.deliver_action(_action_event(token="t1"))
    assert response is not None
    assert response["card"]["type"] == "raw"
    assert response["card"]["data"]["body"]["elements"][0]["content"] == "updated body"
    # Crucially: the channel did NOT PATCH out-of-band.
    assert client.patched_cards == []


async def test_inline_refresh_with_document_content() -> None:
    """/screenshot 🔄 Refresh: the click handler edits the clicked
    card with a fresh DocumentContent. The new image_key rides the
    click response (no out-of-band patch), and the swap lands
    atomically with the click ack."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    async def handler(ev: ActionEvent) -> None:
        await channel.edit(
            ev.card_anchor,
            Outbound(
                conversation=CONV,
                content=DocumentContent(
                    data=b"REFRESHED",
                    filename="screenshot.png",
                    as_image=True,
                ),
            ),
        )

    channel.on_action(handler)
    [response] = await client.deliver_action(_action_event(token="t1"))
    assert response is not None
    assert response["card"]["type"] == "raw"
    assert response["card"]["data"]["body"]["elements"][0]["tag"] == "img"
    assert response["card"]["data"]["body"]["elements"][0]["img_key"].startswith("img_fake_")
    [upload] = client.uploaded_images
    assert upload.image_data == b"REFRESHED"
    # No PATCH out-of-band — the response body is the swap.
    assert client.patched_cards == []


async def test_inline_refresh_combines_with_ack_toast() -> None:
    from paige.domain.card import Card

    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    async def handler(ev: ActionEvent) -> None:
        await channel.ack(ev, "Bound!")
        await channel.edit(
            ev.card_anchor,
            Outbound(
                conversation=CONV,
                content=CardContent(card=Card(text="✓ done")),
            ),
        )

    channel.on_action(handler)
    [response] = await client.deliver_action(_action_event())
    assert response is not None
    assert response["toast"]["content"] == "Bound!"
    assert response["card"]["type"] == "raw"
    assert response["card"]["data"]["body"]["elements"][0]["content"] == "✓ done"


async def test_edit_to_other_card_during_click_uses_patch() -> None:
    """If the click handler edits a card OTHER than the clicked
    one, that edit goes through the normal patch path — only the
    clicked card's edit gets folded into the response."""
    from paige.domain.card import Card

    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    other_anchor = Anchor(conversation=CONV, message_id="om_other_card")

    async def handler(_ev: ActionEvent) -> None:
        await channel.edit(
            other_anchor,
            Outbound(
                conversation=CONV,
                content=CardContent(card=Card(text="other update")),
            ),
        )

    channel.on_action(handler)
    [response] = await client.deliver_action(_action_event())
    # No clicked-card refresh, no toast → no response body.
    assert response is None
    # The other-card edit went through patch.
    [call] = client.patched_cards
    assert call.message_id == "om_other_card"


async def test_edit_outside_click_uses_patch() -> None:
    """Outside of a click handler (the normal case), card edits
    always go through patch_card_message."""
    from paige.domain.card import Card

    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    anchor = Anchor(conversation=CONV, message_id="om_card")
    await channel.edit(
        anchor,
        Outbound(conversation=CONV, content=CardContent(card=Card(text="t"))),
    )
    [call] = client.patched_cards
    assert call.message_id == "om_card"


async def test_ack_consumed_per_token() -> None:
    """Two simultaneous clicks (different tokens) shouldn't bleed
    each other's toast."""
    client = FakeFeishuClient()
    channel = FeishuChannel(client=client)

    async def handler(ev: ActionEvent) -> None:
        if ev.ack_token == "t1":
            await channel.ack(ev, "First!")

    channel.on_action(handler)
    [r1] = await client.deliver_action(_action_event(token="t1"))
    [r2] = await client.deliver_action(_action_event(token="t2"))
    assert r1 is not None and r1["toast"]["content"] == "First!"
    assert r2 is None


# ── group-mention filter ────────────────────────────────────────


def _group_event(*, mentions: list[str], chat_type: str = "group") -> dict[str, Any]:
    return {
        "message": {
            "message_id": "om_inbound",
            "root_id": None,
            "chat_id": "oc_chat",
            "chat_type": chat_type,
            "message_type": "text",
            "content": json.dumps({"text": "hi"}),
            "create_time": "1700000000000",
            "mentions": mentions,
        },
        "sender": {
            "sender_id": {"open_id": "ou_alice"},
            "name": "Alice",
        },
    }


async def test_start_fetches_bot_open_id() -> None:
    client = FakeFeishuClient()
    client.bot_open_id_next = "ou_paige_bot"
    channel = FeishuChannel(client=client)

    await channel.start()

    assert client.bot_info_calls == 1


async def test_group_message_without_mention_is_dropped() -> None:
    client = FakeFeishuClient()
    client.bot_open_id_next = "ou_paige_bot"
    channel = FeishuChannel(client=client)
    received: list[Inbound] = []

    async def handler(inbound: Inbound) -> None:
        received.append(inbound)

    channel.on_inbound(handler)
    await channel.start()

    await client.deliver_message(_group_event(mentions=["ou_someone_else"]))

    assert received == []


async def test_group_message_with_bot_mention_passes_through() -> None:
    client = FakeFeishuClient()
    client.bot_open_id_next = "ou_paige_bot"
    channel = FeishuChannel(client=client)
    received: list[Inbound] = []

    async def handler(inbound: Inbound) -> None:
        received.append(inbound)

    channel.on_inbound(handler)
    await channel.start()

    await client.deliver_message(_group_event(mentions=["ou_paige_bot", "ou_alice"]))

    assert len(received) == 1


async def test_p2p_message_always_passes_through() -> None:
    """DMs (chat_type=p2p) shouldn't be subject to the mention filter
    — every message in a DM is for the bot."""
    client = FakeFeishuClient()
    client.bot_open_id_next = "ou_paige_bot"
    channel = FeishuChannel(client=client)
    received: list[Inbound] = []

    async def handler(inbound: Inbound) -> None:
        received.append(inbound)

    channel.on_inbound(handler)
    await channel.start()

    await client.deliver_message(_group_event(mentions=[], chat_type="p2p"))

    assert len(received) == 1


async def test_filter_fails_open_when_bot_open_id_unknown() -> None:
    """If get_bot_info returned nothing (missing scope, etc.), the
    filter shouldn't suppress group messages — better to over-respond
    than to go silent in a configured chat."""
    client = FakeFeishuClient()
    # Don't seed bot_open_id_next → channel can't resolve its identity.
    channel = FeishuChannel(client=client)
    received: list[Inbound] = []

    async def handler(inbound: Inbound) -> None:
        received.append(inbound)

    channel.on_inbound(handler)
    await channel.start()

    await client.deliver_message(_group_event(mentions=["ou_someone_else"]))

    assert len(received) == 1


async def test_start_survives_get_bot_info_exception() -> None:
    client = FakeFeishuClient()
    client.fail_next["get_bot_info"] = RuntimeError("boom")
    channel = FeishuChannel(client=client)

    # Shouldn't raise — fail-open is the contract.
    await channel.start()

    received: list[Inbound] = []

    async def handler(inbound: Inbound) -> None:
        received.append(inbound)

    channel.on_inbound(handler)
    await client.deliver_message(_group_event(mentions=[]))
    assert len(received) == 1


async def test_start_survives_get_bot_info_error_code() -> None:
    """Non-zero `code` from get_bot_info (e.g. missing scope) should
    log a warning and continue with the filter disabled."""
    client = FakeFeishuClient()
    client.code_next["get_bot_info"] = 99991672  # arbitrary non-zero
    channel = FeishuChannel(client=client)

    await channel.start()

    received: list[Inbound] = []

    async def handler(inbound: Inbound) -> None:
        received.append(inbound)

    channel.on_inbound(handler)
    await client.deliver_message(_group_event(mentions=[]))
    assert len(received) == 1
