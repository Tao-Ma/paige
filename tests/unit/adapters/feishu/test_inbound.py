"""Feishu inbound converter — pure event dict → Inbound."""

from __future__ import annotations

import json
from typing import Any

from paige.adapters.feishu.inbound import (
    split_command,
    to_action_event,
    to_inbound,
)


def _msg_event(
    *,
    text: str = "hello",
    chat_id: str = "oc_chat",
    chat_type: str = "group",
    message_id: str = "om_msg",
    root_id: str | None = None,
    thread_id: str | None = None,
    sender_open_id: str = "ou_alice",
    sender_name: str = "Alice",
    message_type: str = "text",
    content_raw: str | None = None,
    create_time: str = "1700000000000",
) -> dict[str, Any]:
    """Build a normalized Feishu message event for tests.

    `content_raw`, when provided, overrides the auto-generated
    `{"text": text}` content (used to test image/audio/file shapes
    that have different content payloads).

    `thread_id` (when set) populates `message.thread_id` — Lark's
    `omt_xxx` topic identifier in topic-mode groups.
    """
    content = content_raw if content_raw is not None else json.dumps({"text": text})
    message: dict[str, Any] = {
        "message_id": message_id,
        "root_id": root_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content,
        "create_time": create_time,
    }
    if thread_id is not None:
        message["thread_id"] = thread_id
    return {
        "message": message,
        "sender": {
            "sender_id": {"open_id": sender_open_id},
            "sender_type": "user",
            "name": sender_name,
        },
    }


# ── happy path ───────────────────────────────────────────────────


def test_basic_text_event() -> None:
    inb = to_inbound(_msg_event())
    assert inb is not None
    assert inb.text == "hello"
    assert inb.sender.user_id == "ou_alice"
    assert inb.sender.display_name == "Alice"
    assert inb.conversation.chat_id == "oc_chat"
    assert inb.message_id == "om_msg"


def test_thread_id_uses_root_id_when_present() -> None:
    inb = to_inbound(_msg_event(root_id="om_root"))
    assert inb is not None
    assert inb.conversation.thread_id == "om_root"


def test_thread_id_falls_back_to_message_id_when_no_root() -> None:
    """First message in a chain has no root; future replies will
    target this message's id as their root."""
    inb = to_inbound(_msg_event(root_id=None, message_id="om_first"))
    assert inb is not None
    assert inb.conversation.thread_id == "om_first"


def test_action_event_thread_id_round_trips_via_button_value() -> None:
    """A click event whose button.value carries `_thread_id` reports
    that as the conversation's thread_id, regardless of what
    `context.thread_id` says. The card renderer injects this so a
    click in p2p always reports the chain root the card was sent
    under (Feishu otherwise sets context.thread_id=None in p2p)."""
    event = {
        "operator": {"open_id": "ou_x", "user_name": "X"},
        "action": {
            "tag": "button",
            "value": {
                "action_id": "ses:bind",
                "_thread_id": "om_chain_root",
                "pane_id": "@7",
            },
        },
        "context": {
            "open_message_id": "om_card",
            "open_chat_id": "oc_chat",
            "thread_id": None,
        },
        "token": "t",
    }
    ev = to_action_event(event)
    assert ev is not None
    assert ev.conversation.thread_id == "om_chain_root"
    # `_thread_id` is consumed; downstream handlers see only domain keys.
    assert "_thread_id" not in ev.value
    assert ev.value == {"pane_id": "@7"}


def test_action_event_falls_back_to_context_thread_id_when_value_missing() -> None:
    """Cards rendered before the round-trip key was added still
    arrive on clicks; fall through to `context.thread_id` so the
    legacy path keeps working in groups (where Feishu populates it)."""
    event = {
        "operator": {"open_id": "ou_x", "user_name": "X"},
        "action": {
            "tag": "button",
            "value": {"action_id": "ses:bind", "pane_id": "@7"},
        },
        "context": {
            "open_message_id": "om_card",
            "open_chat_id": "oc_chat",
            "thread_id": "om_legacy_thread",
        },
        "token": "t",
    }
    ev = to_action_event(event)
    assert ev is not None
    assert ev.conversation.thread_id == "om_legacy_thread"


def test_topic_id_extracted_from_message_thread_id() -> None:
    """In topic-mode groups, the message event carries Lark's
    `omt_xxx` identifier under `message.thread_id`. paige stores it
    on `Conversation.topic_id` alongside the reply-chain `thread_id`
    so bindings can be scoped per Lark topic."""
    inb = to_inbound(_msg_event(thread_id="omt_topic_xyz", root_id="om_topic_root"))
    assert inb is not None
    assert inb.conversation.topic_id == "omt_topic_xyz"
    assert inb.conversation.thread_id == "om_topic_root"


def test_topic_id_absent_outside_topic_mode_groups() -> None:
    """Regular chats deliver no `message.thread_id`; `topic_id`
    stays None and bindings continue to key on chain root only."""
    inb = to_inbound(_msg_event(root_id="om_root"))
    assert inb is not None
    assert inb.conversation.topic_id is None


def test_topic_id_only_accepts_omt_prefix() -> None:
    """If something else lands in `message.thread_id` (older
    payloads, defensive parsing), don't promote it to `topic_id` —
    the prefix is the only positive signal."""
    inb = to_inbound(_msg_event(thread_id="om_not_a_topic"))
    assert inb is not None
    assert inb.conversation.topic_id is None


def test_action_event_topic_id_round_trips_via_button_value() -> None:
    """Cards rendered inside a topic-mode group inject `_topic_id`
    into every button value. The click event recovers it so the
    binding lookup keys on the right topic even if `context.thread_id`
    is missing/empty."""
    event = {
        "operator": {"open_id": "ou_x", "user_name": "X"},
        "action": {
            "tag": "button",
            "value": {
                "action_id": "ses:bind",
                "_thread_id": "om_topic_root",
                "_topic_id": "omt_topic_xyz",
                "pane_id": "@7",
            },
        },
        "context": {
            "open_message_id": "om_card",
            "open_chat_id": "oc_chat",
            "thread_id": None,
        },
        "token": "t",
    }
    ev = to_action_event(event)
    assert ev is not None
    assert ev.conversation.thread_id == "om_topic_root"
    assert ev.conversation.topic_id == "omt_topic_xyz"
    # round-trip keys are consumed; downstream handlers see only domain keys.
    assert "_topic_id" not in ev.value
    assert "_thread_id" not in ev.value
    assert ev.value == {"pane_id": "@7"}


def test_action_event_promotes_context_thread_id_to_topic_when_omt_prefix() -> None:
    """No round-trip key present? Disambiguate by prefix:
    `omt_xxx` → topic_id; anything else → thread_id."""
    event = {
        "operator": {"open_id": "ou_x", "user_name": "X"},
        "action": {
            "tag": "button",
            "value": {"action_id": "ses:bind"},
        },
        "context": {
            "open_message_id": "om_card",
            "open_chat_id": "oc_chat",
            "thread_id": "omt_topic_xyz",
        },
        "token": "t",
    }
    ev = to_action_event(event)
    assert ev is not None
    assert ev.conversation.topic_id == "omt_topic_xyz"
    assert ev.conversation.thread_id is None


def test_thread_id_handling_is_uniform_across_chat_types() -> None:
    """Both p2p and group chats use the same rule: thread_id is the
    chain root (root_id when set, else the message's own id). The
    chain root model is what makes paige's outgoing replies form a
    Feishu reply-thread (合并/topic display). Click events round-trip
    thread_id through the button's value rather than relying on
    Feishu's `context.thread_id`, which is unreliable in p2p."""
    p2p = to_inbound(_msg_event(chat_type="p2p", root_id=None, message_id="om_x"))
    grp = to_inbound(_msg_event(chat_type="group", root_id=None, message_id="om_x"))
    assert p2p is not None and grp is not None
    assert p2p.conversation.thread_id == "om_x"
    assert grp.conversation.thread_id == "om_x"


def test_create_time_parses_to_int_ms() -> None:
    inb = to_inbound(_msg_event(create_time="1700000000123"))
    assert inb is not None
    assert inb.timestamp_ms == 1700000000123


def test_blank_create_time_is_zero() -> None:
    event = _msg_event()
    event["message"]["create_time"] = "abc"
    inb = to_inbound(event)
    assert inb is not None
    assert inb.timestamp_ms == 0


# ── rejection paths ─────────────────────────────────────────────


def test_no_message_returns_none() -> None:
    assert to_inbound({"sender": {"sender_id": {"open_id": "ou_x"}}}) is None


def test_no_sender_returns_none() -> None:
    assert to_inbound({"message": {"message_type": "text"}}) is None


def test_missing_open_id_returns_none() -> None:
    event = _msg_event()
    event["sender"] = {"sender_id": {}}
    assert to_inbound(event) is None


def test_unsupported_message_type_returns_none() -> None:
    """sticker / video / merge_forward types route to nothing yet."""
    assert to_inbound(_msg_event(message_type="sticker")) is None


# ── attachments ──────────────────────────────────────────────────


def test_image_inbound_emits_attachment() -> None:
    from paige.domain.inbound import AttachmentKind

    inb = to_inbound(
        _msg_event(
            message_type="image",
            content_raw=json.dumps({"image_key": "img_abc"}),
        )
    )
    assert inb is not None
    assert inb.text == ""
    [att] = inb.attachments
    assert att.kind is AttachmentKind.IMAGE
    assert att.fetch_id == "img_abc"
    assert att.containing_message_id == "om_msg"


def test_image_with_missing_image_key_returns_none() -> None:
    inb = to_inbound(
        _msg_event(
            message_type="image",
            content_raw=json.dumps({"width": 100}),
        )
    )
    assert inb is None


def test_audio_inbound_emits_attachment_with_duration() -> None:
    """Feishu audio content includes `duration` in milliseconds;
    paige's Attachment.duration_sec converts to seconds."""
    from paige.domain.inbound import AttachmentKind

    inb = to_inbound(
        _msg_event(
            message_type="audio",
            content_raw=json.dumps({"file_key": "file_voice", "duration": 12500}),
        )
    )
    assert inb is not None
    [att] = inb.attachments
    assert att.kind is AttachmentKind.AUDIO
    assert att.fetch_id == "file_voice"
    assert att.duration_sec == 12.5


def test_file_inbound_emits_attachment() -> None:
    from paige.domain.inbound import AttachmentKind

    inb = to_inbound(
        _msg_event(
            message_type="file",
            content_raw=json.dumps({"file_key": "file_doc", "file_name": "report.pdf"}),
        )
    )
    assert inb is not None
    [att] = inb.attachments
    assert att.kind is AttachmentKind.FILE
    assert att.fetch_id == "file_doc"


def test_audio_without_duration_defaults_to_zero() -> None:
    inb = to_inbound(
        _msg_event(
            message_type="audio",
            content_raw=json.dumps({"file_key": "file_voice"}),
        )
    )
    assert inb is not None
    [att] = inb.attachments
    assert att.duration_sec == 0.0


def test_file_with_missing_key_returns_none() -> None:
    inb = to_inbound(
        _msg_event(
            message_type="file",
            content_raw=json.dumps({"file_name": "report.pdf"}),
        )
    )
    assert inb is None


def test_post_inbound_flattens_to_text() -> None:
    """Feishu post messages have a structured tree; we flatten to
    plain text so the dispatcher can route it like a text message."""
    post_content = json.dumps(
        {
            "zh_cn": {
                "title": "",
                "content": [
                    [
                        {"tag": "text", "text": "hello "},
                        {"tag": "a", "text": "world", "href": "http://x"},
                    ],
                    [{"tag": "text", "text": "second line"}],
                ],
            }
        }
    )
    inb = to_inbound(_msg_event(message_type="post", content_raw=post_content))
    assert inb is not None
    assert inb.text == "hello world\nsecond line"
    assert inb.attachments == ()


def test_post_inbound_with_empty_content_returns_none() -> None:
    inb = to_inbound(
        _msg_event(
            message_type="post",
            content_raw=json.dumps({"zh_cn": {"content": [[]]}}),
        )
    )
    assert inb is None


def test_malformed_content_returns_none() -> None:
    event = _msg_event()
    event["message"]["content"] = "{not valid json"
    assert to_inbound(event) is None


def test_empty_chat_id_returns_none() -> None:
    event = _msg_event(chat_id="")
    assert to_inbound(event) is None


def test_top_level_open_id_fallback() -> None:
    """Some shapes put `open_id` flat on the sender instead of nested
    inside `sender_id`. Both work."""
    event = _msg_event()
    event["sender"] = {"open_id": "ou_flat", "name": "Flat"}
    inb = to_inbound(event)
    assert inb is not None
    assert inb.sender.user_id == "ou_flat"


# ── split_command ───────────────────────────────────────────────


def test_split_command_basic() -> None:
    assert split_command("/help") == ("help", "")


def test_split_command_with_arg() -> None:
    assert split_command("/model haiku") == ("model", "haiku")


def test_split_command_strips_bot_mention() -> None:
    """Some Feishu groups suffix `@bot_username`; strip it."""
    assert split_command("/help@my_bot") == ("help", "")


def test_split_command_with_arg_and_mention() -> None:
    assert split_command("/model@my_bot haiku") == ("model", "haiku")


def test_split_command_leading_whitespace_ok() -> None:
    assert split_command("  /esc") == ("esc", "")


def test_split_command_non_command_returns_none() -> None:
    assert split_command("hello") is None
    assert split_command("") is None


def test_split_command_empty_name_returns_none() -> None:
    """`/` alone (or `/ arg`) has no command name."""
    assert split_command("/") is None
    assert split_command("/ help") is None


def test_split_command_strips_trailing_arg_whitespace() -> None:
    assert split_command("/cmd  trailing  ") == ("cmd", "trailing")


# ── to_action_event ──────────────────────────────────────────────


def _action_event_dict(
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


def test_action_event_basic() -> None:
    ev = to_action_event(_action_event_dict())
    assert ev is not None
    assert ev.sender.user_id == "ou_alice"
    assert ev.sender.display_name == "Alice"
    assert ev.action_id == "ses:bind"
    assert ev.value == {}
    assert ev.card_anchor.message_id == "om_card"
    assert ev.conversation.chat_id == "oc_chat"
    assert ev.conversation.thread_id == "om_root"
    assert ev.ack_token == "tg-token"


def test_action_event_unpacks_value_keys() -> None:
    """Extra fields in `action.value` end up on `ActionEvent.value`,
    minus the `action_id` key."""
    ev = to_action_event(
        _action_event_dict(action_id="ses:bind", value={"pane_id": "@7", "extra": "x"})
    )
    assert ev is not None
    assert ev.action_id == "ses:bind"
    assert ev.value == {"pane_id": "@7", "extra": "x"}


def test_action_event_thread_none() -> None:
    """A card sent in a chat root (no reply chain) carries
    thread_id=None in the action event."""
    ev = to_action_event(_action_event_dict(thread_id=None))
    assert ev is not None
    assert ev.conversation.thread_id is None


def test_action_event_missing_operator_returns_none() -> None:
    event = _action_event_dict()
    del event["operator"]
    assert to_action_event(event) is None


def test_action_event_missing_open_id_returns_none() -> None:
    event = _action_event_dict()
    event["operator"] = {"user_name": "Alice"}
    assert to_action_event(event) is None


def test_action_event_missing_action_id_returns_none() -> None:
    event = _action_event_dict()
    event["action"]["value"] = {"pane_id": "@7"}  # no action_id
    assert to_action_event(event) is None


def test_action_event_missing_chat_id_returns_none() -> None:
    event = _action_event_dict()
    event["context"]["open_chat_id"] = ""
    assert to_action_event(event) is None


def test_action_event_missing_message_id_returns_none() -> None:
    event = _action_event_dict()
    event["context"]["open_message_id"] = ""
    assert to_action_event(event) is None


def test_action_event_input_value_surfaces_as_input_key() -> None:
    """Lark/Feishu input element submissions carry the typed text in
    a sibling field of `action` (`input_value`, `value_str`, or
    `option` depending on schema version). The parser lifts the
    first non-empty hit into `value['_input']` so handlers have a
    single canonical key to read."""
    event = _action_event_dict(action_id="ready:slot", value={"slot": "0"})
    event["action"]["tag"] = "input"
    event["action"]["input_value"] = "what's broken?"
    ev = to_action_event(event)
    assert ev is not None
    assert ev.action_id == "ready:slot"
    assert ev.value["slot"] == "0"
    assert ev.value["_input"] == "what's broken?"


def test_action_event_input_value_falls_back_to_option_field() -> None:
    """Older Lark schemas put the typed text on `action.option`."""
    event = _action_event_dict(action_id="ready:slot", value={"slot": "1"})
    event["action"]["tag"] = "input"
    event["action"]["option"] = "fallback shape"
    ev = to_action_event(event)
    assert ev is not None
    assert ev.value["_input"] == "fallback shape"


def test_action_event_empty_input_value_is_dropped() -> None:
    """An empty submission shouldn't pollute `value` with an empty
    `_input` key — the handler can check `_input not in value` to
    short-circuit."""
    event = _action_event_dict(action_id="ready:slot")
    event["action"]["tag"] = "input"
    event["action"]["input_value"] = ""
    ev = to_action_event(event)
    assert ev is not None
    assert "_input" not in ev.value


def test_action_event_button_click_has_no_input_key() -> None:
    """Regular button clicks (no input element involved) must not
    end up with a `_input` key on the event."""
    ev = to_action_event(_action_event_dict(action_id="ses:bind"))
    assert ev is not None
    assert "_input" not in ev.value
