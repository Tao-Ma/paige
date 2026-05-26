"""Conversation + Anchor — *where* messages happen and *which* message."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Conversation:
    """A chat (with optional thread/topic) where messages happen.

    `chat_id` — backend-native chat id, stringified.
        Feishu: `oc_xxx` open chat id, or `ou_xxx` for DMs.
    `thread_id` — optional reply-target message id within the chat.
        Feishu: reply-chain `root_id` (`om_xxx`); used as
        `reply_to_message_id` on outbound sends so subsequent
        messages stay in the same chain (which is what Lark threads
        a topic under in a topic-mode group).
        None means "the chat root, no thread".
    `topic_id` — optional Lark topic id (`omt_xxx`) when the chat is
        a topic-mode group. Independent of `thread_id`: paige uses
        `topic_id` purely as a binding-key discriminator so each
        topic in a shared group gets its own binding; outbound
        replies still target a message id via `thread_id`.
        None outside topic-mode groups.
    """

    chat_id: str
    thread_id: str | None = None
    topic_id: str | None = None


@dataclass(frozen=True)
class Anchor:
    """A reference to a previously-sent message.

    Used for edits, deletes, and replies. Adapter-opaque
    `message_id` — paige doesn't care about the id format.
    """

    conversation: Conversation
    message_id: str
