"""AgentBatchService — coalesce parallel Agent/Task launches into one card.

When Claude fans out N subagents in a turn, each is a distinct
`Agent`/`Task` tool_use. Rendered one-card-each that's N cards plus N
result morphs cluttering the thread. This service collapses a *batch*
— consecutive Agent/Task launches with no intervening non-agent block
— into a single card, one line per agent, and patch-updates it as
agents are added and as their results land:

    🤖 Agents · 1/3 done
    ✓ **Explore** — find the auth middleware
    ⏳ **general-purpose** — refactor the token check
    ⏳ **Plan** — design the rate-limiter

The dispatcher routes Agent/Task tool_use + their tool_results here;
every other tool keeps the generic 1:1 tool_use→tool_result path.

A batch closes when the dispatcher sees any non-agent block (text or
another tool) — the next Agent then opens a fresh card. The
tool_id→batch map outlives that close so a result that lands after
the batch closed still patches the right line.

Card edits go through the Outbox (out-of-band PATCH), same as the
generic tool_result morph — these are transcript-driven, not click-
driven, so they don't need the click-response inline-refresh slot.

Known limitation: `_agent_tool_ids` and `_by_tool` grow for the life
of the process (no prune on batch close — late results still need the
mapping). Entries are small and bounded by the session's agent count;
acceptable for a debug-grade aid. A `clear_binding` hook on unbind
would cap it if a very long-lived run ever makes it matter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from ..domain.card import Card
from ..domain.conversation import Anchor
from ..domain.outbound import CardContent, Outbound
from ..domain.pane import Binding
from ..domain.transcript import Block
from ..infrastructure.markdown_safe import inline_safe
from .outbox import Outbox

logger = logging.getLogger(__name__)

# Tool names Claude Code uses for subagent spawns. Both map here; the
# generic renderer (`tool_renderers`) likewise treats them as one.
AGENT_TOOL_NAMES: frozenset[str] = frozenset({"Agent", "Task"})

_DESC_CLIP = 80
_BindingKey = tuple[str, str, str]


@dataclass
class _Agent:
    tool_id: str
    subagent: str
    description: str
    done: bool = False


@dataclass
class _Batch:
    agents: list[_Agent]
    anchor: Anchor | None = None
    anchor_future: asyncio.Future[Anchor | None] | None = None


class AgentBatchService:
    """Per-binding coalescing of Agent/Task fan-out into one card."""

    def __init__(self, *, outbox: Outbox) -> None:
        self._outbox = outbox
        # The batch currently accepting new agents, per binding.
        self._open: dict[_BindingKey, _Batch] = {}
        # (binding_key, tool_id) → owning batch. Outlives `close()` so a
        # late result still finds its card/line.
        self._by_tool: dict[tuple[_BindingKey, str], _Batch] = {}
        # All agent tool_ids ever seen — lets the dispatcher route a
        # tool_result here vs. the generic 1:1 morph.
        self._agent_tool_ids: set[str] = set()

    def owns(self, tool_id: str) -> bool:
        """True if `tool_id` is an Agent/Task launch this service is
        tracking — the dispatcher checks this before routing a
        tool_result."""
        return tool_id in self._agent_tool_ids

    async def on_use(self, bindings: list[Binding], block: Block) -> None:
        """Handle an Agent/Task tool_use: append it to the open batch
        (patching the card) or open a new batch card."""
        if block.tool_id is None:
            return
        subagent, description = _parse_agent(block.text)
        self._agent_tool_ids.add(block.tool_id)
        for binding in bindings:
            key = _key(binding)
            agent = _Agent(tool_id=block.tool_id, subagent=subagent, description=description)
            batch = self._open.get(key)
            if batch is None:
                batch = _Batch(agents=[agent])
                self._open[key] = batch
                outbound = Outbound(
                    conversation=binding.conversation,
                    content=CardContent(card=_build_card(batch.agents)),
                )
                batch.anchor_future = self._outbox.enqueue_send(binding.person, outbound)
            else:
                batch.agents.append(agent)
                await self._repaint(binding, batch)
            self._by_tool[(key, block.tool_id)] = batch

    async def on_result(self, bindings: list[Binding], block: Block) -> None:
        """Handle an agent's tool_result: tick its line `⏳ → ✓` and
        patch the card. No-op for a tool_id we don't track."""
        if block.tool_id is None:
            return
        for binding in bindings:
            batch = self._by_tool.get((_key(binding), block.tool_id))
            if batch is None:
                continue
            for agent in batch.agents:
                if agent.tool_id == block.tool_id:
                    agent.done = True
                    break
            await self._repaint(binding, batch)

    def close(self, bindings: list[Binding]) -> None:
        """End the open batch for these bindings — the next Agent opens
        a fresh card. Keeps the tool_id→batch map intact so in-flight
        agents still tick their lines when results arrive."""
        for binding in bindings:
            self._open.pop(_key(binding), None)

    async def _repaint(self, binding: Binding, batch: _Batch) -> None:
        anchor = await self._anchor(batch)
        outbound = Outbound(
            conversation=binding.conversation,
            content=CardContent(card=_build_card(batch.agents)),
        )
        if anchor is None:
            # Original send failed / hadn't produced an anchor — degrade
            # to a fresh send so the latest state still reaches the user.
            batch.anchor_future = self._outbox.enqueue_send(binding.person, outbound)
            return
        self._outbox.enqueue_edit(binding.person, anchor, outbound)

    async def _anchor(self, batch: _Batch) -> Anchor | None:
        if batch.anchor is not None:
            return batch.anchor
        if batch.anchor_future is None:
            return None
        try:
            batch.anchor = await batch.anchor_future
        except Exception as e:  # send failed — _repaint will fresh-send
            logger.debug("agent batch send had no anchor: %s", e)
            batch.anchor = None
        return batch.anchor


def _key(binding: Binding) -> _BindingKey:
    return (
        binding.person.user_id,
        binding.conversation.chat_id,
        binding.conversation.thread_id or "",
    )


def _parse_agent(text: str | None) -> tuple[str, str]:
    """Pull `subagent_type` + `description` out of the tool_use input
    JSON. Both degrade gracefully: unknown subagent → "agent", missing
    description → "" (the line then shows just the subagent)."""
    try:
        raw: Any = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        raw = {}
    d = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    # Both fields are Claude-authored free text rendered inline next to
    # `**bold**` markup — flatten newlines + drop backticks so they can't
    # leak a heading or bleed an inline-code span across the card.
    subagent = inline_safe(str(d.get("subagent_type") or "agent")) or "agent"
    desc = inline_safe(str(d.get("description") or ""))
    if len(desc) > _DESC_CLIP:
        desc = desc[: _DESC_CLIP - 1] + "…"
    return subagent, desc


def _build_card(agents: list[_Agent]) -> Card:
    done = sum(1 for a in agents if a.done)
    lines: list[str] = []
    for a in agents:
        glyph = "✓" if a.done else "⏳"
        line = f"{glyph} **{a.subagent}**"
        if a.description:
            line += f" — {a.description}"
        lines.append(line)
    return Card(
        text="\n".join(lines),
        header_title=f"🤖 Agents · {done}/{len(agents)} done",
        header_color="wathet",
        is_status_carrier=True,
    )


__all__ = ["AGENT_TOOL_NAMES", "AgentBatchService"]
