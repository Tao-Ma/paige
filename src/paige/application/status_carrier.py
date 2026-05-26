"""StatusCarrierService — live status badge migrates to the most recent card.

Phase 2 of the "status on the panel anchor" model. Phase 1 put the
spinner text into the end-turn panel header, but the panel scrolls
out of view once tool_use / tool_result / assistant-text cards
land below it. This service migrates a small `⏱ Worked Ns` badge
to whichever card was most recently sent for each (person,
conversation), so the live status surface is always at the bottom
of the chat — no scrollback needed, no DELETEs (every transition
is a PATCH so Lark's deleted-card tombstone never appears).

Pipeline:
  - `StatusService.on_change` → `StatusCarrierService.on_status_change`.
    Updates the per-conversation status text; PATCHes the current
    carrier to reflect.
  - `Outbox.on_send_complete` → `StatusCarrierService._on_send_complete`.
    Whenever a new card is sent or an existing one is edited, the
    handler updates its records. New anchor with `Card.is_status_carrier`
    → strip badge from previous carrier (PATCH), this anchor becomes
    new carrier and gains badge. New anchor *without* the flag (a
    command response, server menu, etc.) is ignored — the current
    carrier keeps the badge.
  - All PATCHes the service issues are `suppress_hooks=True` to
    avoid recursing into its own send-complete handler.

Per-conversation tracking (`_ConvKey`):
  - `_carriers[key]` — the latest (anchor, card_without_badge) we
    want to keep status on. `card` is stored sans badge so the next
    PATCH can re-render the body intact and stamp the latest text.
  - `_status[key]` — current spinner text or None (idle).

When no carrier exists for a conversation (pre-first outbound, or
before any binding/scrape state has fired), status updates are a
no-op — they'll take effect once the next outbound card lands.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from ..domain.card import Card
from ..domain.conversation import Anchor, Conversation
from ..domain.outbound import CardContent, Outbound
from ..domain.pane import Binding
from ..domain.person import Person
from .outbox import Outbox

logger = logging.getLogger(__name__)

_ConvKey = tuple[str, str, str]  # (user_id, chat_id, thread_id_or_empty)


class StatusCarrierService:
    """Migrates the live status badge to the most recent outbound
    card per (person, conversation)."""

    def __init__(self, *, outbox: Outbox) -> None:
        self._outbox = outbox
        # Per-conversation current carrier: anchor + the card body
        # without the status_badge (so we can re-render with whatever
        # the latest status is on each PATCH).
        self._carriers: dict[_ConvKey, tuple[Anchor, Card]] = {}
        # Per-conversation current status text. None means idle.
        self._status: dict[_ConvKey, str | None] = {}

    def install(self) -> None:
        """Wire to the Outbox so we track every successful send."""
        self._outbox.on_send_complete(self._on_send_complete)

    # ── StatusService handler ───────────────────────────────────

    async def on_status_change(self, binding: Binding, text: str | None) -> None:
        """Called by `StatusService` on every (binding, status_text)
        change. Updates the per-conversation status and PATCHes the
        current carrier's badge."""
        key = _key(binding.person, binding.conversation)
        if self._status.get(key) == text:
            return
        self._status[key] = text
        await self._patch_carrier_badge(binding.person, binding.conversation)

    # ── Outbox handler ──────────────────────────────────────────

    async def _on_send_complete(
        self,
        person: Person,
        conversation: Conversation,
        anchor: Anchor,
        card: Card,
    ) -> None:
        """Called by `Outbox` after every successful send/edit.

        Decision tree:
          - If `anchor` matches the current carrier's anchor: this
            was an external edit (e.g. EndTurnPanelService morphed
            the panel). Refresh our stored card (sans badge) and
            re-PATCH so the badge stays applied.
          - Else: this is a new outbound. Strip badge from the
            previous carrier (if any), make this anchor the new
            carrier, and PATCH it to stamp the current status badge.
        """
        key = _key(person, conversation)
        prev = self._carriers.get(key)
        bare_card = replace(card, status_badge=None)
        if prev is not None and prev[0] == anchor:
            # External edit on the current carrier — update stored
            # card and re-apply badge. Carrier identity is anchor-
            # bound; the new card body's `is_status_carrier` flag is
            # not consulted here (e.g. an ask_user post-pick edit
            # might omit the flag but should still carry status).
            self._carriers[key] = (anchor, bare_card)
            await self._patch_carrier_badge(person, conversation)
            return
        if not card.is_status_carrier:
            # New anchor from a non-agent surface (command response,
            # interactive UI, server menu). Leave the current carrier
            # alone — the badge stays on the most recent agent card.
            return
        # New carrier. Strip badge from prior, switch.
        if prev is not None:
            await self._strip_badge(person, conversation, prev[0], prev[1])
        self._carriers[key] = (anchor, bare_card)
        await self._patch_carrier_badge(person, conversation)

    # ── internal: PATCH helpers ─────────────────────────────────

    async def _patch_carrier_badge(self, person: Person, conversation: Conversation) -> None:
        """PATCH the current carrier with the latest status badge.
        No-op when there's no carrier or no status text (idle)."""
        key = _key(person, conversation)
        carrier = self._carriers.get(key)
        if carrier is None:
            return
        text = self._status.get(key)
        if text is None:
            # Idle — strip any badge that may be on the carrier.
            await self._strip_badge(person, conversation, carrier[0], carrier[1])
            return
        anchor, bare_card = carrier
        with_badge = replace(bare_card, status_badge=text)
        outbound = Outbound(conversation=conversation, content=CardContent(card=with_badge))
        self._outbox.enqueue_edit(person, anchor, outbound, suppress_hooks=True)

    async def _strip_badge(
        self,
        person: Person,
        conversation: Conversation,
        anchor: Anchor,
        bare_card: Card,
    ) -> None:
        """PATCH `anchor` to render `bare_card` (badge=None), used
        when migrating away from a previous carrier."""
        outbound = Outbound(
            conversation=conversation,
            content=CardContent(card=replace(bare_card, status_badge=None)),
        )
        self._outbox.enqueue_edit(person, anchor, outbound, suppress_hooks=True)


def _key(person: Person, conversation: Conversation) -> _ConvKey:
    return (person.user_id, conversation.chat_id, conversation.thread_id or "")


__all__ = ["StatusCarrierService"]
