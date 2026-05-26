"""Chooser sub-handler — /sessions top-level category card + sub-pane
listings + row-pick repaints.

Surfaces:
- `/sessions` top-level: multi-host overview (≥2 hosts) or the
  Active / Resume / New chooser (single-host or after picking a host).
- Active sub-pane: list of registered run-pointers ordered by cwd.
- Resume sub-pane: list of dormant JSONL transcripts ordered by cwd.
- New sub-pane: list of immediate subdirs under `new_projects_root`.
- Per-row detail cards (Active / Resume / New) for the picked entry.

Pure UX surface — no state mutations beyond the channel edits. State
mutations (bind, resume, new-start spawn, dormant delete) live in
`LifecycleHandlers`; the Manage card + Prefs / Commands live in
`ManageHandlers`. The chooser is the dependency root: Lifecycle and
Manage both reach back into it for chooser-as-back-target repaints
(via `render_chooser_into_anchor`), but the chooser doesn't import
from either sibling.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..domain.card import Action, ActionCell, ActionEvent, Card, TextCell
from ..domain.conversation import Conversation
from ..domain.host import LOCAL_HOST_ID
from ..domain.outbound import CardContent, Outbound
from ..domain.person import Person
from ..infrastructure.sessions_index import DormantSession
from ._sessions_actions import (
    ACTION_ACTIVE_PICK,
    ACTION_ARCHIVE_PAGE,
    ACTION_ARCHIVE_PICK,
    ACTION_DORMANT_PAGE,
    ACTION_DORMANT_PICK,
    ACTION_MANAGE_DISMISS,
    ACTION_NEW_PICK,
    ACTION_OPEN_ACTIVE,
    ACTION_OPEN_ARCHIVE,
    ACTION_OPEN_HOST,
    ACTION_OPEN_NEW,
    ACTION_OPEN_OVERVIEW,
    ACTION_OPEN_RESUME,
    ACTION_SESSIONS_REFRESH,
)
from ._sessions_cards import (
    build_active_detail_card,
    build_archived_detail_card,
    build_dormant_detail_card,
    build_new_detail_card,
    subpane_nav,
)
from ._sessions_context import SessionsContext
from .directories import list_subdirs

# Resume sub-pane shows this many dormants per page. Lark caps card
# actions at ~20 rows; 10 picks + a page-nav row + the subpane-nav
# row stays well under that with room to spare on mobile screens.
_RESUME_PAGE_SIZE = 10

# Cache key: (user_id, chat_id, thread_id_or_empty) — matches
# HistoryService._ConvKey so the shape's familiar.
_ConvKey = tuple[str, str, str]


def _conv_key(sender: Person, conversation: Conversation) -> _ConvKey:
    return (sender.user_id, conversation.chat_id, conversation.thread_id or "")


class ChooserHandlers:
    """`/sessions` chooser + sub-pane action handlers.

    Constructed by `SessionsService` with the shared `SessionsContext`.
    The top-level dispatch in `SessionsService._handle_action` routes
    chooser-owned action_ids through `dispatch()`; everything else
    falls through to the lifecycle / manage handlers.
    """

    # action_ids owned by this sub-handler. The set lets the top-level
    # dispatcher route in one membership test instead of an elif chain.
    OWNED_ACTIONS: frozenset[str] = frozenset(
        {
            ACTION_OPEN_HOST,
            ACTION_OPEN_OVERVIEW,
            ACTION_OPEN_ACTIVE,
            ACTION_OPEN_RESUME,
            ACTION_OPEN_NEW,
            ACTION_OPEN_ARCHIVE,
            ACTION_NEW_PICK,
            ACTION_ACTIVE_PICK,
            ACTION_DORMANT_PICK,
            ACTION_DORMANT_PAGE,
            ACTION_ARCHIVE_PICK,
            ACTION_ARCHIVE_PAGE,
            ACTION_SESSIONS_REFRESH,
        }
    )

    def __init__(self, ctx: SessionsContext) -> None:
        self._ctx = ctx
        # Per-conversation cache of the dormant listing. Populated by
        # `on_open_resume` (and the sub-pane's 🔄 Refresh, which routes
        # there). Page taps slice from this cache so repeated paging
        # doesn't re-walk the JSONL tree. Lifetime: until explicit
        # refresh or process restart — same model as HistoryService.
        self._dormant_cache: dict[_ConvKey, list[DormantSession]] = {}
        # Same shape for the archive listing — populated by
        # `on_open_archive` and reused by `on_archive_page`.
        self._archive_cache: dict[_ConvKey, list[DormantSession]] = {}

    async def dispatch(self, event: ActionEvent) -> None:
        """Route an OWNED_ACTIONS event to the matching handler.
        Caller (SessionsService) gates on `OWNED_ACTIONS` membership."""
        action_id = event.action_id
        if action_id == ACTION_OPEN_HOST:
            await self.on_open_host(event)
        elif action_id == ACTION_OPEN_OVERVIEW:
            await self.on_open_overview(event)
        elif action_id == ACTION_OPEN_ACTIVE:
            await self.on_open_active(event)
        elif action_id == ACTION_OPEN_RESUME:
            await self.on_open_resume(event)
        elif action_id == ACTION_OPEN_NEW:
            await self.on_open_new(event)
        elif action_id == ACTION_NEW_PICK:
            await self.on_new_pick(event)
        elif action_id == ACTION_ACTIVE_PICK:
            await self.on_active_pick(event)
        elif action_id == ACTION_DORMANT_PICK:
            await self.on_dormant_pick(event)
        elif action_id == ACTION_DORMANT_PAGE:
            await self.on_dormant_page(event)
        elif action_id == ACTION_OPEN_ARCHIVE:
            await self.on_open_archive(event)
        elif action_id == ACTION_ARCHIVE_PICK:
            await self.on_archive_pick(event)
        elif action_id == ACTION_ARCHIVE_PAGE:
            await self.on_archive_page(event)
        elif action_id == ACTION_SESSIONS_REFRESH:
            await self.on_sessions_refresh(event)

    # ── chooser-as-new-card ──────────────────────────────────────

    async def send_chooser_card(self, sender: Person, conversation: Conversation) -> None:
        """Enqueue a fresh chooser card (or host overview, when multi-
        host is in play) as a new outbound message. Used by the
        `/sessions` command and by the Manage card's ◀ Back path,
        which both want a new card on the chat surface rather than
        an in-place anchor edit."""
        if self._ctx.hosts is not None and len(self._ctx.hosts.list()) > 1:
            card = self.build_host_overview_card()
        else:
            card = await self.build_chooser_card()
        self._ctx.outbox.enqueue_send(
            sender,
            Outbound(conversation=conversation, content=CardContent(card=card)),
        )

    # ── card builders ────────────────────────────────────────────

    def build_host_overview_card(self) -> Card:
        """Multi-host top-level: list every configured host with
        its status and count summary, plus a Refresh / Dismiss nav
        row. Tap a host row → opens the category chooser (currently
        host-agnostic; SSH-slice will filter the chooser by host_id).

        Status today:
        - `local` is always `●` (paige itself runs there).
        - Remote hosts show `✗ disconnected` until the SSH adapter
          slice lands; the placeholder makes the multi-host UX
          visible even before remotes are functional.

        Single-host installs never reach this card — `SessionsService._sessions`
        short-circuits when `len(hosts) <= 1`.
        """
        hosts = self._ctx.hosts
        if hosts is None:
            # Defensive: shouldn't be reachable since `_sessions`
            # gates on `self._hosts is not None`. Render an
            # informative placeholder rather than crashing.
            body = "*Multi-host overview unavailable* — HostsService not wired."
            return Card(
                text=body,
                rows=((Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),),),
                header_title="🔗 Sessions",
                header_color="wathet",
            )
        host_count = len(hosts.list())
        body = f"*{host_count} hosts* — pick one to see its sessions"
        host_rows: list[tuple[Action, ...]] = []
        for host in hosts.list():
            status = "●" if host.host_id == LOCAL_HOST_ID else "✗ disconnected"
            label = f"🖥 {host.display_name} — {status}"
            host_rows.append(
                (
                    Action(
                        label=label,
                        action_id=ACTION_OPEN_HOST,
                        value={"host_id": host.host_id},
                    ),
                )
            )
        nav_row: tuple[Action, ...] = (
            Action(label="🔄 Refresh", action_id=ACTION_OPEN_OVERVIEW),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        )
        return Card(
            text=body,
            rows=tuple(host_rows) + (nav_row,),
            header_title="🔗 Sessions",
            header_color="wathet",
        )

    async def build_chooser_card(self) -> Card:
        """Build the /sessions category chooser.

        Four categories in a 3×2 grid:
            ● Active(N)  | ○ Resume(M)
            🆕 New       | 📦 Archive(K)
            🔄 Refresh   | ✕ Dismiss

        The body summarises counts so the user knows whether tapping
        a category will land on something. Always returns a card (no
        None empty-state) — even with 0 of everything the user can
        still tap 🆕 New. Archive is always shown even at 0 so the
        category is discoverable; the count would otherwise stay at 0
        until the first archive happens."""
        active_count = self._count_active()
        active_run_ids = self._tracked_run_ids()
        dormant_count = await self._ctx.dormant_count(self._ctx.projects_root, active_run_ids)
        archive_count = await self._ctx.archive_count(self._ctx.archive_root)
        body = self._render_chooser_body(
            active=active_count, dormant=dormant_count, archive=archive_count
        )
        rows: tuple[tuple[Action, ...], ...] = (
            (
                Action(label=f"● Active ({active_count})", action_id=ACTION_OPEN_ACTIVE),
                Action(label=f"○ Resume ({dormant_count})", action_id=ACTION_OPEN_RESUME),
            ),
            (
                Action(label="🆕 New", action_id=ACTION_OPEN_NEW),
                Action(label=f"📦 Archive ({archive_count})", action_id=ACTION_OPEN_ARCHIVE),
            ),
            (
                Action(label="🔄 Refresh", action_id=ACTION_SESSIONS_REFRESH),
                Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
            ),
        )
        return Card(
            text=body,
            rows=rows,
            header_title="🔗 Sessions",
            header_color="wathet",
        )

    async def build_active_listing_card(self) -> Card:
        """Active sub-pane — list of panes with a registered run,
        ordered by cwd. Trailing nav: 🔄 Refresh / ◀ Back / ✕ Dismiss
        where Back returns to the top-level chooser."""
        entries: list[tuple[str, Path, str]] = []
        for pane_id in self._ctx.registry.list_panes():
            ptr = self._ctx.registry.get_run_pointer(pane_id)
            if ptr is None:
                continue
            pane = await self._ctx.multiplexer.find_pane(pane_id)
            label = self._active_row_label(pane_id, pane, ptr.cwd)
            entries.append((pane_id, ptr.cwd, label))
        entries.sort(key=lambda e: str(e[1]).lower())
        rows: list[tuple[Action, ...]] = [
            (Action(label=label, action_id=ACTION_ACTIVE_PICK, value={"pane_id": pid}),)
            for pid, _cwd, label in entries
        ]
        body = "*No active sessions.*" if not rows else f"*{len(rows)} active session(s)*"
        rows.append(subpane_nav(self_action=ACTION_OPEN_ACTIVE))
        return Card(
            text=body,
            rows=tuple(rows),
            header_title="● Active sessions",
            header_color="green",
        )

    async def fetch_and_cache_dormants(
        self, sender: Person, conversation: Conversation
    ) -> list[DormantSession]:
        """Walk the JSONL tree, cache the result for this conversation,
        and return it sorted by cwd. Subsequent page taps reuse the
        cache. Called from `on_open_resume` (initial render + the
        sub-pane's 🔄 Refresh button)."""
        active_run_ids = self._tracked_run_ids()
        dormants = await self._ctx.dormant_index(self._ctx.projects_root, active_run_ids)
        # `dormants` already comes sorted by mtime; re-sort by cwd to
        # match the user's "order them by dir" request and so the
        # listing is stable across refreshes.
        dormants_sorted = sorted(dormants, key=lambda d: str(d.cwd).lower())
        self._dormant_cache[_conv_key(sender, conversation)] = dormants_sorted
        return dormants_sorted

    def render_resume_page(self, dormants: list[DormantSession], page: int) -> Card:
        """Build the Resume sub-pane card showing one page of
        `dormants`. Each dormant is one row of the card's
        `column_set_rows` faux-table — cwd basename + full path on
        the left, date / time / msg-count stacked in a slim middle
        column, and a small primary `⚙` button on the right. Mobile-
        tuned layout, live-validated against Lark JSON 2.0.
        Pagination (◀ Prev / N/M / Next ▶) and the sub-pane nav
        still ride on `rows` since they're single-line button
        strips."""
        total = len(dormants)
        if total == 0:
            body = "*No dormant sessions.*"
            rows: tuple[tuple[Action, ...], ...] = (subpane_nav(self_action=ACTION_OPEN_RESUME),)
            return Card(
                text=body,
                rows=rows,
                header_title="○ Resume",
                header_color="wathet",
            )
        page_count = max(1, (total + _RESUME_PAGE_SIZE - 1) // _RESUME_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        slice_start = page * _RESUME_PAGE_SIZE
        slice_end = slice_start + _RESUME_PAGE_SIZE
        page_dormants = dormants[slice_start:slice_end]
        body = f"*{total} dormant session(s) — page {page + 1} of {page_count}*"
        data_rows: list[tuple[TextCell | ActionCell, ...]] = [
            self._dormant_column_row(d) for d in page_dormants
        ]
        listing_rows: list[tuple[Action, ...]] = []
        if page_count > 1:
            page_buttons: list[Action] = []
            if page > 0:
                page_buttons.append(
                    Action(
                        label="◀ Prev",
                        action_id=ACTION_DORMANT_PAGE,
                        value={"i": str(page - 1)},
                    )
                )
            page_buttons.append(
                Action(
                    label=f"{page + 1}/{page_count}",
                    action_id=ACTION_DORMANT_PAGE,
                    value={"i": str(page)},
                )
            )
            if page < page_count - 1:
                page_buttons.append(
                    Action(
                        label="Next ▶",
                        action_id=ACTION_DORMANT_PAGE,
                        value={"i": str(page + 1)},
                    )
                )
            listing_rows.append(tuple(page_buttons))
        listing_rows.append(subpane_nav(self_action=ACTION_OPEN_RESUME))
        return Card(
            text=body,
            column_set_rows=tuple(data_rows),
            rows=tuple(listing_rows),
            header_title="○ Resume",
            header_color="wathet",
        )

    async def build_new_listing_card(self) -> Card:
        """New sub-pane — list of immediate subdirectories under
        `new_projects_root`, ordered by name. Each row is a
        candidate cwd for a fresh `claude` session; tapping opens
        the confirmation card. Trailing nav like the other sub-panes.
        """
        # `list_subdirs` swallows OS errors and returns []; either
        # missing root or unreadable both manifest as an empty
        # listing here. The body line distinguishes them so the
        # user knows what to fix.
        new_root = self._ctx.new_projects_root
        if not new_root.exists():
            body = (
                f"*Projects root not found:* `{new_root}`. Create it or set `PAIGE_PROJECTS_ROOT`."
            )
            rows = (subpane_nav(self_action=ACTION_OPEN_NEW),)
            return Card(
                text=body,
                rows=rows,
                header_title="🆕 New",
                header_color="wathet",
            )
        directories = sorted(list_subdirs(new_root), key=lambda p: p.name.lower())
        if not directories:
            body = f"*No directories under* `{new_root}`. Create one to start a fresh session."
            rows = (subpane_nav(self_action=ACTION_OPEN_NEW),)
            return Card(
                text=body,
                rows=rows,
                header_title="🆕 New",
                header_color="wathet",
            )
        body = f"*Pick a directory* — {len(directories)} candidate(s)"
        listing_rows: list[tuple[Action, ...]] = [
            (Action(label=f"📁 {d.name}", action_id=ACTION_NEW_PICK, value={"cwd": str(d)}),)
            for d in directories
        ]
        listing_rows.append(subpane_nav(self_action=ACTION_OPEN_NEW))
        return Card(
            text=body,
            rows=tuple(listing_rows),
            header_title="🆕 New",
            header_color="wathet",
        )

    # ── chooser-as-back-target ───────────────────────────────────

    async def render_chooser_into_anchor(self, event: ActionEvent) -> None:
        """Build the chooser fresh and edit it into `event.card_anchor`.
        Public because Lifecycle handlers call this after a successful
        dormant-delete to repaint the chooser in place."""
        card = await self.build_chooser_card()
        await self._ctx.edit_anchor(event, card)

    # ── handlers ─────────────────────────────────────────────────

    async def on_active_pick(self, event: ActionEvent) -> None:
        """Tap on an active row in the Active sub-pane → repaint into
        the per-row detail card (Bind / Refresh / Back / Dismiss).
        Validates the pane still exists; degrades to the Active
        sub-pane listing if it vanished between list and tap."""
        pane_id = event.value.get("pane_id", "")
        if not pane_id:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        pane = await self._ctx.multiplexer.find_pane(pane_id)
        if pane is None:
            await self._ctx.channel.ack(event, "Pane gone — refreshing")
            await self._ctx.edit_anchor(event, await self.build_active_listing_card())
            return
        ptr = self._ctx.registry.get_run_pointer(pane_id)
        pane_name = getattr(pane, "pane_name", "") or pane_id
        card = build_active_detail_card(pane_id=pane_id, pane_name=pane_name, ptr=ptr)
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, pane_name)

    async def on_dormant_pick(self, event: ActionEvent) -> None:
        """Tap on a dormant row in the Resume sub-pane → repaint to
        the per-row detail card (Resume / Delete / Back / Dismiss)."""
        sid = event.value.get("sid", "")
        cwd_str = event.value.get("cwd", "")
        file_path_str = event.value.get("file_path", "")
        if not sid or not cwd_str or not file_path_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        card = build_dormant_detail_card(sid=sid, cwd=Path(cwd_str), file_path=Path(file_path_str))
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, "⚙ " + (Path(cwd_str).name or sid[:8]))

    async def on_open_host(self, event: ActionEvent) -> None:
        """Tap on a host row in the multi-host overview → render the
        category chooser into the same anchor. The chooser is
        (currently) host-agnostic — it shows the global registry's
        active/dormant counts. Once SSH adapters land and the
        registry holds entries with non-local host_ids, we'll filter
        the chooser by host_id; for now this is UX scaffolding so
        the overview→chooser navigation is in place."""
        host_id = event.value.get("host_id", "")
        if not host_id:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        await self._ctx.edit_anchor(event, await self.build_chooser_card())
        host_label = host_id
        if self._ctx.hosts is not None:
            host_label = self._ctx.hosts.get(host_id).display_name
        await self._ctx.channel.ack(event, f"🖥 {host_label}")

    async def on_open_overview(self, event: ActionEvent) -> None:
        """🔄 Refresh on the host overview, or future paths that need
        to reach the overview from a card. No-op when single-host
        is in play (the overview card wouldn't have rendered)."""
        await self._ctx.edit_anchor(event, self.build_host_overview_card())
        await self._ctx.channel.ack(event, "🔄")

    async def on_open_active(self, event: ActionEvent) -> None:
        """Top-level chooser's `● Active(N)` button or the Active
        sub-pane's `🔄 Refresh` button — both render the Active
        listing into the same anchor."""
        await self._ctx.edit_anchor(event, await self.build_active_listing_card())
        await self._ctx.channel.ack(event, "● Active")

    async def on_open_resume(self, event: ActionEvent) -> None:
        """Top-level chooser's `○ Resume(M)` button or the Resume
        sub-pane's `🔄 Refresh` button. Both walk the JSONL tree fresh
        and re-render page 0 — refresh re-fetches, doesn't reuse the
        cache."""
        dormants = await self.fetch_and_cache_dormants(event.sender, event.conversation)
        await self._ctx.edit_anchor(event, self.render_resume_page(dormants, page=0))
        await self._ctx.channel.ack(event, "○ Resume")

    async def on_open_archive(self, event: ActionEvent) -> None:
        """Top-level chooser's `📦 Archive(K)` button or the Archive
        sub-pane's `🔄 Refresh` button. Walks the archive tree fresh
        and re-renders page 0."""
        archives = await self.fetch_and_cache_archives(event.sender, event.conversation)
        await self._ctx.edit_anchor(event, self.render_archive_page(archives, page=0))
        await self._ctx.channel.ack(event, "📦 Archive")

    async def on_archive_pick(self, event: ActionEvent) -> None:
        """Tap on an archived row → repaint to the archived row-detail
        card (View / Restore / Back / Dismiss)."""
        sid = event.value.get("sid", "")
        cwd_str = event.value.get("cwd", "")
        file_path_str = event.value.get("file_path", "")
        if not sid or not cwd_str or not file_path_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        card = build_archived_detail_card(sid=sid, cwd=Path(cwd_str), file_path=Path(file_path_str))
        await self._ctx.edit_anchor(event, card)
        await self._ctx.channel.ack(event, "⚙ " + (Path(cwd_str).name or sid[:8]))

    async def on_archive_page(self, event: ActionEvent) -> None:
        """Page-tap on the Archive sub-pane. Slices the cached listing
        if present, refetches on miss (same shape as `on_dormant_page`)."""
        try:
            page = int(event.value.get("i", "0"))
        except ValueError:
            page = 0
        key = _conv_key(event.sender, event.conversation)
        archives = self._archive_cache.get(key)
        if archives is None:
            archives = await self.fetch_and_cache_archives(event.sender, event.conversation)
        await self._ctx.edit_anchor(event, self.render_archive_page(archives, page=page))
        page_count = max(1, (len(archives) + _RESUME_PAGE_SIZE - 1) // _RESUME_PAGE_SIZE)
        await self._ctx.channel.ack(event, f"Page {min(page, page_count - 1) + 1}/{page_count}")

    async def fetch_and_cache_archives(
        self, sender: Person, conversation: Conversation
    ) -> list[DormantSession]:
        """Walk the archive tree, cache the result for this conversation,
        and return it sorted by cwd. Mirrors `fetch_and_cache_dormants`."""
        archives = await self._ctx.archive_index(self._ctx.archive_root)
        archives_sorted = sorted(archives, key=lambda d: str(d.cwd).lower())
        self._archive_cache[_conv_key(sender, conversation)] = archives_sorted
        return archives_sorted

    def render_archive_page(self, archives: list[DormantSession], page: int) -> Card:
        """Build the Archive sub-pane card for one page. Same column_set
        faux-table layout as Resume; the per-row pick action goes to
        the archive detail card. Self-action for Refresh / pagination
        is ACTION_OPEN_ARCHIVE / ACTION_ARCHIVE_PAGE."""
        total = len(archives)
        if total == 0:
            body = (
                "*No archived sessions.*  \n_Tap 📦 Archive on a dormant session to move it here._"
            )
            rows: tuple[tuple[Action, ...], ...] = (subpane_nav(self_action=ACTION_OPEN_ARCHIVE),)
            return Card(
                text=body,
                rows=rows,
                header_title="📦 Archive",
                header_color="grey",
            )
        page_count = max(1, (total + _RESUME_PAGE_SIZE - 1) // _RESUME_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        slice_start = page * _RESUME_PAGE_SIZE
        slice_end = slice_start + _RESUME_PAGE_SIZE
        page_archives = archives[slice_start:slice_end]
        body = f"*{total} archived session(s) — page {page + 1} of {page_count}*"
        data_rows: list[tuple[TextCell | ActionCell, ...]] = [
            self._archive_column_row(d) for d in page_archives
        ]
        listing_rows: list[tuple[Action, ...]] = []
        if page_count > 1:
            page_buttons: list[Action] = []
            if page > 0:
                page_buttons.append(
                    Action(
                        label="◀ Prev",
                        action_id=ACTION_ARCHIVE_PAGE,
                        value={"i": str(page - 1)},
                    )
                )
            page_buttons.append(
                Action(
                    label=f"{page + 1}/{page_count}",
                    action_id=ACTION_ARCHIVE_PAGE,
                    value={"i": str(page)},
                )
            )
            if page < page_count - 1:
                page_buttons.append(
                    Action(
                        label="Next ▶",
                        action_id=ACTION_ARCHIVE_PAGE,
                        value={"i": str(page + 1)},
                    )
                )
            listing_rows.append(tuple(page_buttons))
        listing_rows.append(subpane_nav(self_action=ACTION_OPEN_ARCHIVE))
        return Card(
            text=body,
            column_set_rows=tuple(data_rows),
            rows=tuple(listing_rows),
            header_title="📦 Archive",
            header_color="grey",
        )

    async def on_dormant_page(self, event: ActionEvent) -> None:
        """Page-tap on the Resume sub-pane. Slices from the cached
        listing — no JSONL re-walk. If the cache is empty (process
        restarted between refresh and page tap), do a fresh fetch."""
        try:
            page = int(event.value.get("i", "0"))
        except ValueError:
            page = 0
        key = _conv_key(event.sender, event.conversation)
        dormants = self._dormant_cache.get(key)
        if dormants is None:
            dormants = await self.fetch_and_cache_dormants(event.sender, event.conversation)
        await self._ctx.edit_anchor(event, self.render_resume_page(dormants, page=page))
        page_count = max(1, (len(dormants) + _RESUME_PAGE_SIZE - 1) // _RESUME_PAGE_SIZE)
        await self._ctx.channel.ack(event, f"Page {min(page, page_count - 1) + 1}/{page_count}")

    async def on_open_new(self, event: ActionEvent) -> None:
        await self._ctx.edit_anchor(event, await self.build_new_listing_card())
        await self._ctx.channel.ack(event, "🆕 New")

    async def on_new_pick(self, event: ActionEvent) -> None:
        """Tap on a directory in the New sub-pane → confirmation card
        (Start / Refresh / Back / Dismiss). The two-step prevents
        accidental fresh-pane creation on a mistap."""
        cwd_str = event.value.get("cwd", "")
        if not cwd_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        cwd = Path(cwd_str)
        if not cwd.is_dir():
            await self._ctx.channel.ack(event, "Directory gone")
            await self._ctx.edit_anchor(event, await self.build_new_listing_card())
            return
        await self._ctx.edit_anchor(event, build_new_detail_card(cwd=cwd))
        await self._ctx.channel.ack(event, f"🆕 {cwd.name}")

    async def on_sessions_refresh(self, event: ActionEvent) -> None:
        """Re-render the chooser into the same anchor. Doubles as the
        Back button on either sub-pane."""
        await self.render_chooser_into_anchor(event)
        await self._ctx.channel.ack(event, "🔄")

    # ── private helpers ──────────────────────────────────────────

    def _count_active(self) -> int:
        """Count panes with a registered run pointer (the same gating
        `_build_active_rows` used to do)."""
        registry = self._ctx.registry
        return sum(1 for pid in registry.list_panes() if registry.get_run_pointer(pid))

    def _tracked_run_ids(self) -> frozenset[str]:
        ids: list[str] = []
        for pane_id in self._ctx.registry.list_panes():
            ptr = self._ctx.registry.get_run_pointer(pane_id)
            if ptr is not None:
                ids.append(ptr.run_id)
        return frozenset(ids)

    @staticmethod
    def _render_chooser_body(*, active: int, dormant: int, archive: int = 0) -> str:
        bits: list[str] = []
        if active:
            bits.append(f"*{active} active*")
        if dormant:
            bits.append(f"{dormant} dormant")
        if archive:
            bits.append(f"{archive} archived")
        if not bits:
            # No sessions of any kind — surface the New path as the
            # next step. Avoids a dead-end "no sessions" placeholder.
            return "*Sessions* — none yet · pick *🆕 New* to start"
        return "*Sessions* — " + " · ".join(bits)

    @staticmethod
    def _active_row_label(pane_id: str, pane: object, cwd: object) -> str:
        name = getattr(pane, "pane_name", "") or pane_id
        cwd_short = getattr(cwd, "name", "") or str(cwd)
        return f"📁 {name} — {cwd_short}"

    @staticmethod
    def _archive_column_row(d: DormantSession) -> tuple[TextCell | ActionCell, ...]:
        """Same column shape as `_dormant_column_row` — the archive
        listing is visually identical to Resume, just with a different
        pick action so taps route to the archived detail card."""
        basename = d.cwd.name or str(d.cwd)
        if d.mtime:
            dt = datetime.fromtimestamp(d.mtime)
            date_s = dt.strftime("%Y-%m-%d")
            time_s = dt.strftime("%H:%M")
        else:
            date_s = "—"
            time_s = ""
        time_line = f"{time_s}  \n" if time_s else ""
        return (
            TextCell(
                content=f"**{basename}**  \n<font color='grey'>`{d.cwd}`</font>",
                weight=3,
            ),
            TextCell(
                content=(f"<font color='grey'>{date_s}  \n{time_line}{d.message_count} msg</font>"),
                weight=2,
            ),
            ActionCell(
                action=Action(
                    label="⚙",
                    action_id=ACTION_ARCHIVE_PICK,
                    value={
                        "sid": d.session_id,
                        "cwd": str(d.cwd),
                        "file_path": str(d.file_path),
                    },
                )
            ),
        )

    @staticmethod
    def _dormant_column_row(d: DormantSession) -> tuple[TextCell | ActionCell, ...]:
        """Three cells per Resume row: cwd basename bold over the
        full path in grey (weight 3), date / time / N-msg stacked in
        grey (weight 2), then a small ⚙ details button (auto width)
        that drills into the per-row detail card (Resume / Delete /
        Back / Dismiss)."""
        basename = d.cwd.name or str(d.cwd)
        if d.mtime:
            dt = datetime.fromtimestamp(d.mtime)
            date_s = dt.strftime("%Y-%m-%d")
            time_s = dt.strftime("%H:%M")
        else:
            date_s = "—"
            time_s = ""
        time_line = f"{time_s}  \n" if time_s else ""
        return (
            TextCell(
                content=f"**{basename}**  \n<font color='grey'>`{d.cwd}`</font>",
                weight=3,
            ),
            TextCell(
                content=(f"<font color='grey'>{date_s}  \n{time_line}{d.message_count} msg</font>"),
                weight=2,
            ),
            ActionCell(
                action=Action(
                    label="⚙",
                    action_id=ACTION_DORMANT_PICK,
                    value={
                        "sid": d.session_id,
                        "cwd": str(d.cwd),
                        "file_path": str(d.file_path),
                    },
                )
            ),
        )
