"""UsageService — `/usage` Claude-Code-quota peek.

`/usage` is one of Claude Code's modal slash commands: the TUI
opens a Settings overlay with a "Usage" tab showing daily / weekly
quota bars and reset timestamps. paige can't query this through an
API — Claude doesn't surface it. So we drive the TUI: send `/usage`
into the bound pane, wait for the modal to render, scrape the pane
text, dismiss with Escape, parse + reply.

The wait is fixed (1.5s default) — enough for Claude to repaint
without making the user wait long. Tunable for tests.

Falls back to a code-blocked dump of the raw capture (truncated)
when `parse_usage` doesn't recognize the modal — covers the case
where the TUI version drifts and our parser stops matching, so the
user still sees something instead of "Failed to capture."
"""

from __future__ import annotations

import asyncio
import logging

from ..domain.inbound import Inbound
from ..domain.outbound import Outbound, TextContent
from ..infrastructure.usage_parser import parse_usage
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .outbox import Outbox
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

UNBOUND_HINT = "No session bound to this conversation. Use /start to pick a directory."
PANE_GONE_HINT = "Pane is gone — its window must have been closed."
CAPTURE_FAILED_HINT = "Failed to capture usage info."

_RAW_FALLBACK_LIMIT = 3000


class UsageService:
    """`/usage` — drive the TUI modal, parse, reply."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        allow_list: AllowList,
        modal_render_delay: float = 1.5,
    ) -> None:
        self._registry = registry
        self._multiplexer = multiplexer
        self._outbox = outbox
        self._allow_list = allow_list
        self._modal_render_delay = modal_render_delay

    def install(self, channel: Channel) -> None:
        channel.on_command("usage", self._allow_list.guard_command(self._usage))

    async def _usage(self, inbound: Inbound, _arg: str) -> None:
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            self._reply(inbound, UNBOUND_HINT)
            return
        pane = await self._multiplexer.find_pane(pane_id)
        if pane is None:
            self._reply(inbound, PANE_GONE_HINT)
            return

        # Drive the modal: type `/usage`, wait for repaint, capture,
        # dismiss with Escape.
        await self._multiplexer.send_keys(pane_id, "/usage", enter=True, literal=True)
        await asyncio.sleep(self._modal_render_delay)
        text = await self._multiplexer.capture(pane_id)
        await self._multiplexer.send_keys(pane_id, "Escape", enter=False, literal=False)

        if not text:
            self._reply(inbound, CAPTURE_FAILED_HINT)
            return

        info = parse_usage(text)
        if info is not None:
            body = "\n".join(info.lines)
        else:
            body = text.strip()
            if len(body) > _RAW_FALLBACK_LIMIT:
                body = body[:_RAW_FALLBACK_LIMIT] + "\n… (truncated)"
        self._reply(inbound, f"```\n{body}\n```")

    def _reply(self, inbound: Inbound, text: str) -> None:
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=TextContent(text),
            ),
        )


__all__ = [
    "CAPTURE_FAILED_HINT",
    "PANE_GONE_HINT",
    "UNBOUND_HINT",
    "UsageService",
]
