"""Manage sub-handler — /session command + Manage card action surface.

The Manage card is the per-binding control surface (`/session`):
🔓 Unbind, 📋 History, 🛠 Commands, ⚙ Prefs, ◀ Back, ✕ Dismiss. The
Prefs sub-panel (verbosity toggles + msg-seq + collapse cycle), the
Commands sub-panel (quick-action forwarded slash-commands), and the
History card all share the Manage anchor — every sub-flow edits the
same card in place, then ◀ Back paints the Manage card back into
the same anchor.

The single Manage → Chooser path (Manage's ◀ Back) routes through
`ChooserHandlers.send_chooser_card`, which sends a fresh chooser
card to the chat — that's the legacy behaviour; the Manage card
stays untouched and a new chooser card appears below it.
"""

from __future__ import annotations

from ..domain.card import ActionEvent, Card
from ..domain.conversation import Conversation
from ..domain.host import Host
from ..domain.outbound import CardContent, Outbound
from ..domain.person import Person
from ._sessions_actions import (
    ACTION_MANAGE_BACK,
    ACTION_MANAGE_CMD,
    ACTION_MANAGE_COMMANDS,
    ACTION_MANAGE_DISMISS,
    ACTION_MANAGE_HISTORY,
    ACTION_MANAGE_PREFS,
    ACTION_MANAGE_UNBIND,
    ACTION_PREFS_BACK,
    ACTION_PREFS_COLLAPSE,
    ACTION_PREFS_MSG_SEQ,
    ACTION_PREFS_TOGGLE,
)
from ._sessions_cards import (
    build_commands_card,
    build_manage_card,
    build_prefs_card,
)
from ._sessions_chooser import ChooserHandlers
from ._sessions_context import SessionsContext
from .collapse_pref import CollapsePrefService
from .history import ACTION_HIST_BACK, HistoryService
from .verbosity import ContentKind, VerbosityService


class ManageHandlers:
    """`/session` command + Manage card / Prefs / Commands handlers.

    Takes the shared context plus references to ChooserHandlers (for
    the Back path that sends a chooser card) and HistoryService (for
    the 📋 History anchor-edit). VerbosityService + the optional
    CollapsePrefService back the Prefs sub-panel toggles.
    """

    OWNED_ACTIONS: frozenset[str] = frozenset(
        {
            ACTION_MANAGE_UNBIND,
            ACTION_MANAGE_HISTORY,
            ACTION_MANAGE_BACK,
            ACTION_MANAGE_DISMISS,
            ACTION_MANAGE_CMD,
            ACTION_MANAGE_COMMANDS,
            ACTION_MANAGE_PREFS,
            ACTION_PREFS_TOGGLE,
            ACTION_PREFS_BACK,
            ACTION_PREFS_MSG_SEQ,
            ACTION_PREFS_COLLAPSE,
            # History's ◀ Back routes here — same shape as Prefs Back
            # (edit the Manage card back into the anchor).
            ACTION_HIST_BACK,
        }
    )

    def __init__(
        self,
        ctx: SessionsContext,
        chooser: ChooserHandlers,
        history_service: HistoryService,
        verbosity: VerbosityService,
        collapse_pref: CollapsePrefService | None,
    ) -> None:
        self._ctx = ctx
        self._chooser = chooser
        self._history_service = history_service
        self._verbosity = verbosity
        # Optional. When None, the Prefs sub-pane omits the
        # 📄 Collapse cycle button.
        self._collapse_pref = collapse_pref

    async def dispatch(self, event: ActionEvent) -> None:
        """Route an OWNED_ACTIONS event to the matching handler.
        Caller (SessionsService) gates on `OWNED_ACTIONS` membership."""
        action_id = event.action_id
        if action_id == ACTION_MANAGE_UNBIND:
            await self.on_manage_unbind(event)
        elif action_id == ACTION_MANAGE_HISTORY:
            await self.on_manage_history(event)
        elif action_id == ACTION_MANAGE_BACK:
            await self.on_manage_back(event)
        elif action_id == ACTION_MANAGE_DISMISS:
            await self.on_manage_dismiss(event)
        elif action_id == ACTION_MANAGE_CMD:
            await self.on_manage_cmd(event)
        elif action_id == ACTION_MANAGE_COMMANDS:
            await self.on_manage_commands(event)
        elif action_id == ACTION_MANAGE_PREFS:
            await self.on_manage_prefs(event)
        elif action_id == ACTION_PREFS_TOGGLE:
            await self.on_prefs_toggle(event)
        elif action_id in (ACTION_PREFS_BACK, ACTION_HIST_BACK):
            await self.on_prefs_back(event)
        elif action_id == ACTION_PREFS_MSG_SEQ:
            await self.on_prefs_msg_seq(event)
        elif action_id == ACTION_PREFS_COLLAPSE:
            await self.on_prefs_collapse(event)

    # ── /session command ─────────────────────────────────────────

    async def send_for(self, sender: Person, conversation: Conversation) -> None:
        """`/session` — open the Manage card for the binding currently
        attached to this conversation. Unbound → fall through to the
        chooser; the chooser is more useful than a dead-end error."""
        pane_id = self._ctx.registry.get_pane(sender, conversation)
        if pane_id is None:
            await self._chooser.send_chooser_card(sender, conversation)
            return
        pane = await self._ctx.multiplexer.find_pane(pane_id)
        ptr = self._ctx.registry.get_run_pointer(pane_id)
        pane_name = getattr(pane, "pane_name", "") or pane_id
        host = self._badge_host(sender, conversation)
        card = build_manage_card(pane_id=pane_id, pane_name=pane_name, ptr=ptr, host=host)
        self._ctx.outbox.enqueue_send(
            sender,
            Outbound(conversation=conversation, content=CardContent(card=card)),
        )

    def _badge_host(self, person: Person, conversation: Conversation) -> Host | None:
        """Return the Host whose badge should appear on the Manage
        card for this binding, or None when the badge should be
        hidden.

        Hidden when (a) HostsService isn't wired, (b) only the
        synthetic local host is configured, or (c) the binding has
        no recognizable host_id. Showing `🖥 local` on every card in
        a single-host install would just be noise; once the user
        adds a remote in `~/.paige/hosts.toml`, the badge surfaces
        automatically."""
        hosts = self._ctx.hosts
        if hosts is None:
            return None
        if len(hosts.list()) <= 1:
            return None
        host_id = self._ctx.registry.get_host(person, conversation)
        if host_id is None:
            return None
        return hosts.get(host_id)

    # ── Manage card actions ─────────────────────────────────────

    async def on_manage_unbind(self, event: ActionEvent) -> None:
        had_binding = self._ctx.registry.get_pane(event.sender, event.conversation) is not None
        if had_binding:
            await self._ctx.registry.unbind(event.sender, event.conversation)
        await self._ctx.channel.ack(event, "🔓 Unbound" if had_binding else "Already unbound")
        confirmation = Outbound(
            conversation=event.conversation,
            content=CardContent(
                card=Card(
                    text="✓ Unbound. Use /sessions to pick another or /start a new one.",
                    header_title="✓ Unbound",
                    header_color="green",
                )
            ),
        )
        self._ctx.outbox.enqueue_edit(event.sender, event.card_anchor, confirmation)

    async def on_manage_history(self, event: ActionEvent) -> None:
        """Tap 📋 History on the Manage card → edit the History card
        into the Manage anchor in place. Matches the Prefs / Commands
        repaint-in-place shape (same anchor for the whole sub-flow).
        Back from History routes through `ACTION_HIST_BACK` →
        `on_prefs_back`, which edits the Manage card back into the
        same anchor."""
        card = await self._history_service.build_card_for(event.sender, event.conversation)
        if card is None:
            # Hint already sent as a text message (unbound / no run /
            # empty / read failed) — leave the Manage card untouched.
            await self._ctx.channel.ack(event, "📋 History")
            return
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, "📋 History")

    async def on_manage_back(self, event: ActionEvent) -> None:
        await self._ctx.channel.ack(event, "◀ Back")
        # Send a fresh chooser card to the chat — that's the legacy
        # behaviour the user expects from "Back from Manage". The
        # Manage card stays where it is.
        await self._chooser.send_chooser_card(event.sender, event.conversation)

    async def on_manage_dismiss(self, event: ActionEvent) -> None:
        await self._ctx.channel.ack(event, "✕")
        self._ctx.outbox.enqueue_delete(event.sender, event.card_anchor)

    async def on_manage_prefs(self, event: ActionEvent) -> None:
        """Open the Preferences sub-panel — replaces the Manage card
        in place via the inline-refresh slot. Back returns here."""
        card = build_prefs_card(
            event.sender,
            event.conversation,
            self._verbosity,
            self._ctx.message_seq,
            self._collapse_pref,
        )
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, "⚙ Prefs")

    async def on_prefs_toggle(self, event: ActionEvent) -> None:
        kind_str = event.value.get("kind", "")
        try:
            kind = ContentKind(kind_str)
        except ValueError:
            await self._ctx.channel.ack(event, "Invalid setting")
            return
        new_v = self._verbosity.toggle(event.sender, event.conversation, kind)
        card = build_prefs_card(
            event.sender,
            event.conversation,
            self._verbosity,
            self._ctx.message_seq,
            self._collapse_pref,
        )
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, f"{kind.value} → {new_v.value}")

    async def on_prefs_msg_seq(self, event: ActionEvent) -> None:
        """Toggle the message-seq stamping for this (person, conv).
        Re-renders the Prefs card so the user sees the new state in
        the toggle label."""
        new_state = self._ctx.message_seq.toggle(event.sender, event.conversation)
        card = build_prefs_card(
            event.sender,
            event.conversation,
            self._verbosity,
            self._ctx.message_seq,
            self._collapse_pref,
        )
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, f"msg seq → {'on' if new_state else 'off'}")

    async def on_prefs_collapse(self, event: ActionEvent) -> None:
        """Cycle the long-body collapse threshold for this
        (person, conv): 25 → 50 → 100 → off → 25. Re-renders
        the Prefs card so the user sees the new state in the
        toggle label."""
        if self._collapse_pref is None:
            await self._ctx.channel.ack(event, "Collapse prefs not wired")
            return
        new_threshold = self._collapse_pref.cycle(event.sender, event.conversation)
        card = build_prefs_card(
            event.sender,
            event.conversation,
            self._verbosity,
            self._ctx.message_seq,
            self._collapse_pref,
        )
        await self._ctx.edit_anchor(event, card)
        ack = "off" if new_threshold == 0 else f"{new_threshold} lines"
        await self._ctx.channel.ack(event, f"collapse → {ack}")

    async def on_prefs_back(self, event: ActionEvent) -> None:
        """Return to the Manage card (same anchor, in-place repaint).
        Reused as the back-handler for the Prefs sub-pane, the
        Commands sub-pane, and the History card — they all repaint
        the Manage card when dismissed.

        If the binding's been cleared meanwhile, fall through to the
        chooser so the user isn't stranded on a stale Manage view."""
        pane_id = self._ctx.registry.get_pane(event.sender, event.conversation)
        if pane_id is None:
            await self._ctx.channel.ack(event, "◀ Back")
            await self.on_manage_back(event)
            return
        pane = await self._ctx.multiplexer.find_pane(pane_id)
        if pane is None:
            await self._ctx.channel.ack(event, "Pane gone")
            await self.on_manage_back(event)
            return
        ptr = self._ctx.registry.get_run_pointer(pane_id)
        host = self._badge_host(event.sender, event.conversation)
        manage = build_manage_card(pane_id=pane_id, pane_name=pane.pane_name, ptr=ptr, host=host)
        await self._ctx.edit_anchor(event, manage)
        await self._ctx.channel.ack(event, "◀ Back")

    async def on_manage_commands(self, event: ActionEvent) -> None:
        """Open the Commands sub-pane — replaces the Manage card in
        place via the inline-refresh slot. Back returns to Manage.
        Mirrors the shape of `on_manage_prefs`."""
        card = build_commands_card()
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, "🛠 Commands")

    async def on_manage_cmd(self, event: ActionEvent) -> None:
        """Quick-action: send a forwarded slash command to the bound
        pane. Mirrors what typing `/clear` does — the Manage card
        stays open so the user can fire several in a row."""
        cmd = event.value.get("cmd", "")
        if not cmd:
            await self._ctx.channel.ack(event, "Invalid command")
            return
        pane_id = self._ctx.registry.get_pane(event.sender, event.conversation)
        if pane_id is None:
            await self._ctx.channel.ack(event, "No bound pane")
            return
        ok = await self._ctx.multiplexer.send_keys(pane_id, f"/{cmd}", enter=True, literal=True)
        if not ok:
            await self._ctx.channel.ack(event, "send_keys failed")
            return
        await self._ctx.channel.ack(event, f"/{cmd}")
