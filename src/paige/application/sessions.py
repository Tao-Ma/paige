"""SessionsService — /sessions chooser + /session Manage card.

`/sessions` is a **three-category chooser** so the top-level surface
stays tight even when the user has many sessions:

  1. **Active** — claude processes paige is currently tracking via
     `RunRegistry`. Tap → Active sub-pane lists active panes
     ordered by cwd. Pick a pane → row-detail card with `🔗 Bind`.
  2. **Resume** — dormant JSONL transcripts under
     `~/.claude/projects/`. Tap → Resume sub-pane lists dormants
     ordered by cwd, paginated. Pick → `▶ Resume` (spawns
     `claude --resume <sid>`) or `🗑 Delete`.
  3. **New** — directories under the configured `projects_root`
     (default `~/projects`). Tap → New sub-pane lists immediate
     subdirs. Pick → confirmation card with `🚀 Start`.

Each sub-pane has a trailing `🔄 Refresh / ◀ Back / ✕ Dismiss` row
where Back returns to the top-level chooser. Each row-detail card
has a per-shape primary action plus `◀ Back` (returns to that
sub-pane) / `✕ Dismiss`. Shape: `/sessions → category → list →
detail → confirmation`.

`/session` (singular) opens a Manage card for the binding currently
attached to this conversation. Unbound → falls through to `/sessions`
(more useful than a dead-end error). Bound → renders pane info +
action rows: 🔓 Unbind, 📋 History, 🛠 Commands, ⚙ Prefs, ◀ Back,
✕ Dismiss.

This module is the orchestrator — it builds the shared context,
constructs the three sub-handlers (chooser / lifecycle / manage),
and routes `_handle_action` to whichever sub-handler owns the
incoming action_id. The actual handler bodies live in the siblings:

- `_sessions_chooser.ChooserHandlers` — chooser + sub-pane listings
  + row-pick repaints. Pure UX, no state mutations.
- `_sessions_lifecycle.LifecycleHandlers` — bind / resume /
  new-start / dormant-delete. State mutations + spawn `claude`.
- `_sessions_manage.ManageHandlers` — `/session` command + Manage
  card + Prefs / Commands sub-panels. Anchor edits, no spawns.
- `_sessions_cards` — pure card builders (manage / prefs / commands
  / per-row detail cards + subpane_nav).
- `_sessions_actions` — every `ACTION_*` string constant; this
  module re-exports them so existing imports keep working.
- `_sessions_context.SessionsContext` — shared deps bundle + the
  `edit_anchor` helper used by every sub-handler.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..domain.card import ActionEvent
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from ._sessions_actions import (
    ACTION_ACTIVE_PICK,
    ACTION_ARCHIVE_PAGE,
    ACTION_ARCHIVE_PICK,
    ACTION_ARCHIVE_RESTORE,
    ACTION_ARCHIVE_VIEW,
    ACTION_BIND,
    ACTION_DORMANT_ARCHIVE,
    ACTION_DORMANT_DELETE,
    ACTION_DORMANT_PAGE,
    ACTION_DORMANT_PICK,
    ACTION_MANAGE_BACK,
    ACTION_MANAGE_CMD,
    ACTION_MANAGE_COMMANDS,
    ACTION_MANAGE_DISMISS,
    ACTION_MANAGE_HISTORY,
    ACTION_MANAGE_PREFS,
    ACTION_MANAGE_UNBIND,
    ACTION_NEW_PICK,
    ACTION_NEW_START,
    ACTION_OPEN_ACTIVE,
    ACTION_OPEN_ARCHIVE,
    ACTION_OPEN_HOST,
    ACTION_OPEN_NEW,
    ACTION_OPEN_OVERVIEW,
    ACTION_OPEN_RESUME,
    ACTION_PREFS_BACK,
    ACTION_PREFS_COLLAPSE,
    ACTION_PREFS_MSG_SEQ,
    ACTION_PREFS_TOGGLE,
    ACTION_RESUME,
    ACTION_SESSIONS_REFRESH,
)
from ._sessions_chooser import ChooserHandlers
from ._sessions_context import (
    ArchiveCountFn,
    ArchiveIndexFn,
    DormantCountFn,
    DormantIndexFn,
    SessionsContext,
    default_archive_count,
    default_archive_index,
    default_dormant_count,
    default_dormant_index,
)
from ._sessions_lifecycle import LifecycleHandlers
from ._sessions_manage import ManageHandlers
from .access import AllowList
from .collapse_pref import CollapsePrefService
from .history import HistoryService
from .hosts import HostsService
from .message_seq import MessageSeqService
from .outbox import Outbox
from .run_registry import RunRegistry
from .verbosity import VerbosityService

logger = logging.getLogger(__name__)


class SessionsService:
    """`/sessions` + `/session` command surface.

    Thin orchestrator — builds the shared `SessionsContext` and the
    three sub-handlers (chooser / lifecycle / manage) once at
    construction time, then routes incoming actions to whichever
    owns the action_id. The handler bodies live in the siblings; see
    the module docstring.
    """

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        channel: Channel,
        allow_list: AllowList,
        history_service: HistoryService,
        verbosity: VerbosityService,
        message_seq: MessageSeqService,
        collapse_pref: CollapsePrefService | None = None,
        hosts: HostsService | None = None,
        claude_projects_root: Path | None = None,
        new_projects_root: Path | None = None,
        dormant_index: DormantIndexFn = default_dormant_index,
        dormant_count: DormantCountFn | None = None,
        archive_index: ArchiveIndexFn = default_archive_index,
        archive_count: ArchiveCountFn | None = None,
    ) -> None:
        self._allow_list = allow_list
        # When the caller leaves `dormant_count=None`, derive it from
        # whatever `dormant_index` they passed: prod default → cheap
        # stat-only walk; test override → just len() the returned list
        # so tests don't have to pass two synchronized fakes.
        if dormant_count is None:
            if dormant_index is default_dormant_index:
                dormant_count = default_dormant_count
            else:
                _index = dormant_index

                async def _derived_count(root: Path, excl: frozenset[str]) -> int:
                    return len(await _index(root, excl))

                dormant_count = _derived_count
        if archive_count is None:
            if archive_index is default_archive_index:
                archive_count = default_archive_count
            else:
                _arch_index = archive_index

                async def _derived_archive_count(root: Path) -> int:
                    return len(await _arch_index(root))

                archive_count = _derived_archive_count
        # `claude_projects_root` is where dormant transcripts live
        # (~/.claude/projects). `new_projects_root` is where the
        # `🆕 New` sub-pane scans for candidate directories
        # (~/projects by default — the same root /start uses). The
        # two are deliberately separate: starting a new session in
        # ~/projects/foo is what the user typically wants, but the
        # dormant index reads claude's transcript layout. The
        # archive root is a sibling of `claude_projects_root` —
        # `~/.claude/archive` for the default — derived once here.
        projects_root = claude_projects_root or (Path.home() / ".claude" / "projects")
        self._ctx = SessionsContext(
            registry=registry,
            multiplexer=multiplexer,
            outbox=outbox,
            channel=channel,
            message_seq=message_seq,
            hosts=hosts,
            projects_root=projects_root,
            new_projects_root=new_projects_root or (Path.home() / "projects"),
            archive_root=projects_root.parent / "archive",
            dormant_index=dormant_index,
            dormant_count=dormant_count,
            archive_index=archive_index,
            archive_count=archive_count,
        )
        self._chooser = ChooserHandlers(self._ctx)
        self._lifecycle = LifecycleHandlers(self._ctx, self._chooser)
        self._manage = ManageHandlers(
            self._ctx,
            self._chooser,
            history_service,
            verbosity,
            collapse_pref,
        )
        # Held for the single ACTION_ARCHIVE_VIEW callback — building
        # a History card from an archived JSONL path. Lives at the
        # orchestrator (not in a sub-handler) because no sub-handler
        # otherwise needs HistoryService except Manage, which owns
        # the bound-session History.
        self._history_service = history_service

    def install(self, channel: Channel) -> None:
        channel.on_command("sessions", self._allow_list.guard_command(self._sessions))
        channel.on_command("session", self._allow_list.guard_command(self._session))
        channel.on_action(self._allow_list.guard_action(self._handle_action))

    async def _sessions(self, inbound: Inbound, _arg: str) -> None:
        await self._chooser.send_chooser_card(inbound.sender, inbound.conversation)

    async def _session(self, inbound: Inbound, _arg: str) -> None:
        await self._manage.send_for(inbound.sender, inbound.conversation)

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id in ChooserHandlers.OWNED_ACTIONS:
            await self._chooser.dispatch(event)
            return
        if event.action_id in LifecycleHandlers.OWNED_ACTIONS:
            await self._lifecycle.dispatch(event)
            return
        if event.action_id in ManageHandlers.OWNED_ACTIONS:
            await self._manage.dispatch(event)
            return
        if event.action_id == ACTION_ARCHIVE_VIEW:
            await self._on_archive_view(event)
            return
        # Other action_ids are someone else's problem.

    async def _on_archive_view(self, event: ActionEvent) -> None:
        """Render an archived session's transcript by building a fresh
        History card and sending it as a new outbound — the archive
        detail card stays in place. Pages are cached on the
        HistoryService so subsequent ◀ Older / Newer ▶ taps work the
        same way as `/history`. Same shape limitation: pages live
        under the conversation key, so flipping between /history and
        an archive view in the same conversation will overwrite each
        other's pagination cache (see HistoryService docstring)."""
        file_path_str = event.value.get("file_path", "")
        if not file_path_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        jsonl_path = Path(file_path_str)
        card = await self._history_service.build_card_for_path(
            event.sender, event.conversation, jsonl_path
        )
        if card is None:
            # build_card_for_path already sent a hint (empty / read
            # failed) — nothing to add.
            await self._ctx.channel.ack(event, "📖 No content")
            return
        outbound = Outbound(
            conversation=event.conversation,
            content=CardContent(card=card),
        )
        self._ctx.outbox.enqueue_send(event.sender, outbound)
        await self._ctx.channel.ack(event, "📖 View")


__all__ = [
    "ACTION_ACTIVE_PICK",
    "ACTION_ARCHIVE_PAGE",
    "ACTION_ARCHIVE_PICK",
    "ACTION_ARCHIVE_RESTORE",
    "ACTION_ARCHIVE_VIEW",
    "ACTION_BIND",
    "ACTION_DORMANT_ARCHIVE",
    "ACTION_DORMANT_DELETE",
    "ACTION_DORMANT_PAGE",
    "ACTION_DORMANT_PICK",
    "ACTION_MANAGE_BACK",
    "ACTION_MANAGE_CMD",
    "ACTION_MANAGE_COMMANDS",
    "ACTION_MANAGE_DISMISS",
    "ACTION_MANAGE_HISTORY",
    "ACTION_MANAGE_PREFS",
    "ACTION_MANAGE_UNBIND",
    "ACTION_NEW_PICK",
    "ACTION_NEW_START",
    "ACTION_OPEN_ACTIVE",
    "ACTION_OPEN_ARCHIVE",
    "ACTION_OPEN_HOST",
    "ACTION_OPEN_NEW",
    "ACTION_OPEN_OVERVIEW",
    "ACTION_OPEN_RESUME",
    "ACTION_PREFS_BACK",
    "ACTION_PREFS_COLLAPSE",
    "ACTION_PREFS_MSG_SEQ",
    "ACTION_PREFS_TOGGLE",
    "ACTION_RESUME",
    "ACTION_SESSIONS_REFRESH",
    "SessionsService",
]
