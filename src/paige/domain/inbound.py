"""Inbound — a message *from* the user, in neutral terms."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .conversation import Conversation
from .person import Person


class AttachmentKind(StrEnum):
    """What kind of attachment came in."""

    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"


@dataclass(frozen=True)
class Attachment:
    """An attached file the user sent.

    `fetch_id` is whatever the adapter needs to download the bytes
    (Feishu `image_key` / `file_key`). `containing_message_id` is
    set when the adapter needs the enclosing message's id to fetch —
    Feishu does, since its resource endpoint is keyed by both
    message + attachment. Adapters that can fetch from `fetch_id`
    alone leave this `None`.
    """

    kind: AttachmentKind
    fetch_id: str
    mime_type: str = ""
    duration_sec: float = 0.0
    containing_message_id: str | None = None


@dataclass(frozen=True)
class Inbound:
    """A message received from a user."""

    sender: Person
    conversation: Conversation
    text: str
    message_id: str
    attachments: tuple[Attachment, ...] = ()
    mentions: tuple[str, ...] = field(default_factory=tuple)
    timestamp_ms: int = 0
