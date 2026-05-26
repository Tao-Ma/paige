"""InteractiveUIService — pane-scrape detection of Claude Code's
interactive TUI overlays + button-driven keystroke synthesis.

Covers the overlays Claude Code surfaces in-terminal that never reach
the JSONL: permission prompts (`Do you want to proceed?`), bash
approval frames, ExitPlanMode confirmations, RestoreCheckpoint
pickers, and the Settings palette. AskUserQuestion is also detected
here as a fallback, but yields to `paige.application.ask_user`
(which has the richer JSONL data — real option labels, descriptions
— rather than the truncated checkbox glyphs the pane scrape sees).

Lifecycle per binding (same shape as StatusService):

    UI detected on pane   → send a card; refresh body if pane text
                            changed, debounce same-content ticks
    UI gone (≥N ticks)    → delete the card

Card content
------------
The captured pane text becomes the card body verbatim (after the
`─────` shortener). Buttons depend on the UI:

* If `extract_options` finds a numbered menu (`❯ 1. Yes`, `2. No`,
  ...): one tap-to-pick button per option (3 per row), plus an
  Esc/🔄/Enter trailer for cancel + refresh + default-confirm.
* Otherwise: a 3×3 nav keyboard — Space/↑/Tab, ←/↓/→, Esc/🔄/Enter.

Click → keystroke
-----------------
Action ids `iui:up`/`iui:dn`/... map to tmux named keys via
`Multiplexer.send_keys(literal=False)`; option picks send the
literal digit (no Enter — Claude Code 2.x's numbered menus accept
the digit alone, then dismiss the dialog). After every click we
re-tick (capture again) so the card either repaints (next prompt
rendered) or gets deleted (dialog cleared).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..domain.card import Action, ActionEvent, Card
from ..domain.conversation import Anchor, Conversation
from ..domain.outbound import CardContent, Outbound
from ..domain.pane import Binding
from ..infrastructure.terminal_parser import (
    extract_interactive_content,
    extract_options,
)
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .message_seq import MessageSeqService
from .outbox import Outbox
from .run_registry import RunRegistry

if TYPE_CHECKING:
    from .live_pane import LivePaneService

logger = logging.getLogger(__name__)

# Action ids — short prefixes to keep callback payloads compact.
# `iui:` = interactive UI.
ACTION_KEY_UP = "iui:up"
ACTION_KEY_DOWN = "iui:dn"
ACTION_KEY_LEFT = "iui:lt"
ACTION_KEY_RIGHT = "iui:rt"
ACTION_KEY_ESC = "iui:esc"
ACTION_KEY_ENTER = "iui:ent"
ACTION_KEY_TAB = "iui:tab"
ACTION_KEY_SPACE = "iui:spc"
ACTION_REFRESH = "iui:rfsh"
ACTION_OPTION = "iui:opt"

# action_id → (named tmux key, literal flag)
_KEY_MAP: dict[str, tuple[str, bool]] = {
    ACTION_KEY_UP: ("Up", False),
    ACTION_KEY_DOWN: ("Down", False),
    ACTION_KEY_LEFT: ("Left", False),
    ACTION_KEY_RIGHT: ("Right", False),
    ACTION_KEY_ESC: ("Escape", False),
    ACTION_KEY_ENTER: ("Enter", False),
    ACTION_KEY_TAB: ("Tab", False),
    ACTION_KEY_SPACE: ("Space", False),
}

# Suppressed names — when pane scrape detects these, the iui card
# path skips rendering. `AskUserQuestion` is no longer in here:
# when wired with a `LivePaneService`, it's auto-handed off to
# `/livepane`'s rich rendering (input slot, ANSI
# highlights). When no `LivePaneService` is wired, the JSONL
# renderer in `paige.application.ask_user` takes over once the
# tool_use line lands in JSONL (often minutes late in plan mode —
# so the livepane handoff is the better default).
_SUPPRESS_NAMES: frozenset[str] = frozenset()

_BindingKey = tuple[str, str, str]


class _BindingState:
    __slots__ = ("anchor", "last_content", "last_name", "idle_misses")

    def __init__(self) -> None:
        self.anchor: Anchor | None = None
        self.last_content: str = ""
        self.last_name: str = ""
        self.idle_misses: int = 0


def _humanize_name(raw: str) -> str:
    """`BashApproval` → "Bash approval". The terminal_parser tags
    overlays with PascalCase identifiers — fine internally, but
    they read like demo code in a colored card header strip."""
    if not raw:
        return "Interactive UI"
    parts: list[str] = []
    current = ""
    for ch in raw:
        if ch.isupper() and current:
            parts.append(current)
            current = ch
        else:
            current += ch
    if current:
        parts.append(current)
    if not parts:
        return raw
    head = parts[0]
    tail = " ".join(p.lower() for p in parts[1:])
    return f"{head} {tail}".strip()


class InteractiveUIService:
    """Periodic pane scrape; one interactive-UI card per binding."""

    def __init__(
        self,
        *,
        multiplexer: Multiplexer,
        registry: RunRegistry,
        outbox: Outbox,
        channel: Channel,
        allow_list: AllowList,
        message_seq: MessageSeqService,
        poll_interval: float = 1.0,
        idle_debounce: int = 2,
        live_pane: LivePaneService | None = None,
    ) -> None:
        self._multiplexer = multiplexer
        self._registry = registry
        self._outbox = outbox
        self._channel = channel
        self._allow_list = allow_list
        self._message_seq = message_seq
        self._poll_interval = poll_interval
        self._idle_debounce = idle_debounce
        self._states: dict[_BindingKey, _BindingState] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Optional LivePaneService — when wired, AskUserQuestion
        # detections delegate to its `start_for_binding`. The iui
        # card path stays the renderer for every other overlay
        # (BashApproval, ExitPlanMode, RestoreCheckpoint, Settings).
        self._live_pane = live_pane

    def install(self, channel: Channel) -> None:
        channel.on_action(self._allow_list.guard_action(self._handle_action))

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="interactive-ui")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as e:
                logger.warning("InteractiveUIService tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    # ── per-tick logic (public for tests) ────────────────────────

    async def tick(self) -> None:
        for pane_id in self._registry.list_panes():
            bindings = self._registry.find_bindings_for_pane(pane_id)
            if not bindings:
                continue
            text = await self._multiplexer.capture(pane_id)
            ui = extract_interactive_content(text or "")
            if ui is not None and ui.name in _SUPPRESS_NAMES:
                # AskUserQuestion handled by the JSONL path with a
                # richer card; treat as "no UI detected here" so we
                # also clear any stale interactive card we'd left.
                ui = None
            for binding in bindings:
                if ui is not None:
                    await self._on_ui(binding, ui.content, ui.name)
                else:
                    await self._on_clear(binding)

    async def refresh_pane(self, pane_id: str) -> None:
        """Re-tick a single pane right now. Called after click
        handlers send keystrokes so the card reflects the resulting
        state without waiting for the next poll cycle."""
        bindings = self._registry.find_bindings_for_pane(pane_id)
        if not bindings:
            return
        text = await self._multiplexer.capture(pane_id)
        ui = extract_interactive_content(text or "")
        if ui is not None and ui.name in _SUPPRESS_NAMES:
            ui = None
        for binding in bindings:
            if ui is not None:
                await self._on_ui(binding, ui.content, ui.name)
            else:
                await self._on_clear(binding)

    # ── state transitions ───────────────────────────────────────

    async def _on_ui(self, binding: Binding, content: str, name: str) -> None:
        # AskUserQuestion hands off to LivePaneService when wired —
        # better UX (auto-poll, mode-aware input slot, Stop/Dismiss
        # buttons, ANSI highlights). `start_for_binding` is
        # idempotent so calling it on every tick is safe. When no
        # live_pane is wired (tests, or a deployment without
        # /livepane), skip the iui card render — the JSONL renderer
        # in `paige.application.ask_user` is the late-arriving
        # fallback.
        if name == "AskUserQuestion":
            if self._live_pane is not None:
                await self._live_pane.start_for_binding(binding.person, binding.conversation)
            return
        state = self._state_for(binding)
        state.idle_misses = 0
        if state.anchor is None:
            await self._send(binding, state, content, name)
        elif content != state.last_content or name != state.last_name:
            await self._edit(binding, state, content, name)
        # else: same content + same overlay name — dedup, no-op

    async def _on_clear(self, binding: Binding) -> None:
        if self._live_pane is not None:
            # Best-effort stop for any live-pane loop we auto-spawned
            # for this binding. No-op when none was spawned, so this
            # is safe for non-AskUserQuestion clears too.
            await self._live_pane.stop_for_binding(binding.person, binding.conversation)
        state = self._states.get(self._key(binding))
        if state is None or state.anchor is None:
            return
        state.idle_misses += 1
        if state.idle_misses < self._idle_debounce:
            return
        await self._delete(binding, state)

    async def _send(self, binding: Binding, state: _BindingState, content: str, name: str) -> None:
        outbound = _build_outbound(binding.conversation, content, name)
        future = self._outbox.enqueue_send(binding.person, outbound)
        try:
            anchor = await future
        except Exception as e:
            logger.debug("interactive UI send failed: %s", e)
            return
        if anchor is not None:
            state.anchor = anchor
            state.last_content = content
            state.last_name = name

    async def _edit(self, binding: Binding, state: _BindingState, content: str, name: str) -> None:
        if state.anchor is None:
            return
        outbound = _build_outbound(binding.conversation, content, name)
        future = self._outbox.enqueue_edit(binding.person, state.anchor, outbound)
        try:
            replacement = await future
        except Exception as e:
            logger.debug("interactive UI edit failed: %s", e)
            return
        if replacement is not None:
            state.anchor = replacement
        state.last_content = content
        state.last_name = name

    async def _delete(self, binding: Binding, state: _BindingState) -> None:
        if state.anchor is None:
            return
        future = self._outbox.enqueue_delete(binding.person, state.anchor)
        try:
            await future
        except Exception as e:
            logger.debug("interactive UI delete failed: %s", e)
        state.anchor = None
        state.last_content = ""
        state.idle_misses = 0

    # ── click handler ───────────────────────────────────────────

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id not in _KEY_MAP and event.action_id not in (
            ACTION_REFRESH,
            ACTION_OPTION,
        ):
            return  # not ours
        pane_id = self._registry.get_pane(event.sender, event.conversation)
        if pane_id is None:
            await self._channel.ack(event, "No bound pane")
            return

        if event.action_id == ACTION_REFRESH:
            await self.refresh_pane(pane_id)
            await self._channel.ack(event, "🔄")
            return

        if event.action_id == ACTION_OPTION:
            digit = event.value.get("num", "")
            if not digit.isdigit():
                await self._channel.ack(event, "Invalid option")
                return
            ok = await self._multiplexer.send_keys(pane_id, digit, enter=False, literal=True)
            if not ok:
                await self._channel.ack(event, "send_keys failed")
                return
            # Numbered options finalize a choice — drop the buttons
            # and tag what was sent so the user gets immediate
            # feedback instead of staring at stale buttons. Rides
            # the inline-refresh slot (this runs inside click
            # dispatch) so the swap lands atomically with the click
            # ack — Feishu's out-of-band PATCH is unreliable on the
            # clicker. The next StatusService tick re-emits if the
            # TUI overlay is still there, or deletes when it's gone.
            await self._patch_picked(event, pane_id, f"#{digit}")
            await self._channel.ack(event, f"#{digit}")
            return

        # Named-key path.
        key, literal = _KEY_MAP[event.action_id]
        ok = await self._multiplexer.send_keys(pane_id, key, enter=False, literal=literal)
        if not ok:
            await self._channel.ack(event, "send_keys failed")
            return
        await asyncio.sleep(0.5)
        await self.refresh_pane(pane_id)
        await self._channel.ack(event, key)

    async def _patch_picked(
        self,
        event: ActionEvent,
        pane_id: str,
        label: str,
    ) -> None:
        """Drop the buttons and append a "✓ Sent: <label>" footer to
        the card the user just tapped. Same UX as `ask_user.py`'s
        post-pick patch — the body stays so the captured TUI text
        remains as context. Resets `state.last_content` so the next
        StatusService tick's dedup doesn't skip a legitimate update
        (the visible card no longer matches the captured pane)."""
        binding = self._binding_for_event(event, pane_id)
        if binding is None:
            return
        state = self._state_for(binding)
        if state.anchor is None:
            return
        body = state.last_content or ""
        quoted = _render_pane_body(body) if body.strip() else ""
        text = f"{quoted}\n\n✓ Sent: {label}" if quoted else f"✓ Sent: {label}"
        card = Card(
            text=text,
            rows=(),
            header_title=_humanize_name(state.last_name),
            header_color="orange",
        )
        outbound = Outbound(conversation=event.conversation, content=CardContent(card=card))
        outbound, _ = self._message_seq.stamp_edit(
            event.sender, event.conversation, state.anchor, outbound
        )
        await self._channel.edit(state.anchor, outbound)
        # Detach the anchor so the polling loop's idle-debounce
        # doesn't delete this card. The "✓ Sent: #N" card is the
        # user's permanent record of what they picked — deleting it
        # leaves a Feishu "撤回" tombstone in the thread, which
        # looks worse than the original stale-buttons issue. If a
        # new overlay shows up later, _on_ui will send a fresh card.
        state.anchor = None
        state.last_content = ""
        state.last_name = ""
        state.idle_misses = 0

    def _binding_for_event(self, event: ActionEvent, pane_id: str) -> Binding | None:
        """Find the binding owning the card the user just tapped.
        Matches both pane and (person, conversation) — paige supports
        multiple bindings for the same pane across different
        conversations, so pane_id alone isn't a unique key."""
        for b in self._registry.find_bindings_for_pane(pane_id):
            if b.person.user_id == event.sender.user_id and b.conversation == event.conversation:
                return b
        return None

    # ── helpers ──────────────────────────────────────────────────

    def _state_for(self, binding: Binding) -> _BindingState:
        key = self._key(binding)
        state = self._states.get(key)
        if state is None:
            state = _BindingState()
            self._states[key] = state
        return state

    @staticmethod
    def _key(binding: Binding) -> _BindingKey:
        return (
            binding.person.user_id,
            binding.conversation.chat_id,
            binding.conversation.thread_id or "",
        )


def _build_outbound(conversation: Conversation, content: str, name: str) -> Outbound:
    rows = _build_rows(content)
    card = Card(
        text=_render_pane_body(content),
        rows=rows,
        header_title=_humanize_name(name),
        header_color="orange",
    )
    return Outbound(conversation=conversation, content=CardContent(card=card))


def _render_pane_body(content: str) -> str:
    """Render the captured pane text as a markdown blockquote rather
    than a fenced code block. Lark's renderer was silently eating
    the content of long fenced blocks (Bash approval cards came
    through as an empty `code-block-without-info` shell). Blockquote
    preserves every line as `> {line}`, sidesteps the code-fence
    code path entirely, and reads naturally as "this is what the
    TUI is currently showing." Empty lines stay as bare `>`."""
    if not content.strip():
        return " "
    lines = content.split("\n")
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _build_rows(content: str) -> tuple[tuple[Action, ...], ...]:
    """Numbered-menu options when present; arrow nav keyboard
    otherwise. The options-row layout is 3-per-row with an
    Esc/🔄/Enter trailer for cancel/refresh/default-confirm; the
    arrow layout is the original 3×3 (Space/↑/Tab, ←/↓/→,
    Esc/🔄/Enter)."""
    options = extract_options(content)
    if options:
        return _option_rows(options)
    return _arrow_rows()


def _option_rows(options: list[tuple[int, str]]) -> tuple[tuple[Action, ...], ...]:
    rows: list[tuple[Action, ...]] = []
    for i in range(0, len(options), 3):
        chunk = options[i : i + 3]
        row: list[Action] = []
        for num, text in chunk:
            label = text[:14] + "…" if len(text) > 14 else text
            row.append(
                Action(
                    label=f"{num}· {label}",
                    action_id=ACTION_OPTION,
                    value={"num": str(num)},
                )
            )
        rows.append(tuple(row))
    rows.append(_trailer_row())
    return tuple(rows)


def _arrow_rows() -> tuple[tuple[Action, ...], ...]:
    return (
        (
            Action(label="␣ Space", action_id=ACTION_KEY_SPACE),
            Action(label="↑", action_id=ACTION_KEY_UP),
            Action(label="⇥ Tab", action_id=ACTION_KEY_TAB),
        ),
        (
            Action(label="←", action_id=ACTION_KEY_LEFT),
            Action(label="↓", action_id=ACTION_KEY_DOWN),
            Action(label="→", action_id=ACTION_KEY_RIGHT),
        ),
        _trailer_row(),
    )


def _trailer_row() -> tuple[Action, ...]:
    return (
        Action(label="⎋ Esc", action_id=ACTION_KEY_ESC),
        Action(label="🔄", action_id=ACTION_REFRESH),
        Action(label="⏎ Enter", action_id=ACTION_KEY_ENTER),
    )


__all__ = [
    "ACTION_KEY_DOWN",
    "ACTION_KEY_ENTER",
    "ACTION_KEY_ESC",
    "ACTION_KEY_LEFT",
    "ACTION_KEY_RIGHT",
    "ACTION_KEY_SPACE",
    "ACTION_KEY_TAB",
    "ACTION_KEY_UP",
    "ACTION_OPTION",
    "ACTION_REFRESH",
    "InteractiveUIService",
]
