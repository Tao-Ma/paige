"""Pure converters: Feishu WS event → paige domain types.

Takes a stripped-down event dict (the shape lark-oapi delivers) and
produces an `Inbound` or `ActionEvent`. No SDK dep, no I/O —
testable in isolation against synthetic dicts.

Why dict input rather than lark types: testing. lark-oapi's
`P2MessageReceiveV1` is awkward to construct in tests; a small
normalized dict is easy. The channel adapter does the
lark-event → dict normalization at the wire boundary so this
module stays SDK-agnostic.

Threading on Feishu = reply-chain via `root_id`. The first message
of a chain has `root_id=None`; all replies reference its
`message_id` as their `root_id`. paige models this by setting
`Conversation.thread_id` to `root_id` (or to the message's own
`message_id` when there's no root, so future replies can attach).

Topic-mode groups (Lark "话题模式群") add a second identifier:
`thread_id` on the event payload — an `omt_xxx` topic id —
distinct from the reply-chain `root_id`. Paige stores it on
`Conversation.topic_id` so bindings can be scoped per Lark topic
without changing the outbound send path (still reply-targets a
message id via `thread_id`).
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from ...domain.card import ActionEvent
from ...domain.conversation import Anchor, Conversation
from ...domain.inbound import Attachment, AttachmentKind, Inbound
from ...domain.person import Person

logger = logging.getLogger(__name__)


def to_inbound(event: dict[str, Any]) -> Inbound | None:
    """Convert a normalized Feishu message event into an `Inbound`.

    Returns None when:
      - the event isn't a message event (other event types pass
        through different handlers),
      - sender info is missing (system / anonymous messages),
      - `message_type` isn't supported (text only for slice 15b;
        image/file/audio land in a later slice).

    Expected event shape (normalized at the channel boundary):

        {
          "message": {
            "message_id": "om_xxx",
            "root_id": "om_yyy" | None,
            "create_time": "1700000000000",
            "chat_id": "oc_xxx",
            "chat_type": "group" | "p2p",
            "message_type": "text",
            "content": "{\\"text\\": \\"hello\\"}",
            "mentions": [...] | None,
          },
          "sender": {
            "sender_id": {"open_id": "ou_xxx", ...},
            "sender_type": "user",
            "name": "Alice"
          }
        }
    """
    msg_raw = event.get("message")
    sender_raw = event.get("sender")
    if not isinstance(msg_raw, dict) or not isinstance(sender_raw, dict):
        return None
    msg = cast("dict[str, Any]", msg_raw)
    sender = cast("dict[str, Any]", sender_raw)

    open_id = _open_id(sender)
    if not open_id:
        return None

    msg_type = str(msg.get("message_type", ""))
    chat_id = str(msg.get("chat_id", ""))
    if not chat_id:
        return None

    msg_id = str(msg.get("message_id", ""))
    raw_content = msg.get("content", "")

    # Pull text + attachments out of the content payload based on
    # message_type. Unsupported types (sticker, video, merge_forward)
    # return None — the channel drops them silently.
    body = _parse_content_for_type(msg_type, raw_content, msg_id)
    if body is None:
        return None
    text, attachments = body

    # Threading: each Feishu reply chain is one paige "conversation."
    # The first message of a chain has root_id=None and uses its own
    # msg_id as the chain root; replies in the chain set root_id to
    # the chain root's msg_id. Either way, every message in the same
    # chain shares the same thread_id value.
    raw_root = msg.get("root_id")
    thread_id = str(raw_root) if raw_root else msg_id

    # Topic mode: `message.thread_id` carries Lark's `omt_xxx`
    # identifier in topic-mode groups; absent in regular chats.
    topic_id = _topic_id(msg.get("thread_id"))

    return Inbound(
        sender=Person(
            user_id=open_id,
            display_name=str(sender.get("name", "")),
        ),
        conversation=Conversation(
            chat_id=chat_id,
            thread_id=thread_id or None,
            topic_id=topic_id,
        ),
        text=text,
        message_id=msg_id,
        attachments=attachments,
        timestamp_ms=_parse_create_time(msg.get("create_time")),
    )


# ── per-message-type content parsers ────────────────────────────


def _parse_content_for_type(
    msg_type: str,
    content_raw: Any,
    message_id: str,
) -> tuple[str, tuple[Attachment, ...]] | None:
    """Dispatch on msg_type. Each handler returns (text, attachments)
    or None when the type is unsupported. The channel uses None as
    "drop event."
    """
    if msg_type == "text":
        text = _extract_text(content_raw)
        return (text, ()) if text is not None else None
    if msg_type == "image":
        att = _attachment_from_image(content_raw, message_id)
        return ("", (att,)) if att is not None else None
    if msg_type in ("audio", "file"):
        att = _attachment_from_file(content_raw, message_id, msg_type)
        return ("", (att,)) if att is not None else None
    if msg_type == "post":
        text = _flatten_post(content_raw)
        return (text, ()) if text is not None else None
    return None


def _attachment_from_image(content_raw: Any, message_id: str) -> Attachment | None:
    """Image message content: `{"image_key": "img_xxx"}`."""
    parsed = _parse_json_dict(content_raw)
    if parsed is None:
        return None
    image_key = parsed.get("image_key")
    if not isinstance(image_key, str) or not image_key:
        return None
    return Attachment(
        kind=AttachmentKind.IMAGE,
        fetch_id=image_key,
        containing_message_id=message_id or None,
    )


def _attachment_from_file(content_raw: Any, message_id: str, msg_type: str) -> Attachment | None:
    """Audio + file messages share the `file_key` shape; differ only
    in `duration` (audio) and `file_name` (file)."""
    parsed = _parse_json_dict(content_raw)
    if parsed is None:
        return None
    file_key = parsed.get("file_key")
    if not isinstance(file_key, str) or not file_key:
        return None
    kind = AttachmentKind.AUDIO if msg_type == "audio" else AttachmentKind.FILE
    duration = parsed.get("duration")
    duration_sec = float(duration) / 1000.0 if isinstance(duration, int | float) else 0.0
    return Attachment(
        kind=kind,
        fetch_id=file_key,
        duration_sec=duration_sec,
        containing_message_id=message_id or None,
    )


def _flatten_post(content_raw: Any) -> str | None:
    """Feishu post messages have a structured tree similar to what
    paige's post.py renders. For inbound, we flatten to plain text:
    paragraphs joined by newlines, elements within a paragraph
    concatenated (text and link `text` fields)."""
    parsed = _parse_json_dict(content_raw)
    if parsed is None:
        return None
    paragraphs: list[Any] | None = None
    for key in ("zh_cn", "en_us", "ja_jp"):
        section_raw = parsed.get(key)
        if isinstance(section_raw, dict):
            section = cast("dict[str, Any]", section_raw)
            content = section.get("content")
            if isinstance(content, list):
                paragraphs = cast("list[Any]", content)
                break
    if paragraphs is None:
        return None
    out_lines: list[str] = []
    for para in paragraphs:
        if not isinstance(para, list):
            continue
        para_typed = cast("list[Any]", para)
        line_parts: list[str] = []
        for elem in para_typed:
            if isinstance(elem, dict):
                elem_typed = cast("dict[str, Any]", elem)
                text = elem_typed.get("text", "")
                if isinstance(text, str) and text:
                    line_parts.append(text)
        out_lines.append("".join(line_parts))
    return "\n".join(out_lines).strip() or None


def _parse_json_dict(content_raw: Any) -> dict[str, Any] | None:
    """Common pattern: Feishu `content` is a JSON-encoded dict.
    Returns the parsed dict or None on shape/parse error."""
    if not isinstance(content_raw, str) or not content_raw:
        return None
    try:
        raw = json.loads(content_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return cast("dict[str, Any]", raw)


# ── helpers ──────────────────────────────────────────────────────


def _open_id(sender: dict[str, Any]) -> str:
    """Pull the `open_id` out of the nested sender_id dict.
    Falls back to top-level `open_id` for shapes that don't nest."""
    sid_raw = sender.get("sender_id")
    if isinstance(sid_raw, dict):
        sid = cast("dict[str, Any]", sid_raw)
        oid = sid.get("open_id")
        if oid:
            return str(oid)
    flat = sender.get("open_id")
    return str(flat) if flat else ""


def _extract_text(content_raw: Any) -> str | None:
    """Feishu `content` for text messages is a JSON string with a
    single `text` field. Return the text, or None on parse error."""
    parsed = _parse_json_dict(content_raw)
    if parsed is None:
        return None
    text = parsed.get("text")
    return text if isinstance(text, str) else None


def _parse_create_time(raw: Any) -> int:
    """Feishu sends `create_time` as a stringified millisecond
    epoch. Returns 0 on missing/invalid."""
    if not isinstance(raw, str):
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _topic_id(raw: Any) -> str | None:
    """Return `raw` as a topic id when it looks like Lark's
    `omt_xxx` shape; None otherwise. Lets the parser distinguish
    topic-mode group identifiers from reply-chain message ids
    (`om_xxx`) that share the `thread_id` JSON field name."""
    if isinstance(raw, str) and raw.startswith("omt_"):
        return raw
    return None


# ── command splitting ───────────────────────────────────────────


def split_command(text: str) -> tuple[str, str] | None:
    """If `text` looks like `/<name> [arg]`, return `(name, arg)`.
    Otherwise None. Used by the channel to route `/cmd` messages
    to `on_command` handlers instead of `on_inbound`.

    Strips `@bot_username` mention suffixes that some Feishu groups
    auto-append to slash commands (e.g. `/help@my_bot`).
    """
    s = text.lstrip()
    if not s.startswith("/"):
        return None
    head, _, rest = s.partition(" ")
    name = head[1:]
    if "@" in name:
        name = name.split("@", 1)[0]
    if not name:
        return None
    return name, rest.strip()


def to_action_event(event: dict[str, Any]) -> ActionEvent | None:
    """Convert a Feishu card-action trigger event into an `ActionEvent`.

    Expected normalized shape (from the channel boundary):

        {
          "operator": {"open_id": "ou_alice", "user_name": "Alice"} | None,
          "action": {
            "tag": "button",
            "value": {"action_id": "ses:bind", "pane_id": "@7"}
          },
          "context": {
            "open_message_id": "om_card_xxx",
            "open_chat_id": "oc_chat",
            "thread_id": "om_root" | None
          },
          "token": "tg-trigger-token-xxx"
        }

    The packed `action_id` is unpacked back out of `value`; the
    remaining keys form the cleaned `value` dict on the
    `ActionEvent`. Returns None on missing required fields —
    malformed events are dropped silently.
    """
    operator_raw = event.get("operator")
    action_raw = event.get("action")
    context_raw = event.get("context")
    if not (
        isinstance(operator_raw, dict)
        and isinstance(action_raw, dict)
        and isinstance(context_raw, dict)
    ):
        return None
    operator = cast("dict[str, Any]", operator_raw)
    action = cast("dict[str, Any]", action_raw)
    context = cast("dict[str, Any]", context_raw)

    open_id = str(operator.get("open_id", ""))
    if not open_id:
        return None

    raw_value = action.get("value")
    if not isinstance(raw_value, dict):
        return None
    value_dict = cast("dict[str, Any]", raw_value)
    action_id = value_dict.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return None
    # `_thread_id` / `_topic_id` are paige's round-trip keys — the
    # card renderer injects them into every button's value so a click
    # always reports the same identifiers the card was sent under.
    # Without this, Feishu's `context.thread_id` is unreliable in
    # p2p chats (lands as None on the click), and a bind made via
    # click would never match the typed-text inbound (which keys on
    # the chain root's msg_id).
    embedded_thread_id = value_dict.get("_thread_id")
    embedded_topic_id = value_dict.get("_topic_id")
    cleaned_value: dict[str, str] = {
        k: str(v)
        for k, v in value_dict.items()
        if k not in ("action_id", "_thread_id", "_topic_id")
    }
    # Input-element submissions carry the user's typed text outside
    # the static `value` payload. Lark/Feishu use a handful of
    # field names across schema versions; we try them in order and
    # surface the first non-empty hit as `_input` so handlers have
    # a single canonical key. The action's `tag` is `input` for
    # native inputs and `button`/etc. for click events that don't
    # have a typed value.
    input_raw = action.get("input_value") or action.get("value_str") or action.get("option")
    if input_raw is not None and not isinstance(input_raw, dict):
        text_in = str(input_raw)
        if text_in:
            cleaned_value["_input"] = text_in

    chat_id = str(context.get("open_chat_id", ""))
    if not chat_id:
        return None
    # context.thread_id from card-click events can be either the
    # Lark topic id (`omt_xxx`, in topic-mode groups) or the reply
    # chain root (`om_xxx`). Disambiguate by prefix; treat anything
    # else as a chain root for backwards compat.
    raw_thread = context.get("thread_id")
    ctx_topic_id = _topic_id(raw_thread)
    ctx_chain_id = str(raw_thread) if raw_thread and ctx_topic_id is None else None
    if isinstance(embedded_thread_id, str) and embedded_thread_id:
        thread_id: str | None = embedded_thread_id
    else:
        thread_id = ctx_chain_id
    if isinstance(embedded_topic_id, str) and embedded_topic_id:
        topic_id: str | None = embedded_topic_id
    else:
        topic_id = ctx_topic_id
    message_id = str(context.get("open_message_id", ""))
    if not message_id:
        return None

    conv = Conversation(chat_id=chat_id, thread_id=thread_id, topic_id=topic_id)
    return ActionEvent(
        sender=Person(
            user_id=open_id,
            display_name=str(operator.get("user_name", "")),
        ),
        conversation=conv,
        card_anchor=Anchor(conversation=conv, message_id=message_id),
        action_id=action_id,
        value=cleaned_value,
        ack_token=str(event.get("token", "")),
    )


__all__ = ["split_command", "to_action_event", "to_inbound"]
