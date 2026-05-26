"""EndTurnPanelService — render the "ready, pick or type" panel.

Subscribes to `ReadinessService` transitions. On a READY transition
for a given run, posts one panel per binding to that run; on a
NOT_READY transition, edits the panel(s) to a "🟡 Working…" header
so the user knows their next click would be queued.

Panel shape — 4 editable input rows:
  - slot 0/1/2 — pre-filled from `QuickReplyPrefs`. Edits land
    back via `update(slot, text)` so the next panel shows the
    user's most recent phrasing.
  - free      — empty placeholder. Free-form next prompt; nothing
    is saved.

Each row has its own Send button; clicking submits the row's text
to the bound pane via the existing `Multiplexer.send_keys` path
(with `EchoDedup.record` so the prompt doesn't bounce back through
JSONL as if the user had typed it directly).
"""

from __future__ import annotations

import asyncio
import logging

from ..domain.card import Card, InputSlot
from ..domain.conversation import Anchor
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound
from ..domain.pane import Binding
from ..domain.transcript import BlockKind, Role, TranscriptEvent
from ..infrastructure.terminal_parser import extract_prompt_suggestion
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .echo_dedup import EchoDedup
from .outbox import Outbox
from .quick_reply_prefs import SLOT_COUNT, QuickReplyPrefs
from .readiness import ReadinessService
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)


ACTION_SLOT = "ready:slot"  # value carries {"slot": "N"}
ACTION_FREE = "ready:free"
ACTION_ACCEPT = "ready:accept"  # value carries {"text": <ghost suggestion>}

# The ghost suggestion can lag the end_turn record — poll a few times
# after sending the panel to catch it, then give up (no button).
_GHOST_POLL_INTERVAL_S = 0.5
_GHOST_POLL_TRIES = 6


class EndTurnPanelService:
    """Fires a quick-reply panel on every `end_turn` for each
    binding on the run that finished, and morphs / removes it as
    state changes."""

    def __init__(
        self,
        *,
        channel: Channel,
        registry: RunRegistry,
        outbox: Outbox,
        multiplexer: Multiplexer,
        echo_dedup: EchoDedup,
        readiness: ReadinessService,
        quick_reply: QuickReplyPrefs,
        allow_list: AllowList,
    ) -> None:
        self._channel = channel
        self._registry = registry
        self._outbox = outbox
        self._multiplexer = multiplexer
        self._echo_dedup = echo_dedup
        self._readiness = readiness
        self._quick_reply = quick_reply
        self._allow_list = allow_list
        # (binding key) → most recent panel anchor. Kept alive across
        # panel ↔ receipt morphs so live status (`_render_working`)
        # always has an anchor to edit. Cleared only when the next
        # `_send_panel` issues a fresh anchor (a new turn boundary).
        self._anchors: dict[tuple[str, str, str], Anchor] = {}
        # (binding key) → "> sent text" body to keep displayed while
        # the panel is in receipt-shape. Presence of this entry is
        # how `_morph_to_working` knows the panel is in receipt
        # state (and skips the morph — receipt header stays
        # "✓ Sent", live status comes via the carrier badge).
        # Cleared on `_send_panel`.
        self._receipt_text: dict[tuple[str, str, str], str] = {}

    def install(self) -> None:
        self._readiness.on_change(self._on_readiness_change)
        self._channel.on_action(self._allow_list.guard_action(self._handle_action))

    async def _on_readiness_change(
        self,
        run_id: str,
        ready: bool,
        event: TranscriptEvent,
    ) -> None:
        bindings = self._registry.find_bindings_for_run(run_id)
        if not bindings:
            return
        for binding in bindings:
            if ready:
                # Fire-and-forget so this handler returns before the
                # dispatcher's handler runs on the SAME end_turn
                # event. Otherwise the panel reaches Lark before the
                # assistant text — the watcher fans out handlers
                # sequentially, ReadinessService is registered first,
                # and `_send_panel` awaits the outbox future to
                # completion before returning. Scheduling on the
                # event loop lets the dispatcher enqueue its text
                # card first; the panel queues after. Trade-off:
                # `self._anchors[binding]` isn't populated
                # synchronously, so a NOT_READY transition that
                # races within ~100ms of READY would miss the
                # morph. Acceptable — claude doesn't emit another
                # event that fast in practice.
                asyncio.create_task(self._send_panel(binding, run_id))
                continue
            # NOT_READY: morph differently based on what triggered it.
            #   USER text → the user just typed in tmux (or paige
            #     itself just submitted via panel — the
            #     `_forward_to_pane` path normally pops the anchor
            #     first, so this branch fires only for tmux-typed).
            #     Morph to a "✓ Sent" receipt with the typed text so
            #     the panel surface mirrors the panel-submit shape.
            #   tool_use / anything else → mid agent loop, still
            #     queuing-capable. Keep the inputs visible under a
            #     "🟡 Working…" header.
            user_text = _user_text_from_event(event)
            if user_text is not None:
                await self._morph_to_sent_receipt(binding, user_text)
            else:
                await self._morph_to_working(binding)

    def _ready_card(self, binding: Binding, suggestion: str | None) -> Card:
        """Build the 🟢 Ready panel. When `suggestion` is present (the
        grey ghost prompt Claude shows, optimized for "just hit
        Enter"), it renders as the first input box — see
        `_build_inputs`. Absent → byte-identical to the plain shape."""
        return Card(
            text=" ",
            inputs=self._build_inputs(binding, suggestion),
            header_title="🟢 Ready — pick or type",
            header_color="green",
            is_status_carrier=True,
        )

    async def _send_panel(self, binding: Binding, run_id: str) -> None:
        # Fresh panel → reset receipt-state tracking. New anchor
        # will be stashed below once the outbox future resolves.
        key = _binding_key(binding)
        self._receipt_text.pop(key, None)
        # Scrape the ghost suggestion now — covers the case where it's
        # already on screen. Claude often renders it a beat *after* the
        # end_turn JSONL record, though, so if it's absent we send the
        # card immediately (snappy) and poll briefly to patch the
        # Accept button in once the ghost materializes (see below).
        suggestion = await self._read_suggestion(binding)
        outbound = Outbound(
            conversation=binding.conversation,
            content=CardContent(card=self._ready_card(binding, suggestion)),
        )
        future = self._outbox.enqueue_send(binding.person, outbound)
        # Stash the anchor so the NOT_READY transition can morph
        # this panel in place. Awaiting the future would serialize
        # us behind the outbox queue; instead, capture the anchor
        # opportunistically and skip the morph if the send hasn't
        # completed by the time we'd need it.
        try:
            anchor = await future
        except Exception as e:
            logger.debug("end_turn panel send failed: %s", e)
            return
        if anchor is not None:
            self._anchors[key] = anchor
            if suggestion is None:
                asyncio.create_task(self._poll_for_suggestion(binding, run_id, anchor))

    async def _poll_for_suggestion(self, binding: Binding, run_id: str, anchor: Anchor) -> None:
        """The ghost suggestion can lag the end_turn record (Claude
        renders it shortly after). Re-scrape on a short cadence and, on
        the first hit, PATCH the live Ready card to add the Accept
        button. Bail the moment the panel is no longer a fresh ready
        panel: claude resumed (NOT_READY), the user already acted (the
        panel turned into a receipt), or a newer panel took the anchor.
        """
        key = _binding_key(binding)
        for _ in range(_GHOST_POLL_TRIES):
            await asyncio.sleep(_GHOST_POLL_INTERVAL_S)
            if not self._readiness.is_ready(run_id):
                return  # claude started working / a reply landed
            if key in self._receipt_text:
                return  # user already submitted — panel is a receipt now
            if self._anchors.get(key) is not anchor:
                return  # a newer panel owns this binding
            suggestion = await self._read_suggestion(binding)
            if not suggestion:
                continue
            outbound = Outbound(
                conversation=binding.conversation,
                content=CardContent(card=self._ready_card(binding, suggestion)),
            )
            self._outbox.enqueue_edit(binding.person, anchor, outbound)
            return

    async def _read_suggestion(self, binding: Binding) -> str | None:
        """Capture the pane *with ANSI* and pull out Claude Code's
        grey ghost prompt suggestion, or None. Best-effort — any
        capture failure or unrecognized styling degrades to None (no
        Accept button), never blocks the panel."""
        try:
            ansi = await self._multiplexer.capture_with_ansi(binding.pane_id)
        except Exception as e:
            logger.debug("ghost-suggestion capture failed: %s", e)
            return None
        if not ansi:
            return None
        return extract_prompt_suggestion(ansi)

    async def _morph_to_sent_receipt(self, binding: Binding, text: str) -> None:
        """Edit the panel anchor (if any) to a `✓ Sent` receipt with
        the user's text. Mirrors the post-panel-submit shape so the
        chat surface is symmetric whether the input came via the
        panel or via direct typing in tmux. Pops the stash so the
        next NOT_READY transition (a follow-up tool_use, for
        instance) doesn't re-morph back to "🟡 Working…".

        Also `echo_dedup.record(pane_id, text)` so the dispatcher's
        subsequent USER-event handler trips its `_all_echos` check
        and skips emitting a standalone text card — without this we
        get both a panel-receipt AND a duplicate plain-text card
        for the same tmux-typed message. Works because the watcher
        fans out events to handlers sequentially in registration
        order and ReadinessService is registered before the
        Dispatcher (see entrypoint/app.py).
        """
        binding_key = _binding_key(binding)
        anchor = self._anchors.get(binding_key)
        if anchor is None:
            return
        self._echo_dedup.record(binding.pane_id, text)
        # Stash receipt context so `_morph_to_working` knows we're
        # in receipt-shape (and skips its yellow-header morph; the
        # status badge from StatusCarrierService is the live-state
        # surface from here on).
        self._receipt_text[binding_key] = text
        receipt = Card(
            text=f"> {text}",
            header_title="✓ Sent",
            header_color="green",
            is_status_carrier=True,
        )
        outbound = Outbound(
            conversation=binding.conversation,
            content=CardContent(card=receipt),
        )
        self._outbox.enqueue_edit(binding.person, anchor, outbound)

    async def _morph_to_working(self, binding: Binding) -> None:
        """Morph the panel anchor (if any) to "working" state. The
        live spinner text is now carried by `StatusCarrierService`
        as a footer badge that migrates to the most recent card, so
        this header is just the coarse "claude is working" signal
        (no live `Worked Ns` data in the header itself).

        Re-render the same inputs (panel shape) or keep the receipt
        body (receipt shape), depending on the current state. Edits
        made now still submit normally — claude queues them; the
        header just signals that the next click would land
        mid-turn."""
        key = _binding_key(binding)
        anchor = self._anchors.get(key)
        if anchor is None:
            return
        sent_text = self._receipt_text.get(key)
        if sent_text is not None:
            # Receipt-shape: header stays "✓ Sent" (semantic record
            # of what was dispatched); the status badge on the
            # carrier conveys "still working". No morph needed
            # here — return early to avoid overwriting the receipt's
            # green check header with a yellow working banner.
            return
        card = Card(
            text=" ",
            inputs=self._build_inputs(binding),
            header_title="🟡 Working…",
            header_color="yellow",
            is_status_carrier=True,
        )
        outbound = Outbound(
            conversation=binding.conversation,
            content=CardContent(card=card),
        )
        self._outbox.enqueue_edit(binding.person, anchor, outbound)

    def _build_inputs(
        self, binding: Binding, suggestion: str | None = None
    ) -> tuple[InputSlot, ...]:
        """Build the input boxes for any panel state. Order, top to
        bottom: the ghost-suggestion box (only when `suggestion` is
        present), then the 1/2/3 quick-reply slots, then the free
        "type your next message" box last. Each box is pre-fillable +
        editable with its own Send; same shape across ready/working
        so morphing in place doesn't blank in-flight edits.

        The suggestion box is pre-filled with Claude's grey ghost
        prompt; tapping its Send is the card analog of "just hit
        Enter", and it stays editable for a quick tweak first."""
        inputs: list[InputSlot] = []
        if suggestion:
            inputs.append(
                InputSlot(
                    label="💡 Suggested",
                    default_value=suggestion,
                    action_id=ACTION_ACCEPT,
                    placeholder="edit + Send",
                    submit_label="Send",
                )
            )
        defaults = self._quick_reply.get(binding.person, binding.conversation)
        for i in range(SLOT_COUNT):
            inputs.append(
                InputSlot(
                    label=f"{i + 1}",
                    default_value=defaults[i],
                    action_id=ACTION_SLOT,
                    value={"slot": str(i)},
                    placeholder="edit + Send",
                    submit_label="Send",
                )
            )
        inputs.append(
            InputSlot(
                label="✏️",
                default_value="",
                action_id=ACTION_FREE,
                placeholder="type your next message…",
                submit_label="Send",
            )
        )
        return tuple(inputs)

    async def _handle_action(self, event: object) -> None:
        # Local import to avoid widening the module's import graph
        # (and to keep this method easy to drop into other services).
        from ..domain.card import ActionEvent

        if not isinstance(event, ActionEvent):
            return
        if event.action_id == ACTION_SLOT:
            await self._on_slot_submit(event)
        elif event.action_id == ACTION_FREE:
            await self._on_free_submit(event)
        elif event.action_id == ACTION_ACCEPT:
            await self._on_accept_submit(event)

    async def _on_slot_submit(self, event: object) -> None:
        from ..domain.card import ActionEvent  # noqa: F811 — same reason as above

        assert isinstance(event, ActionEvent)
        text = event.value.get("_input", "").strip()
        slot_raw = event.value.get("slot", "")
        try:
            slot = int(slot_raw)
        except ValueError:
            await self._channel.ack(event, "Invalid slot")
            return
        if not 0 <= slot < SLOT_COUNT:
            await self._channel.ack(event, "Invalid slot")
            return
        if not text:
            await self._channel.ack(event, "Empty text")
            return
        if await self._try_dispatch_command(event, text):
            # Don't save commands as a quick-reply default — the slot
            # is for prompts, not control-plane shortcuts.
            return
        # Save the (possibly edited) text as the next default for
        # this slot. The next panel renders with this value.
        self._quick_reply.update(event.sender, event.conversation, slot, text)
        await self._forward_to_pane(event, text)

    async def _on_free_submit(self, event: object) -> None:
        from ..domain.card import ActionEvent  # noqa: F811

        assert isinstance(event, ActionEvent)
        text = event.value.get("_input", "").strip()
        if not text:
            await self._channel.ack(event, "Empty text")
            return
        if await self._try_dispatch_command(event, text):
            return
        await self._forward_to_pane(event, text)

    async def _on_accept_submit(self, event: object) -> None:
        """Send the ghost-suggestion box — the card analog of accepting
        Claude's grey prompt by hitting Enter. It's an input box, so
        the (possibly edited) text arrives as `_input`. Unlike the
        quick slots, the accepted text isn't saved as a reply default
        — it's a one-shot suggestion. Routes through the same
        command-intercept + `_forward_to_pane` path the slots use."""
        from ..domain.card import ActionEvent  # noqa: F811

        assert isinstance(event, ActionEvent)
        text = event.value.get("_input", "").strip()
        if not text:
            await self._channel.ack(event, "Empty suggestion")
            return
        if await self._try_dispatch_command(event, text):
            return
        await self._forward_to_pane(event, text)

    async def _try_dispatch_command(self, event: object, text: str) -> bool:
        """If `text` looks like `/<name> [arg]` and the channel has
        a handler for `name`, dispatch it as if the user had typed
        the command directly. Morph the panel to the same `✓ Sent`
        receipt shape used by tmux-forwarded submits so the chat
        surface stays consistent. Returns True when intercepted.

        Falls back to False (caller forwards to tmux) when:
          - text doesn't start with `/`, OR
          - the parsed name isn't a registered command.
        That preserves the user's ability to send a literal `/...`
        line into Claude's prompt by typing a command name claude
        cares about but paige doesn't.
        """
        from ..domain.card import ActionEvent  # noqa: F811

        assert isinstance(event, ActionEvent)
        split = _split_command(text)
        if split is None:
            return False
        name, arg = split
        inbound = Inbound(
            sender=event.sender,
            conversation=event.conversation,
            text=text,
            message_id=event.card_anchor.message_id,
        )
        dispatched = await self._channel.dispatch_command(inbound, name, arg)
        if not dispatched:
            return False
        await self._channel.ack(event, f"Sent /{name}")
        await self._morph_to_command_receipt(event, text)
        return True

    async def _morph_to_command_receipt(self, event: object, text: str) -> None:
        from ..domain.card import ActionEvent  # noqa: F811

        assert isinstance(event, ActionEvent)
        receipt = Card(
            text=f"> {text}",
            header_title="✓ Sent",
            header_color="green",
            is_status_carrier=True,
        )
        outbound = Outbound(
            conversation=event.conversation,
            content=CardContent(card=receipt),
        )
        self._outbox.enqueue_edit(event.sender, event.card_anchor, outbound)
        binding_key = (
            event.sender.user_id,
            event.conversation.chat_id,
            event.conversation.thread_id or "",
        )
        self._receipt_text[binding_key] = text

    async def _forward_to_pane(self, event: object, text: str) -> None:
        from ..domain.card import ActionEvent  # noqa: F811

        assert isinstance(event, ActionEvent)
        pane_id = self._registry.get_pane(event.sender, event.conversation)
        if pane_id is None:
            await self._channel.ack(event, "Not bound — use /sessions")
            return
        # Same shape as `Dispatcher._handle_inbound`: record the
        # echo so the JSONL user-event doesn't bounce back, then
        # send_keys with literal=True.
        self._echo_dedup.record(pane_id, text)
        ok = await self._multiplexer.send_keys(pane_id, text, enter=True, literal=True)
        if not ok:
            await self._channel.ack(event, "send_keys failed")
            return
        await self._channel.ack(event, "Sent")
        # Morph the panel anchor to a compact receipt — keeps the
        # chat surface tight (no stale 4-input form sitting there
        # under a frozen "🟢 Ready" header) and gives the user a
        # clear record of what was dispatched. Drop the stashed
        # anchor so the NOT_READY transition that follows (when the
        # JSONL user-event lands) doesn't morph our fresh receipt
        # back to "🟡 Working…"; the next READY will send a fresh
        # panel under its own anchor anyway. Anchor is *kept* in
        # `self._anchors` and `_receipt_text` is set so
        # `update_working_status` ticks can morph the receipt's
        # header through the working states without losing the
        # `> sent text` body.
        receipt = Card(
            text=f"> {text}",
            header_title="✓ Sent",
            header_color="green",
            is_status_carrier=True,
        )
        outbound = Outbound(
            conversation=event.conversation,
            content=CardContent(card=receipt),
        )
        self._outbox.enqueue_edit(event.sender, event.card_anchor, outbound)
        binding_key = (
            event.sender.user_id,
            event.conversation.chat_id,
            event.conversation.thread_id or "",
        )
        self._receipt_text[binding_key] = text


def _binding_key(binding: Binding) -> tuple[str, str, str]:
    return (
        binding.person.user_id,
        binding.conversation.chat_id,
        binding.conversation.thread_id or "",
    )


def _split_command(text: str) -> tuple[str, str] | None:
    """If `text` looks like `/<name> [arg]`, return `(name, arg)`;
    otherwise None. Local copy of the same split used by the feishu
    adapter — kept here so the application layer doesn't import an
    adapter. Strips a single `@suffix` after the name to match the
    adapter's group-mention handling."""
    s = text.lstrip()
    if not s.startswith("/"):
        return None
    head, _, rest = s.partition(" ")
    name = head[1:]
    if "@" in name:
        name = name.split("@", 1)[0]
    if not name:
        return None
    return name, rest.strip()


def _user_text_from_event(event: TranscriptEvent) -> str | None:
    """Return the user's typed text from a USER-role event with at
    least one TEXT block, or None when this isn't a tmux-typed user
    message. Tool-result blocks (the agent-loop continuation that
    also arrives as role=USER) are filtered out — they have no
    TEXT block, only TOOL_RESULT."""
    if event.role is not Role.USER:
        return None
    for block in event.blocks:
        if block.kind is BlockKind.TEXT and block.text.strip():
            return block.text.strip()
    return None


__all__ = ["ACTION_ACCEPT", "ACTION_FREE", "ACTION_SLOT", "EndTurnPanelService"]
