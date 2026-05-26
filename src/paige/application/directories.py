"""DirectoryService — /start picker; spawn-claude-in-fresh-pane flow.

The minimum first-contact UX: a user with no live Claude session
sends `/start`, gets a card listing immediate subdirectories of
`projects_root`, taps one, and paige spawns
`claude --session-id <uuid>` in a fresh tmux pane there + binds
the conversation.

Why deterministic session_id (`--session-id <uuid>`):
`doc/session-discovery.md` argues for it as Signal #1 — paige
knows the run_id from the moment we spawn, so the registry is
correct without waiting on /proc fd-walk. RunDiscovery's tick
will confirm the same data shortly after.

Why per-conversation listing state: tap callbacks carry indices,
not paths, so the action's `value` stays small (some IM backends
cap callback-data length). The DirectoryService remembers the
listing keyed by (user_id, chat_id, thread_id) until the user taps;
a fresh /start in the same conversation overwrites the listing.

`/start` in a *bound* conversation shows a binding-status text
instead of the picker — same as v1's behavior.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from ..domain.card import Action, ActionEvent, Card
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound, TextContent
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .message_seq import MessageSeqService
from .outbox import Outbox
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

ACTION_PICK = "dir:pick"

EMPTY_HINT_TMPL = (
    "No directories found under `{root}`. Set PAIGE_PROJECTS_ROOT or "
    "create some project folders there."
)
NO_ROOT_HINT_TMPL = (
    "Projects root `{root}` doesn't exist. Set PAIGE_PROJECTS_ROOT or create the directory."
)
PICK_HEADER = "*Pick a project* — tap to start a Claude session"

_ListingKey = tuple[str, str, str]  # (user_id, chat_id, thread_id_or_empty)


class DirectoryService:
    """`/start` picker + tap-to-spawn-claude action handler."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        channel: Channel,
        allow_list: AllowList,
        projects_root: Path,
        message_seq: MessageSeqService | None = None,
    ) -> None:
        self._registry = registry
        self._multiplexer = multiplexer
        self._outbox = outbox
        self._channel = channel
        self._allow_list = allow_list
        self._projects_root = projects_root
        # The confirmation repaint goes direct to the channel (inline-
        # refresh), bypassing the Outbox — stamp the seq footer here to
        # match the click-edit convention.
        self._message_seq = message_seq
        # Per-conversation pending listings — taps reference by index.
        self._listings: dict[_ListingKey, list[Path]] = {}

    def install(self, channel: Channel) -> None:
        channel.on_command("start", self._allow_list.guard_command(self._start))
        channel.on_action(self._allow_list.guard_action(self._handle_action))

    # ── /start ───────────────────────────────────────────────────

    async def _start(self, inbound: Inbound, _arg: str) -> None:
        existing = self._registry.get_pane(inbound.sender, inbound.conversation)
        if existing is not None:
            text = (
                f"This conversation is already bound to pane `{existing}`. "
                "Use /unbind to detach, or /sessions to switch."
            )
            self._outbox.enqueue_send(
                inbound.sender,
                Outbound(
                    conversation=inbound.conversation,
                    content=TextContent(text),
                ),
            )
            return

        if not self._projects_root.exists():
            self._send_text(
                inbound,
                NO_ROOT_HINT_TMPL.format(root=self._projects_root),
            )
            return

        directories = sorted(list_subdirs(self._projects_root))
        if not directories:
            self._send_text(
                inbound,
                EMPTY_HINT_TMPL.format(root=self._projects_root),
            )
            return

        # Stash the listing for the action handler to look up.
        self._listings[self._key(inbound)] = directories

        rows = tuple(
            (
                Action(
                    label=f"📁 {d.name}",
                    action_id=ACTION_PICK,
                    value={"i": str(i)},
                ),
            )
            for i, d in enumerate(directories)
        )
        card = Card(
            text=PICK_HEADER,
            rows=rows,
            header_title="📂 Pick a directory",
            header_color="wathet",
        )
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=CardContent(card=card),
            ),
        )

    # ── pick action ──────────────────────────────────────────────

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id != ACTION_PICK:
            return
        key = self._key_for(event.sender.user_id, event.conversation)
        listings = self._listings.get(key)
        if listings is None:
            await self._channel.ack(event, "Picker expired — /start again")
            return
        try:
            idx = int(event.value.get("i", "-1"))
        except ValueError:
            idx = -1
        if not (0 <= idx < len(listings)):
            await self._channel.ack(event, "Invalid pick — /start again")
            return

        cwd = listings[idx]
        # Generate a deterministic run_id so the registry is correct
        # immediately, before RunDiscovery's next tick.
        # Dashed RFC 4122 form (`str(uuid.uuid4())`) — `claude
        # --session-id` rejects bare-hex UUIDs with "Invalid session
        # ID. Must be a valid UUID."
        run_id = str(uuid.uuid4())
        try:
            pane = await self._multiplexer.create_pane(
                name=cwd.name,
                cwd=cwd,
                command=f"claude --session-id {run_id}",
            )
        except Exception as e:
            logger.exception("create_pane failed for %s: %s", cwd, e)
            await self._channel.ack(event, "Failed to spawn pane — see logs")
            return

        await self._registry.register_run(pane.pane_id, run_id, cwd)
        await self._registry.bind(event.sender, event.conversation, pane.pane_id)
        # Listing consumed.
        self._listings.pop(key, None)
        await self._channel.ack(event, f"Started: {cwd.name}")

        # CardContent (not TextContent) so the FeishuChannel inline-
        # refresh path fires — the click response carries the new
        # card and Feishu repaints atomically. A TextContent edit on
        # a card anchor would PATCH cross-type and Feishu rejects it
        # with 400 (codes 230001 / 230099).
        confirmation = Outbound(
            conversation=event.conversation,
            content=CardContent(
                card=Card(
                    text=(
                        f"✓ Started Claude in *{cwd.name}* "
                        f"(pane `{pane.pane_id}`). "
                        "Send a message to begin."
                    ),
                    header_title="✓ Started",
                    header_color="green",
                )
            ),
        )
        # Direct channel.edit (not the Outbox) so the repaint rides the
        # click-response inline-refresh slot — an out-of-band PATCH to
        # the just-clicked card repaints unreliably (often as a new card).
        # Stamp the seq footer here since the Outbox is bypassed.
        if self._message_seq is not None:
            confirmation, _ = self._message_seq.stamp_edit(
                event.sender, event.conversation, event.card_anchor, confirmation
            )
        await self._channel.edit(event.card_anchor, confirmation)

    # ── helpers ──────────────────────────────────────────────────

    def _send_text(self, inbound: Inbound, text: str) -> None:
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=TextContent(text),
            ),
        )

    @staticmethod
    def _key(inbound: Inbound) -> _ListingKey:
        return (
            inbound.sender.user_id,
            inbound.conversation.chat_id,
            inbound.conversation.thread_id or "",
        )

    @staticmethod
    def _key_for(user_id: str, conversation: object) -> _ListingKey:
        chat_id = getattr(conversation, "chat_id", "")
        thread_id = getattr(conversation, "thread_id", None) or ""
        return (user_id, str(chat_id), str(thread_id))


def list_subdirs(root: Path) -> list[Path]:
    """Immediate child directories of `root`, hidden ones excluded.

    OS errors (permissions, root vanished mid-call) are swallowed —
    the picker treats them the same as "no directories."
    """
    try:
        entries = list(root.iterdir())
    except OSError as e:
        logger.debug("listing %s failed: %s", root, e)
        return []
    return [p for p in entries if p.is_dir() and not p.name.startswith(".")]


__all__ = [
    "ACTION_PICK",
    "EMPTY_HINT_TMPL",
    "NO_ROOT_HINT_TMPL",
    "PICK_HEADER",
    "DirectoryService",
    "list_subdirs",
]
