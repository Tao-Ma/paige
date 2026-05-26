"""Channel — the IM port.

A `Channel` is a bidirectional connection to an IM backend. Outbound
messages flow through `send` / `edit` / `delete`; inbound messages
and action events arrive via the registered handlers.

The port is *unified* — there's a single `send(outbound)` instead
of v1's send_text / send_interactive / send_document split. The
adapter dispatches on `outbound.content` internally. Same for
`edit`. This keeps the surface small and the worker code simple
(one type to enqueue).

Quirks specific to a backend (Feishu's card patch dedup,
edit-window expiry, render-lag dance) are the adapter's problem —
they MUST NOT leak through this Protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from ..domain.card import ActionEvent
from ..domain.conversation import Anchor, Conversation
from ..domain.inbound import Attachment, Inbound
from ..domain.outbound import Outbound

InboundHandler = Callable[[Inbound], Awaitable[None]]
ActionHandler = Callable[[ActionEvent], Awaitable[None]]
CommandHandler = Callable[[Inbound, str], Awaitable[None]]


@runtime_checkable
class Channel(Protocol):
    """Bidirectional IM channel — outbound + inbound + actions."""

    # ── lifecycle ────────────────────────────────────────────────
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # ── outbound ─────────────────────────────────────────────────
    async def send(self, outbound: Outbound) -> Anchor | None:
        """Send a message; return its anchor for later edit/delete.

        Returns None for fire-and-forget content (currently
        TypingContent — no message is created on the backend, so
        there's nothing to anchor). Callers wanting an anchor for
        every send should already know which content kinds yield
        one and which don't.
        """
        ...

    async def edit(self, anchor: Anchor, outbound: Outbound) -> Anchor | None:
        """Edit an existing message.

        Some backends fall back to delete-and-resend when the new
        content kind doesn't match the old (e.g. Feishu can patch a
        card but not a post). On fallback the adapter returns the
        new anchor so callers can update their tracking; otherwise
        returns None and the original anchor stays valid.
        """
        ...

    async def delete(self, anchor: Anchor) -> None: ...

    # ── inbound media ────────────────────────────────────────────
    async def download(self, attachment: Attachment) -> bytes: ...

    # ── action handling ──────────────────────────────────────────
    async def ack(self, event: ActionEvent, text: str | None = None) -> None:
        """Acknowledge a button press (Feishu card-action toast).
        `text` shows briefly to the tapper."""
        ...

    # ── liveness ─────────────────────────────────────────────────
    async def probe(self, conversation: Conversation) -> bool:
        """True if the conversation is still alive on the backend.
        False on definitive `chat-deleted` codes; True on transient
        errors (fail-open so live bindings aren't nuked on a hiccup)."""
        ...

    # ── handler registration ─────────────────────────────────────
    def on_inbound(self, handler: InboundHandler) -> None:
        """Register a handler for plain text messages."""
        ...

    def on_command(self, name: str, handler: CommandHandler) -> None:
        """Register a handler for `/<name> [args]` commands."""
        ...

    def on_action(self, handler: ActionHandler) -> None:
        """Register a handler for card button presses."""
        ...

    # ── synthetic command dispatch ───────────────────────────────
    async def dispatch_command(self, inbound: Inbound, name: str, arg: str) -> bool:
        """If a handler for `/<name>` is registered, invoke it with
        `(inbound, arg)` and return True. Otherwise return False.

        Used by application services that need to route synthesized
        user input through the same path real `/cmd` messages take —
        e.g. text typed into a card input slot.
        """
        ...
