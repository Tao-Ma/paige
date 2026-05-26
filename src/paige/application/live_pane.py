"""LivePaneService — `/livepane` text-streamed view of the bound pane.

`/livepane` posts a card whose body is the captured pane text in a
fenced markdown code block, plus the same control-key grid the
`/screenshot` card uses. A background task re-captures the pane on
`_POLL_INTERVAL` and edits the card in place if the text changed.
Stops when the user taps 🛑 Stop, the pane disappears, the capture
returns empty for a few consecutive ticks, or paige shuts down.

Why text and not image:
- ANSI-stripped plain text from `pane.capture_pane()` renders fast
  and stays under Lark's 30 KB body cap for any realistic pane.
- No upload-image RTT per refresh; Feishu's per-group 5 QPS budget
  is much more comfortable.
- The content is searchable / copy-pasteable on the user's phone.

Quirks:
- Auto-skip when the captured text is identical to the last edit —
  saves a PATCH on every idle tick.
- One running loop per (anchor.message_id) — running `/livepane` a
  second time in the same conversation cancels the previous loop
  and starts fresh.
"""

from __future__ import annotations

import asyncio
import logging

from ..domain.card import Action, ActionEvent, Card, InputSlot
from ..domain.conversation import Anchor, Conversation
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound, TextContent
from ..domain.person import Person
from ..infrastructure.ansi_markdown import extract_highlights, strip_ansi
from ..infrastructure.terminal_parser import extract_interactive_content
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .message_seq import MessageSeqService
from .outbox import Outbox
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

ACTION_KEY = "lp:key"
ACTION_STOP = "lp:stop"
ACTION_DISMISS = "lp:dis"
ACTION_TEXT = "lp:txt"

UNBOUND_HINT = "No session bound to this conversation. Use /start to pick a directory."
PANE_GONE_HINT = "Pane is gone — its window must have been closed."
CAPTURE_FAILED_HINT = "Failed to capture pane content."

_POLL_INTERVAL_S = 1.5
_AUTO_REFRESH_DELAY_S = 0.2
# Stop the loop after this many consecutive empty captures — covers
# "the pane was closed mid-stream" without trusting a single None.
_EMPTY_CAPTURES_BEFORE_STOP = 3

# Footers that indicate the TUI is in a "select an option" prompt
# where typed characters are discarded — used to hide the input
# slot so the user doesn't lose text by typing into a list picker.
# Other states (idle text-input, AskUserQuestion's "Type something"
# follow-up, plain shell) leave the input enabled. Conservative:
# only hide on a KNOWN selection footer.
_SELECTION_PROMPT_MARKERS: tuple[str, ...] = (
    "Enter to select",
    "↑/↓ to navigate",
)
# Selection-mode option labels that suggest a free-text follow-up —
# typically Claude Code's catch-all "Type something." row. When the
# pane has `❯` highlighting an option whose label matches one of
# these, paige re-enables the input slot with "commit + type"
# semantics (submit prepends an Enter so the option commits before
# the text lands).
_TEXT_OPTION_MARKERS: tuple[str, ...] = (
    "type something",
    "free text",
    "other (please specify)",
)

# (tmux key name, label, literal) — same dict shape `/screenshot` uses.
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


class LivePaneService:
    """`/livepane` command + control-key + auto-poll handler."""

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
        # message_id of the live card → (task, last_text). The task
        # is cancelled on Stop, on shutdown, or when a fresh
        # /livepane in the same conversation takes over.
        self._loops: dict[str, asyncio.Task[None]] = {}
        self._last_text: dict[str, str] = {}
        # Binding key → anchor.message_id. Tracks loops that were
        # spawned by `start_for_binding` (auto-surfaced by
        # InteractiveUIService on AskUserQuestion detection) so the
        # detector can call `start_for_binding` idempotently each
        # tick and `stop_for_binding` when the overlay clears.
        # Distinct from `_loops` (keyed by anchor.message_id) so
        # user-invoked `/livepane` cards don't get auto-stopped.
        self._binding_anchors: dict[tuple[str, str, str], str] = {}

    def install(self, channel: Channel) -> None:
        channel.on_command("livepane", self._allow_list.guard_command(self._start))
        channel.on_action(self._allow_list.guard_action(self._handle_action))

    async def stop(self) -> None:
        """Cancel every running poll loop. Called from App.stop()."""
        tasks = list(self._loops.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loops.clear()
        self._last_text.clear()
        self._binding_anchors.clear()

    # ── public API for auto-spawn from upstream detectors ────────

    async def start_for_binding(self, person: Person, conversation: Conversation) -> None:
        """Spawn a live-pane card for `(person, conversation)` if one
        isn't already running for this binding. Used by
        `InteractiveUIService` to auto-surface the pane when an
        AskUserQuestion overlay is detected. Idempotent — safe to
        call on every detector tick. No-op when the binding is
        already serving a loop spawned by an earlier call OR when
        the binding has no bound pane."""
        key = _binding_key(person, conversation)
        if key in self._binding_anchors:
            return
        pane_id = self._registry.get_pane(person, conversation)
        if pane_id is None:
            return
        text = await self._multiplexer.capture_with_ansi(pane_id)
        if not text:
            return
        card = self._build_card(text, pane_id, header_suffix="")
        outbound = Outbound(
            conversation=conversation,
            content=CardContent(card=card),
        )
        anchor = await self._outbox.enqueue_send(person, outbound)
        if anchor is None:
            return
        self._last_text[anchor.message_id] = text
        self._binding_anchors[key] = anchor.message_id
        task = asyncio.create_task(
            self._poll_loop(anchor, pane_id, person.user_id),
            name=f"livepane:auto:{anchor.message_id[:12]}",
        )
        self._loops[anchor.message_id] = task

    async def stop_for_binding(self, person: Person, conversation: Conversation) -> None:
        """Stop the auto-spawned loop for `(person, conversation)`,
        if any. Only affects loops registered via `start_for_binding`
        — user-invoked `/livepane` cards (tracked solely under
        `_loops` by anchor.message_id) keep running, since the user
        explicitly asked for them and uses Stop/Dismiss to end."""
        key = _binding_key(person, conversation)
        anchor_id = self._binding_anchors.pop(key, None)
        if anchor_id is None:
            return
        task = self._loops.pop(anchor_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._last_text.pop(anchor_id, None)

    # ── /livepane ────────────────────────────────────────────────

    async def _start(self, inbound: Inbound, _arg: str) -> None:
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            self._send_text(inbound, UNBOUND_HINT)
            return
        text = await self._multiplexer.capture_with_ansi(pane_id)
        if not text:
            self._send_text(inbound, CAPTURE_FAILED_HINT)
            return
        card = self._build_card(text, pane_id, header_suffix="")
        outbound = Outbound(
            conversation=inbound.conversation,
            content=CardContent(card=card),
        )
        anchor_future = self._outbox.enqueue_send(inbound.sender, outbound)
        anchor = await anchor_future
        if anchor is None:
            logger.warning("/livepane: enqueue_send returned no anchor")
            return
        self._last_text[anchor.message_id] = text
        task = asyncio.create_task(
            self._poll_loop(anchor, pane_id, inbound.sender.user_id),
            name=f"livepane:{anchor.message_id[:12]}",
        )
        self._loops[anchor.message_id] = task

    # ── poll loop ────────────────────────────────────────────────

    async def _poll_loop(self, anchor: Anchor, pane_id: str, sender_uid: str) -> None:
        del sender_uid  # held for future per-sender bookkeeping
        empty_streak = 0
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL_S)
                text = await self._multiplexer.capture_with_ansi(pane_id)
                if not text:
                    empty_streak += 1
                    if empty_streak >= _EMPTY_CAPTURES_BEFORE_STOP:
                        logger.info(
                            "/livepane: %d consecutive empty captures for %s — stopping",
                            empty_streak,
                            pane_id,
                        )
                        await self._finalize_card(anchor, reason="pane gone")
                        return
                    continue
                empty_streak = 0
                last = self._last_text.get(anchor.message_id, "")
                if text == last:
                    continue  # nothing changed, skip the PATCH
                await self._edit_card(anchor, pane_id, text)
                self._last_text[anchor.message_id] = text
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("/livepane poll loop crashed for %s", pane_id)
        finally:
            self._loops.pop(anchor.message_id, None)

    async def _edit_card(self, anchor: Anchor, pane_id: str, text: str) -> None:
        card = self._build_card(text, pane_id, header_suffix="")
        outbound = Outbound(
            conversation=anchor.conversation,
            content=CardContent(card=card),
        )
        await self._channel.edit(anchor, outbound)

    async def _finalize_card(self, anchor: Anchor, *, reason: str) -> None:
        """Tag the card body with a 'stopped' footer and stop polling.
        Keeps the last-seen pane text visible so the user has a
        record of why the loop ended."""
        last = self._last_text.get(anchor.message_id, "")
        card = self._build_card(last, pane_id="", header_suffix=f" · stopped ({reason})")
        outbound = Outbound(
            conversation=anchor.conversation,
            content=CardContent(card=card),
        )
        try:
            await self._channel.edit(anchor, outbound)
        except Exception:
            logger.debug("/livepane finalize edit failed (anchor likely stale)")

    # ── action handler ───────────────────────────────────────────

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id == ACTION_STOP:
            await self._on_stop(event)
            return
        if event.action_id == ACTION_DISMISS:
            await self._on_dismiss(event)
            return
        if event.action_id == ACTION_TEXT:
            await self._on_text(event)
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
        ok = await self._multiplexer.send_keys(pane_id, tmux_key, enter=False, literal=literal)
        if not ok:
            logger.warning("/livepane key %s: send_keys failed for %s", key_id, pane_id)
            await self._channel.ack(event, "Send failed")
            return
        # Auto-refresh the card immediately so the user sees the
        # post-keypress state without waiting for the next poll
        # tick. Rides the inline-card-refresh slot so the swap is
        # atomic with the click ack.
        await asyncio.sleep(_AUTO_REFRESH_DELAY_S)
        text = await self._multiplexer.capture_with_ansi(pane_id)
        if text:
            card = self._build_card(text, pane_id, header_suffix="")
            outbound = Outbound(
                conversation=event.conversation,
                content=CardContent(card=card),
            )
            outbound, _ = self._message_seq.stamp_edit(
                event.sender, event.conversation, event.card_anchor, outbound
            )
            await self._channel.edit(event.card_anchor, outbound)
            self._last_text[event.card_anchor.message_id] = text
        await self._channel.ack(event, label)

    async def _on_stop(self, event: ActionEvent) -> None:
        anchor_id = event.card_anchor.message_id
        task = self._loops.pop(anchor_id, None)
        if task is not None and not task.done():
            task.cancel()
        await self._finalize_card(event.card_anchor, reason="stopped")
        await self._channel.ack(event, "🛑 Stopped")

    async def _on_text(self, event: ActionEvent) -> None:
        """InputSlot submit handler. Normally sends `<text><Enter>`
        to the pane via tmux send_keys. When the card was rendered
        with `commit_first=1` (a selection prompt had a "Type
        something"-style option highlighted), prepends an Enter so
        the option commits and the TUI transitions to text-input
        before the typed text lands. After the keys land, re-capture
        the pane so the post-send TUI state appears immediately."""
        text = event.value.get("_input", "").strip()
        pane_id = event.value.get("p", "")
        commit_first = event.value.get("commit_first", "0") == "1"
        if not text:
            await self._channel.ack(event, "Empty text")
            return
        if not pane_id:
            await self._channel.ack(event, "Invalid pane")
            return
        if commit_first:
            # Commit the highlighted option first (Enter), then a
            # brief pause so the TUI redraws into the new state
            # before we feed the text.
            ok = await self._multiplexer.send_keys(pane_id, "Enter", enter=False, literal=False)
            if not ok:
                logger.warning("/livepane commit-first Enter failed for %s", pane_id)
                await self._channel.ack(event, "Send failed")
                return
            await asyncio.sleep(_AUTO_REFRESH_DELAY_S)
        ok = await self._multiplexer.send_keys(pane_id, text, enter=True, literal=True)
        if not ok:
            logger.warning("/livepane text: send_keys failed for %s", pane_id)
            await self._channel.ack(event, "Send failed")
            return
        await asyncio.sleep(_AUTO_REFRESH_DELAY_S)
        captured = await self._multiplexer.capture_with_ansi(pane_id)
        if captured:
            card = self._build_card(captured, pane_id, header_suffix="")
            outbound = Outbound(
                conversation=event.conversation,
                content=CardContent(card=card),
            )
            outbound, _ = self._message_seq.stamp_edit(
                event.sender, event.conversation, event.card_anchor, outbound
            )
            await self._channel.edit(event.card_anchor, outbound)
            self._last_text[event.card_anchor.message_id] = captured
        await self._channel.ack(event, "Sent")

    async def _on_dismiss(self, event: ActionEvent) -> None:
        """Stop polling + remove the card entirely. The `Stop`
        button keeps the card visible as scrollback; `Dismiss`
        is the "I'm done, hide it" button."""
        anchor_id = event.card_anchor.message_id
        task = self._loops.pop(anchor_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._last_text.pop(anchor_id, None)
        await self._channel.ack(event, "✕ Dismissed")
        try:
            await self._channel.delete(event.card_anchor)
        except Exception:
            logger.debug("/livepane dismiss delete failed (anchor likely stale)")

    # ── card rendering ───────────────────────────────────────────

    def _build_card(self, text: str, pane_id: str, *, header_suffix: str) -> Card:
        # `text` is the ANSI-preserved capture. The body itself is
        # rendered as a monospace code block (after ANSI stripping)
        # so TUI column alignment survives Lark's narrow card
        # width. ANSI background-color highlights — Claude Code's
        # active-tab indicator and similar — are surfaced as a
        # small annotation ABOVE the code block via
        # `extract_highlights`, so the styling cue isn't lost.
        # `force_no_collapse=True` keeps the body expanded across
        # PATCHes even if the user's per-topic collapse pref is set.
        #
        # Three render modes for the input slot:
        # - selection mode, non-text option highlighted → hide input
        #   (typed chars would be discarded by the picker)
        # - selection mode, "Type something"-style highlighted →
        #   show input with commit-first semantics: submit prepends
        #   Enter so the option commits and the TUI transitions to
        #   text-input before the typed text lands
        # - non-selection (idle text input) → show input, plain
        #   `<text><Enter>` submit
        plain = strip_ansi(text)
        # Lift Claude Code's ANSI-background-color active-tab
        # indicator into a visible glyph. The TUI highlights the
        # active tab via background color (lost when ANSI is
        # stripped); we use `extract_highlights` to find which
        # bg-color span corresponds to the active tab and rewrite
        # its leading `☐` to `☒` so the trimmed code block carries
        # the active-state cue.
        plain = _mark_active_tab(text, plain)
        # Trim the pane scrollback to just the current interactive
        # overlay (AskUserQuestion / BashApproval / ExitPlanMode /
        # RestoreCheckpoint / Settings) when one is detected. The
        # earlier history is already in the chat surface (paige's
        # JSONL forwarder), so duplicating it in the live-pane
        # card adds noise without adding info. The start line is
        # whatever `_UIPattern.top` matches — e.g. the
        # `←  ☐ ... →` tab strip for a multi-tab AskUserQuestion —
        # and content extends to the prompt's footer.
        #
        # Plan-mode context: if the pane carries a
        # `Planning: <path>` line above the overlay, prepend it so
        # the card surfaces which plan the question is anchored to
        # — that's the one bit of pre-overlay context worth keeping
        # (multi-session juggling otherwise loses the anchor).
        #
        # No overlay detected → show the full pane (idle shell,
        # mid-stream assistant output, etc.).
        ui = extract_interactive_content(plain)
        if ui is not None:
            plan_line = _find_plan_line(plain)
            body_text = f"{plan_line}\n\n{ui.content}" if plan_line else ui.content
        else:
            body_text = plain
        body = f"```\n{body_text.rstrip()}\n```"
        in_selection = _has_selection_prompt(plain)
        commit_first = in_selection and _highlighted_is_text_option(plain)
        if in_selection and not commit_first:
            inputs: tuple[InputSlot, ...] = ()
            mode_tag = " · select (⏎ to pick)"
        else:
            inputs = _build_inputs(pane_id, commit_first=commit_first)
            mode_tag = " · commit + type" if commit_first else " · type"
        return Card(
            text=body,
            header_title=f"🖥 live pane{header_suffix or mode_tag}",
            header_color="wathet",
            inputs=inputs,
            rows=_build_rows(pane_id),
            is_status_carrier=True,
            force_no_collapse=True,
        )

    # ── helpers ──────────────────────────────────────────────────

    def _send_text(self, inbound: Inbound, text: str) -> None:
        outbound = Outbound(
            conversation=inbound.conversation,
            content=TextContent(text),
        )
        self._outbox.enqueue_send(inbound.sender, outbound)


def _has_selection_prompt(text: str) -> bool:
    """Best-effort: True when the pane text contains a known
    selection-prompt footer (arrows-to-navigate, Enter-to-select).
    False otherwise — including unknown states, so the input slot
    stays available unless we explicitly recognize a "typing would
    be lost here" prompt."""
    return any(marker in text for marker in _SELECTION_PROMPT_MARKERS)


def _highlighted_is_text_option(text: str) -> bool:
    """Return True when a selection-mode pane has the highlight
    (`❯`) on an option whose label matches one of the known free-
    text "catch-all" markers — Claude Code's "Type something." row
    being the canonical case. Means an Enter would commit that
    option and (probably) transition into a text-input state, so
    the live-pane card can show the input slot with
    commit-first semantics.

    Claude Code may render multiple `❯` markers in a turn (the
    user's submitted prompt is also prefixed with `❯`); we scan
    every `❯` line and look specifically for `<num>. <label>` —
    only numbered option rows count, which excludes the prompt
    line and any other `❯`-flavored leader."""
    for raw_line in text.splitlines():
        line = raw_line.lstrip()
        if not line.startswith("❯ "):
            continue
        rest = line[2:].lstrip()
        # Numbered-option row: digits + "." + space + label.
        n_end = 0
        while n_end < len(rest) and rest[n_end].isdigit():
            n_end += 1
        if n_end == 0 or n_end >= len(rest) or rest[n_end] != ".":
            continue
        label = rest[n_end + 1 :].strip().lower()
        if any(marker in label for marker in _TEXT_OPTION_MARKERS):
            return True
    return False


def _build_inputs(pane_id: str, *, commit_first: bool) -> tuple[InputSlot, ...]:
    """Free-text slot. Submit normally sends `<text><Enter>` to the
    pane via tmux send_keys. With `commit_first=True`, an extra
    Enter is sent first — used when the TUI is in a selection
    picker with a "Type something" option highlighted, so tapping
    Send commits the option AND types the follow-up text in one
    action. Empty `pane_id` (finalized card) suppresses the input."""
    if not pane_id:
        return ()
    placeholder = "commit option + type…" if commit_first else "type to send to pane…"
    return (
        InputSlot(
            label="✏️",
            default_value="",
            action_id=ACTION_TEXT,
            value={"p": pane_id, "commit_first": "1" if commit_first else "0"},
            placeholder=placeholder,
            submit_label="Send",
        ),
    )


def _build_rows(pane_id: str) -> tuple[tuple[Action, ...], ...]:
    """Same 3×3 control grid the /screenshot card uses, then a row
    with Stop (freeze loop, keep card visible) and Dismiss (freeze
    loop, delete card). Empty `pane_id` (finalized card) emits no
    buttons — the loop has stopped and the keys would no longer
    route anywhere."""
    if not pane_id:
        return ()

    def b(key_id: str) -> Action:
        _, label, _ = _KEYS[key_id]
        return Action(
            label=label,
            action_id=ACTION_KEY,
            value={"k": key_id, "p": pane_id},
        )

    stop = Action(
        label="🛑 Stop",
        action_id=ACTION_STOP,
        value={"p": pane_id},
    )
    dismiss = Action(
        label="✕ Dismiss",
        action_id=ACTION_DISMISS,
        value={"p": pane_id},
    )
    return (
        (b("spc"), b("up"), b("tab")),
        (b("lt"), b("dn"), b("rt")),
        (b("esc"), b("cc"), b("ent")),
        (stop, dismiss),
    )


def _mark_active_tab(ansi_text: str, plain_text: str) -> str:
    """Rewrite the active-tab indicator from Claude Code's ANSI
    background color into a visible glyph swap (`☐` → `☒`) in the
    plain text.

    Background: in a multi-tab AskUserQuestion Claude Code emits
    the strip as `←  ☐ Tab1  ☐ Tab2  ✔ Submit  →` with the active
    tab wrapped in an ANSI bg color (CSI `48;…m`). Once ANSI is
    stripped every checkbox is `☐`, losing the cue. We use the
    `extract_highlights` view of the ANSI text to find each
    bg-color region's plain text and rewrite a leading `☐` in
    that exact substring to `☒` in the plain rendering. Idempotent
    for non-tab highlights (e.g. the user's prompt line — no `☐`
    to swap)."""
    highlights = extract_highlights(ansi_text)
    for h in highlights:
        if "☐" not in h:
            continue
        plain_text = plain_text.replace(h, h.replace("☐", "☒", 1), 1)
    return plain_text


def _find_plan_line(text: str) -> str | None:
    """Return the `Planning: <path>` context line if the pane has
    one — Claude Code emits it inside plan-mode AskUserQuestion
    overlays just above the tab strip. Used by /livepane to
    prepend the plan anchor to the trimmed overlay body so the
    user can tell which plan the question belongs to. Returns
    None outside plan mode."""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("Planning:"):
            return stripped
    return None


def _binding_key(person: Person, conversation: Conversation) -> tuple[str, str, str]:
    """Same shape as `RunRegistry`'s binding-key discriminator —
    `(user_id, chat_id, topic_id-or-thread_id-or-empty)`. Used by
    `start_for_binding` / `stop_for_binding` so a per-binding loop
    can be looked up without going through the anchor."""
    discriminator = conversation.topic_id or conversation.thread_id or ""
    return (person.user_id, conversation.chat_id, discriminator)


__all__ = ["ACTION_DISMISS", "ACTION_KEY", "ACTION_STOP", "ACTION_TEXT", "LivePaneService"]
