"""Dispatcher — wires inbound and outbound flows.

Two directions, one place:

**Outbound** (Claude → user):
  Watcher emits `(run_id, TranscriptEvent)` →
  RunRegistry.find_bindings_for_run(run_id) →
  for each binding, render each block to text →
  Outbox.enqueue_send(person, Outbound(conversation, content)).

  Tool_use ↔ tool_result pairing: when a TOOL_USE block is sent,
  its Outbox Future is stashed by tool_id; when the matching
  TOOL_RESULT arrives, the Future is awaited (which guarantees the
  send completed) and the result body is enqueued as an `edit` of
  that anchor — the tool_use card morphs into the result in place.

  Streaming content goes out as `CardContent` (a one-element card
  with no rows), not `TextContent`. Feishu's `patch_message` only
  works on cards, so cards are required for the in-place edit
  above. UNBOUND_HINT and the StatusService remain `TextContent`
  — neither expects to be patched.

  Echo: USER-role events whose text matches a recent send_keys are
  dropped (it's the user's IM message bouncing back through Claude's
  JSONL). Beyond the TTL window, USER events are assumed to be
  tmux-typed and forwarded.

**Inbound** (user → Claude):
  Channel.on_inbound(Inbound) →
  RunRegistry.get_pane(sender, conversation) →
  EchoDedup.record + Multiplexer.send_keys.

  Unbound conversations get a one-line nudge.

This service is intentionally narrow: rendering is one block at a
time (no merging — the Outbox doesn't merge either; that's a
follow-up if needed), no verbosity filtering (that's
VerbosityService in 6d), no status card (StatusService in 7a).
"""

from __future__ import annotations

import asyncio
import logging

from ..domain.card import Card
from ..domain.conversation import Anchor
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound, TextContent
from ..domain.pane import Binding
from ..domain.transcript import Block, BlockKind, Role, TranscriptEvent
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from ..ports.watcher import Watcher
from .access import AllowList
from .agent_batch import AGENT_TOOL_NAMES, AgentBatchService
from .ask_user import TOOL_NAME as ASK_USER_TOOL_NAME
from .ask_user import build_card as build_ask_user_card
from .ask_user import parse_questions as parse_ask_user_questions
from .echo_dedup import EchoDedup
from .outbox import Outbox
from .run_registry import RunRegistry
from .task_tracker import TASK_TOOL_NAMES, TaskTrackerService
from .tool_renderers import render_tool_use
from .verbosity import ContentKind, VerbosityService

logger = logging.getLogger(__name__)

UNBOUND_HINT = (
    "No session bound to this conversation. Use /sessions to pick one or /start a new one."
)


class Dispatcher:
    """Routes events between Channel, Watcher, Multiplexer, and Outbox."""

    def __init__(
        self,
        *,
        channel: Channel,
        watcher: Watcher,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        echo_dedup: EchoDedup,
        verbosity: VerbosityService,
        allow_list: AllowList,
    ) -> None:
        self._channel = channel
        self._watcher = watcher
        self._registry = registry
        self._multiplexer = multiplexer
        self._outbox = outbox
        self._echo_dedup = echo_dedup
        self._verbosity = verbosity
        self._allow_list = allow_list
        # (person.user_id, chat_id, thread_id_or_empty, tool_id) →
        #   (Future[Anchor | None] of the tool_use's send, tool_name).
        # The tool_name rides along so the tool_result edit can
        # rebuild the card with the same `🔧 {tool_name}` header
        # that the original tool_use card had — Feishu's PATCH
        # replaces the entire card including its header strip, so
        # we have to re-supply it on every edit.
        self._tool_anchors: dict[
            tuple[str, str, str, str],
            tuple[asyncio.Future[Anchor | None], str],
        ] = {}
        # Coalesces parallel Agent/Task fan-out into a single card.
        # Owns its own batch + tool_id→line state; the generic 1:1
        # tool_use→tool_result path below stays for every other tool.
        self._agent_batch = AgentBatchService(outbox=outbox)
        # Coalesces TaskCreate/TaskUpdate spam into per-group task cards.
        self._task_tracker = TaskTrackerService(outbox=outbox)

    def install(self) -> None:
        """Register handlers on the wired Channel + Watcher. Call
        once after construction."""
        # Inbound goes through the allow-list gate. Watcher events
        # are server-internal (Claude's transcript) and are not
        # gated — once a binding exists, its events flow.
        self._channel.on_inbound(self._allow_list.guard_inbound(self._handle_inbound))
        self._watcher.on_event(self._handle_transcript_event)

    # ── inbound (user → Claude) ──────────────────────────────────

    async def _handle_inbound(self, inbound: Inbound) -> None:
        if not inbound.text.strip():
            return  # nothing to forward
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            self._outbox.enqueue_send(
                inbound.sender,
                Outbound(
                    conversation=inbound.conversation,
                    content=TextContent(UNBOUND_HINT),
                ),
            )
            return
        self._echo_dedup.record(pane_id, inbound.text)
        ok = await self._multiplexer.send_keys(pane_id, inbound.text, enter=True, literal=True)
        if not ok:
            logger.warning("send_keys failed for pane %s — pane may be gone", pane_id)

    # ── outbound (Claude → user) ─────────────────────────────────

    async def _handle_transcript_event(self, run_id: str, event: TranscriptEvent) -> None:
        bindings = self._registry.find_bindings_for_run(run_id)
        if not bindings:
            return  # nobody listening
        for block in event.blocks:
            text = render_block(block)
            if text is None:
                continue
            if event.role is Role.USER and self._all_echos(bindings, text):
                continue
            await self._dispatch_block(bindings, block, text)

    def _all_echos(self, bindings: list[Binding], text: str) -> bool:
        """True if every binding's pane has a recent matching send_keys.

        We consume the dedup entries here — even if we end up sending
        anyway (some panes had no record), the consumed ones won't
        re-match later.

        The "all" criterion is conservative: if a single binding's
        pane has no record, we forward to all of them. The user gets
        one extra echo there rather than missing a tmux-typed prompt
        elsewhere.
        """
        results = [self._echo_dedup.is_echo(b.pane_id, text) for b in bindings]
        return all(results)

    async def _dispatch_block(self, bindings: list[Binding], block: Block, text: str) -> None:
        if block.kind is BlockKind.TOOL_RESULT and block.tool_id:
            # An agent's result ticks a line in its batch card; a
            # TaskCreate/TaskUpdate result updates its task card (or is
            # swallowed); every other tool_result morphs its 1:1 card.
            if self._agent_batch.owns(block.tool_id):
                await self._agent_batch.on_result(bindings, block)
                return
            if self._task_tracker.owns(block.tool_id):
                await self._task_tracker.on_result(bindings, block)
                return
            await self._dispatch_tool_result(bindings, block, text)
            return
        # Agent/Task fan-out coalesces into one batch card.
        if (
            block.kind is BlockKind.TOOL_USE
            and block.tool_id is not None
            and block.tool_name in AGENT_TOOL_NAMES
        ):
            await self._agent_batch.on_use(bindings, block)
            return
        # Any other block (text, a non-agent tool) ends the current
        # fan-out group: the next Agent opens a fresh batch card.
        self._agent_batch.close(bindings)
        # TaskCreate/TaskUpdate coalesce into per-group task cards.
        if (
            block.kind is BlockKind.TOOL_USE
            and block.tool_id is not None
            and block.tool_name in TASK_TOOL_NAMES
        ):
            await self._task_tracker.on_use(bindings, block)
            return
        # AskUserQuestion gets a buttoned card (see ask_user.py). On
        # parse failure `_dispatch_ask_user` returns False and we fall
        # through to the generic render so the event isn't dropped.
        if (
            block.kind is BlockKind.TOOL_USE
            and block.tool_id is not None
            and block.tool_name == ASK_USER_TOOL_NAME
            and await self._dispatch_ask_user(bindings, block)
        ):
            return
        tool_name = block.tool_name or "tool"
        is_tool_use = block.kind is BlockKind.TOOL_USE and block.tool_id is not None
        header_title, header_color = (f"🔧 {tool_name}", "wathet") if is_tool_use else (None, None)
        for binding in bindings:
            body = self._apply_verbosity(binding, block, text)
            outbound = Outbound(
                conversation=binding.conversation,
                content=CardContent(
                    card=Card(
                        text=body,
                        header_title=header_title,
                        header_color=header_color,
                        is_status_carrier=True,
                    )
                ),
            )
            future = self._outbox.enqueue_send(binding.person, outbound)
            if is_tool_use and block.tool_id:
                self._tool_anchors[self._anchor_key(binding, block.tool_id)] = (
                    future,
                    tool_name,
                )

    async def _dispatch_ask_user(self, bindings: list[Binding], block: Block) -> bool:
        """Render `AskUserQuestion` as a buttoned card, bypassing
        the generic JSON-blob render and verbosity truncation.
        Returns False if the input shape is unparseable (caller falls
        back to the generic path)."""
        if block.tool_id is None:
            return False
        questions = parse_ask_user_questions(block.text)
        if questions is None:
            return False
        card = build_ask_user_card(block.tool_id, questions)
        for binding in bindings:
            outbound = Outbound(
                conversation=binding.conversation,
                content=CardContent(card=card),
            )
            future = self._outbox.enqueue_send(binding.person, outbound)
            self._tool_anchors[self._anchor_key(binding, block.tool_id)] = (
                future,
                ASK_USER_TOOL_NAME,
            )
        return True

    async def _dispatch_tool_result(
        self,
        bindings: list[Binding],
        block: Block,
        text: str,
    ) -> None:
        if block.tool_id is None:
            return  # caller already filtered, but pyright likes the guard
        tool_id = block.tool_id
        for binding in bindings:
            body = self._apply_verbosity(binding, block, text)
            entry = self._tool_anchors.pop(self._anchor_key(binding, tool_id), None)
            future, tool_name = (entry[0], entry[1]) if entry is not None else (None, "tool")
            # AskUserQuestion's tool_use card lives under the `❓ `
            # header namespace (the buttoned-card path); its
            # tool_result edit is the final state of that flow, so
            # it stays in `❓ Answered` rather than jumping to
            # `🔧 AskUserQuestion`. Other tools all land under `🔧 `.
            header_title = "❓ Answered" if tool_name == ASK_USER_TOOL_NAME else f"🔧 {tool_name}"
            outbound = Outbound(
                conversation=binding.conversation,
                content=CardContent(
                    card=Card(
                        text=body,
                        header_title=header_title,
                        header_color="wathet",
                        is_status_carrier=True,
                    )
                ),
            )
            anchor = await self._await_anchor(future)
            if anchor is None:
                # No matching tool_use, or the send failed / had no
                # anchor (typing). Fall back to a fresh send.
                self._outbox.enqueue_send(binding.person, outbound)
                continue
            self._outbox.enqueue_edit(binding.person, anchor, outbound)

    def _apply_verbosity(self, binding: Binding, block: Block, text: str) -> str:
        kind = _verbosity_kind(block.kind)
        if kind is None:
            return text
        return self._verbosity.maybe_truncate(binding.person, binding.conversation, kind, text)

    @staticmethod
    async def _await_anchor(
        future: asyncio.Future[Anchor | None] | None,
    ) -> Anchor | None:
        if future is None:
            return None
        try:
            return await future
        except Exception as e:
            logger.debug("tool_use send failed; tool_result will fresh-send: %s", e)
            return None

    @staticmethod
    def _anchor_key(binding: Binding, tool_id: str) -> tuple[str, str, str, str]:
        return (
            binding.person.user_id,
            binding.conversation.chat_id,
            binding.conversation.thread_id or "",
            tool_id,
        )


# ── helpers ──────────────────────────────────────────────────────


_BLOCK_KIND_TO_CONTENT_KIND: dict[BlockKind, ContentKind] = {
    BlockKind.TEXT: ContentKind.TEXT,
    BlockKind.TOOL_USE: ContentKind.TOOL_USE,
    BlockKind.TOOL_RESULT: ContentKind.TOOL_RESULT,
    # THINKING is intentionally absent — it has its own rendering rules
    # and isn't user-toggleable on the verbosity card.
}


def _verbosity_kind(block_kind: BlockKind) -> ContentKind | None:
    return _BLOCK_KIND_TO_CONTENT_KIND.get(block_kind)


def render_block(block: Block) -> str | None:
    """Render one transcript block to a text body.

    Returns None when the block carries no body to show. Verbosity
    (BRIEF vs FULL) is layered on top by VerbosityService later — this
    renderer is the FULL form.
    """
    if block.kind is BlockKind.TEXT:
        return block.text or None
    if block.kind is BlockKind.THINKING:
        return f"💭 _{block.text}_" if block.text else None
    if block.kind is BlockKind.TOOL_USE:
        # The card header already carries `🔧 {tool_name}`, so the
        # body focuses on the *interesting* arg(s) — Bash's command,
        # Read's path, Edit's diff, etc. — via per-tool renderers
        # in `tool_renderers`. Unknown tools fall through to a
        # generic `**key**: value` pretty-print.
        return render_tool_use(block.tool_name or "tool", block.text or "")
    if block.kind is BlockKind.TOOL_RESULT:
        return block.text or None
    return None
