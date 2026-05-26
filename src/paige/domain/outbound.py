"""Outbound — a message we want to send TO a user.

Outbound is a small tagged-content union. A single class with a
`content` field carrying the kind-specific payload keeps the
queue / outbox simple (one type to enqueue), while still allowing
the channel adapter to render each kind appropriately.

`reply_to` carries an `Anchor` when the outbound is a threaded
reply; the Feishu adapter honors it via reply-chain `root_id`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .card import Action, Card
from .conversation import Anchor, Conversation


@dataclass(frozen=True)
class TextContent:
    """Plain text (or backend-native rich text) body."""

    text: str


@dataclass(frozen=True)
class CardContent:
    """An interactive card."""

    card: Card


@dataclass(frozen=True)
class DocumentContent:
    """Binary upload — image, file, voice clip.

    `as_image` switches between native image rendering vs file
    attachment. `rows` lets an image carry an attached keyboard
    on backends where that's representable as a single card
    (Feishu `img + action` element); empty tuple means no
    keyboard.
    """

    data: bytes
    filename: str
    as_image: bool = False
    rows: tuple[tuple[Action, ...], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TypingContent:
    """A typing-indicator ping — no body, just "I'm working on it"."""


OutboundContent = TextContent | CardContent | DocumentContent | TypingContent


@dataclass(frozen=True)
class Outbound:
    """A pending outbound message — what we want to send.

    `collapse_threshold_lines` (>0) tells the Feishu adapter to wrap
    the card body in a `collapsible_panel` when the body's newline
    count exceeds the threshold (Lark v7.9+). Stamped by the Outbox
    from `CollapsePrefService` at enqueue time so per-(person,
    conversation) prefs apply uniformly. `0` = render flat.
    """

    conversation: Conversation
    content: OutboundContent
    reply_to: Anchor | None = None
    collapse_threshold_lines: int = 0
