"""ScreenshotService — `/screenshot` command + control-key card.

Captures the bound pane's visible text via the Multiplexer port,
renders it to a PNG with `infrastructure.terminal_image.render`, and
sends it as a single Outbound. Below the image we attach a row of
control buttons (arrows, Esc, Enter, Tab, Space, ^C) that send
matching keystrokes back to the pane on tap, plus a 🔄 Refresh
button that re-captures and replaces the image in-place.

Feishu image messages can't carry buttons; the channel adapter
wraps a `DocumentContent(as_image=True, rows=...)` into a single
`img + action` card. That detail lives in the adapter — this
service just hands the rows to Outbound and trusts the channel.

Refresh flow: the click handler captures the pane afresh, builds a
new `DocumentContent` Outbound, and asks the channel to `edit` the
card the user tapped. On Feishu this rides the inline-card-refresh
slot so the new image lands atomically with the click ack.
"""

from __future__ import annotations

import asyncio
import logging

from ..domain.card import Action, ActionEvent
from ..domain.conversation import Conversation
from ..domain.inbound import Inbound
from ..domain.outbound import DocumentContent, Outbound, TextContent
from ..infrastructure.terminal_image import render
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .message_seq import MessageSeqService
from .outbox import Outbox
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

ACTION_KEY = "ss:key"
ACTION_REFRESH = "ss:rfr"
UNBOUND_HINT = "No session bound to this conversation. Use /start to pick a directory."
PANE_GONE_HINT = "Pane is gone — its window must have been closed."
CAPTURE_FAILED_HINT = "Failed to capture pane content."
REFRESH_LABEL = "🔄 Refresh"
# Sleep after a control-key tap before re-capturing the pane. Gives
# the TUI a moment to redraw on its next frame so the auto-refresh
# image reflects the new state (an arrow press in an option picker
# wouldn't show the new selection if we captured too early).
_AUTO_REFRESH_DELAY_S = 0.2

# key_id → (tmux key name, label, literal). `literal=False` means
# tmux interprets it as a named key (Up, Escape, …); `literal=True`
# sends the characters as-is.
_KEYS: dict[str, tuple[str, str, bool]] = {
    "up": ("Up", "↑", False),
    "dn": ("Down", "↓", False),
    "lt": ("Left", "←", False),
    "rt": ("Right", "→", False),
    "esc": ("Escape", "⎋ Esc", False),
    "ent": ("Enter", "⏎ Enter", False),
    "spc": ("Space", "␣ Space", False),
    "tab": ("Tab", "⇥ Tab", False),
    "cc": ("C-c", "^C", False),
}


class ScreenshotService:
    """`/screenshot` capture + control-key tap handler."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        channel: Channel,
        allow_list: AllowList,
        message_seq: MessageSeqService,
    ) -> None:
        self._registry = registry
        self._multiplexer = multiplexer
        self._outbox = outbox
        self._channel = channel
        self._allow_list = allow_list
        self._message_seq = message_seq

    def install(self, channel: Channel) -> None:
        channel.on_command("screenshot", self._allow_list.guard_command(self._screenshot))
        channel.on_action(self._allow_list.guard_action(self._handle_action))

    # ── /screenshot ──────────────────────────────────────────────

    async def _screenshot(self, inbound: Inbound, _arg: str) -> None:
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            self._send_text(inbound, UNBOUND_HINT)
            return
        outbound = await self._capture_outbound(inbound.conversation, pane_id)
        if isinstance(outbound, str):
            self._send_text(inbound, outbound)
            return
        self._outbox.enqueue_send(inbound.sender, outbound)

    # ── tap on a control key ─────────────────────────────────────

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id == ACTION_REFRESH:
            await self._refresh(event)
            return
        if event.action_id != ACTION_KEY:
            return
        key_id = event.value.get("k", "")
        pane_id = event.value.get("p", "")
        info = _KEYS.get(key_id)
        if info is None or not pane_id:
            await self._channel.ack(event, "Invalid key")
            return
        tmux_key, label, literal = info
        pane = await self._multiplexer.find_pane(pane_id)
        if pane is None:
            await self._channel.ack(event, "Pane is gone")
            return
        ok = await self._multiplexer.send_keys(pane_id, tmux_key, enter=False, literal=literal)
        if not ok:
            logger.warning("/screenshot key %s: send_keys failed for %s", key_id, pane_id)
            await self._channel.ack(event, "Send failed")
            return
        # Auto-refresh: wait briefly for the TUI to redraw, then
        # re-capture and edit the card. Rides the same inline-refresh
        # slot that the explicit 🔄 button uses, so the swap lands
        # atomically with the click ack.
        await asyncio.sleep(_AUTO_REFRESH_DELAY_S)
        outbound = await self._capture_outbound(event.conversation, pane_id)
        if isinstance(outbound, Outbound):
            outbound, _ = self._message_seq.stamp_edit(
                event.sender, event.conversation, event.card_anchor, outbound
            )
            await self._channel.edit(event.card_anchor, outbound)
        else:
            logger.debug("/screenshot auto-refresh skipped: %s", outbound)
        await self._channel.ack(event, label)

    # ── tap on Refresh ───────────────────────────────────────────

    async def _refresh(self, event: ActionEvent) -> None:
        pane_id = event.value.get("p", "")
        if not pane_id:
            await self._channel.ack(event, "Invalid refresh")
            return
        outbound = await self._capture_outbound(event.conversation, pane_id)
        if isinstance(outbound, str):
            await self._channel.ack(event, outbound)
            return
        # On Feishu the channel detects this edit lives inside a click
        # dispatch and writes the new card into the click response so
        # the image swap lands atomically. The service just calls
        # edit and lets the adapter pick the right wire.
        # Seq stamper is consulted for chain bookkeeping; the
        # DocumentContent body has no text slot for a visible footer
        # (image bytes only), so the user sees no seq on the card —
        # but the chain stays consistent for any text-content edit
        # that lands on the same anchor later.
        outbound, _ = self._message_seq.stamp_edit(
            event.sender, event.conversation, event.card_anchor, outbound
        )
        await self._channel.edit(event.card_anchor, outbound)
        await self._channel.ack(event, REFRESH_LABEL)

    # ── shared capture helper ────────────────────────────────────

    async def _capture_outbound(
        self,
        conversation: Conversation,
        pane_id: str,
    ) -> Outbound | str:
        """Capture pane → render PNG → wrap in Outbound. Returns a
        short hint string if the pane is gone or capture is empty so
        the caller can route it to text-or-toast appropriately."""
        pane = await self._multiplexer.find_pane(pane_id)
        if pane is None:
            return PANE_GONE_HINT
        text = await self._multiplexer.capture(pane_id)
        if not text:
            return CAPTURE_FAILED_HINT
        png = await asyncio.to_thread(render, text)
        return Outbound(
            conversation=conversation,
            content=DocumentContent(
                data=png,
                filename="screenshot.png",
                as_image=True,
                rows=_build_rows(pane_id),
            ),
        )

    # ── helpers ──────────────────────────────────────────────────

    def _send_text(self, inbound: Inbound, text: str) -> None:
        outbound = Outbound(
            conversation=inbound.conversation,
            content=TextContent(text),
        )
        self._outbox.enqueue_send(inbound.sender, outbound)


def _build_rows(pane_id: str) -> tuple[tuple[Action, ...], ...]:
    """3×3 grid + ^C / Tab / Space layout, with a full-width Refresh
    row at the bottom. Pane id rides in every action's `value` so
    the handler can route without re-resolving the binding — the
    user might have unbound and re-bound between send and tap."""

    def b(key_id: str) -> Action:
        _, label, _ = _KEYS[key_id]
        return Action(
            label=label,
            action_id=ACTION_KEY,
            value={"k": key_id, "p": pane_id},
        )

    refresh = Action(
        label=REFRESH_LABEL,
        action_id=ACTION_REFRESH,
        value={"p": pane_id},
    )
    return (
        (b("spc"), b("up"), b("tab")),
        (b("lt"), b("dn"), b("rt")),
        (b("esc"), b("cc"), b("ent")),
        (refresh,),
    )


__all__ = ["ACTION_KEY", "ACTION_REFRESH", "ScreenshotService"]
