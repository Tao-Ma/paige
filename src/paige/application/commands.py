"""CommandService — slash command handlers + forwarding fallback.

Native commands (handled by paige):

    /help     — list available commands as a card
    /esc      — send a single Escape keystroke to the bound pane
                (Claude Code's "interrupt" gesture)
    /unbind   — drop the binding between this conversation and its pane;
                the Claude session keeps running

`/start` is registered by `DirectoryService`, not here — picking a
directory + spawning claude is its concern. `/sessions` likewise
lives in `SessionsService`.

Forwarded commands (passed verbatim to the bound pane so Claude Code
handles them):

    /clear /compact /cost /memory /model

These are typed straight into the pane via the Multiplexer; the
acknowledgment the user sees IS Claude rendering the command. paige
adds nothing to chat for forwarded commands — silent forwarding
mirrors v1's behavior.

Per-command unbound behavior: each handler looks up the bound pane
via RunRegistry. If absent, the user gets a small hint suggesting
they bind a session first. Hints go through Outbox like all
content.

Slash commands NOT handled by this slice (each is its own follow-up):

    /sessions /history /screenshot /usage /server

Those need richer card UIs (pagination, image rendering, admin
gating) that aren't worth bundling here.
"""

from __future__ import annotations

import logging

from ..domain.card import Card
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound, TextContent
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .outbox import Outbox
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

# Commands forwarded verbatim to the bound pane.
FORWARDED_COMMANDS: tuple[str, ...] = ("clear", "compact", "cost", "memory", "model")

# Commands implemented natively in paige (CommandService scope only).
# `start` lives in DirectoryService; `sessions` in SessionsService.
NATIVE_COMMANDS: tuple[str, ...] = ("help", "esc", "unbind")

UNBOUND_COMMAND_HINT = "No session bound to this conversation. Use /start to pick a directory."


class CommandService:
    """Native slash-command handlers + Claude-command forwarding."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        allow_list: AllowList,
    ) -> None:
        self._registry = registry
        self._multiplexer = multiplexer
        self._outbox = outbox
        self._allow_list = allow_list

    def install(self, channel: Channel) -> None:
        """Register every native + forwarded handler on `channel`.
        Each handler is wrapped with the allow-list gate at install."""
        guard = self._allow_list.guard_command
        channel.on_command("help", guard(self._help))
        channel.on_command("esc", guard(self._esc))
        channel.on_command("unbind", guard(self._unbind))
        for name in FORWARDED_COMMANDS:
            channel.on_command(name, guard(self._forward))

    # ── /help ────────────────────────────────────────────────────

    async def _help(self, inbound: Inbound, _arg: str) -> None:
        body_lines = [
            "*Available commands*",
            "",
            "_paige:_",
            "  /help — show this card",
            "  /start — pick a directory and start a session",
            "  /esc — send Escape (interrupt Claude)",
            "  /unbind — detach this conversation from its pane",
            "",
            "_Claude (forwarded):_",
            "  /clear /compact /cost /memory /model",
        ]
        outbound = Outbound(
            conversation=inbound.conversation,
            content=CardContent(
                card=Card(
                    text="\n".join(body_lines),
                    header_title="📖 Help",
                    header_color="wathet",
                )
            ),
        )
        self._outbox.enqueue_send(inbound.sender, outbound)

    # ── /esc ─────────────────────────────────────────────────────

    async def _esc(self, inbound: Inbound, _arg: str) -> None:
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            await self._send_unbound_hint(inbound)
            return
        # Escape uses tmux named-key syntax: enter=False, literal=False.
        ok = await self._multiplexer.send_keys(pane_id, "Escape", enter=False, literal=False)
        if not ok:
            logger.warning("/esc: send_keys failed for pane %s", pane_id)

    # ── /unbind ──────────────────────────────────────────────────

    async def _unbind(self, inbound: Inbound, _arg: str) -> None:
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            await self._send_unbound_hint(inbound)
            return
        pane = await self._multiplexer.find_pane(pane_id)
        name = pane.pane_name if pane is not None else pane_id
        await self._registry.unbind(inbound.sender, inbound.conversation)
        outbound = Outbound(
            conversation=inbound.conversation,
            content=TextContent(f"Unbound from *{name}*. The Claude session keeps running."),
        )
        self._outbox.enqueue_send(inbound.sender, outbound)

    # ── /clear /compact /cost /memory /model — forwarded ─────────

    async def _forward(self, inbound: Inbound, _arg: str) -> None:
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            await self._send_unbound_hint(inbound)
            return
        # `inbound.text` already starts with the slash command (e.g.
        # "/clear" or "/compact"). Forward verbatim — Claude renders
        # the result in the pane, no chat-side ack needed.
        ok = await self._multiplexer.send_keys(pane_id, inbound.text, enter=True, literal=True)
        if not ok:
            logger.warning("forward command failed for pane %s: %s", pane_id, inbound.text)

    # ── shared helpers ───────────────────────────────────────────

    async def _send_unbound_hint(self, inbound: Inbound) -> None:
        outbound = Outbound(
            conversation=inbound.conversation,
            content=TextContent(UNBOUND_COMMAND_HINT),
        )
        self._outbox.enqueue_send(inbound.sender, outbound)


__all__ = [
    "FORWARDED_COMMANDS",
    "NATIVE_COMMANDS",
    "UNBOUND_COMMAND_HINT",
    "CommandService",
]
