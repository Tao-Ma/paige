"""Lifecycle sub-handler — actions that mutate session state.

Six handlers, all triggered from the chooser's row-detail cards:

- `on_bind` — attach this conversation to an existing pane (Active
  detail card's 🔗 Bind).
- `on_resume` — spawn `claude --resume <sid>` in a fresh pane and
  bind the conversation to it (Resume detail card's ▶ Resume).
- `on_new_start` — spawn fresh `claude` in a chosen cwd, register
  the run, and bind (New confirmation card's 🚀 Start).
- `on_dormant_delete` — unlink the JSONL transcript and repaint the
  chooser in place (Resume detail card's 🗑 Delete).
- `on_dormant_archive` — move the JSONL to ~/.claude/archive,
  repaint the chooser (Resume detail card's 📦 Archive). Soft-delete:
  recoverable via the Archive sub-pane.
- `on_archive_restore` — move an archived JSONL back to
  ~/.claude/projects, repaint the archive sub-pane (Archive detail
  card's ♻ Restore).

Each handler ends by editing the row-detail/confirmation anchor with
a `✓ Bound` / `✓ Started` confirmation card (or in the delete /
archive / restore cases, the freshly-built chooser / sub-pane card).
The chooser sub-handler is reused for these repaints.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from ..domain.card import ActionEvent, Card
from ..domain.outbound import CardContent, Outbound
from ..infrastructure.sessions_index import (
    archive_dormant_session,
    delete_dormant_session,
    restore_archived_session,
)
from ._sessions_actions import (
    ACTION_ARCHIVE_RESTORE,
    ACTION_BIND,
    ACTION_DORMANT_ARCHIVE,
    ACTION_DORMANT_DELETE,
    ACTION_NEW_START,
    ACTION_RESUME,
)
from ._sessions_chooser import ChooserHandlers
from ._sessions_context import SessionsContext

logger = logging.getLogger(__name__)


class LifecycleHandlers:
    """Bind / resume / new-start / dormant-delete / archive / restore
    handlers.

    Takes the shared context plus a `ChooserHandlers` reference for
    the post-mutation repaints (delete → chooser, archive → chooser,
    restore → archive sub-pane).
    """

    OWNED_ACTIONS: frozenset[str] = frozenset(
        {
            ACTION_BIND,
            ACTION_RESUME,
            ACTION_NEW_START,
            ACTION_DORMANT_DELETE,
            ACTION_DORMANT_ARCHIVE,
            ACTION_ARCHIVE_RESTORE,
        }
    )

    def __init__(self, ctx: SessionsContext, chooser: ChooserHandlers) -> None:
        self._ctx = ctx
        self._chooser = chooser

    async def dispatch(self, event: ActionEvent) -> None:
        """Route an OWNED_ACTIONS event to the matching handler.
        Caller (SessionsService) gates on `OWNED_ACTIONS` membership."""
        action_id = event.action_id
        if action_id == ACTION_BIND:
            await self.on_bind(event)
        elif action_id == ACTION_RESUME:
            await self.on_resume(event)
        elif action_id == ACTION_NEW_START:
            await self.on_new_start(event)
        elif action_id == ACTION_DORMANT_DELETE:
            await self.on_dormant_delete(event)
        elif action_id == ACTION_DORMANT_ARCHIVE:
            await self.on_dormant_archive(event)
        elif action_id == ACTION_ARCHIVE_RESTORE:
            await self.on_archive_restore(event)

    async def on_new_start(self, event: ActionEvent) -> None:
        """Confirmation card's `🚀 Start` — spawn `claude` in cwd, bind
        to the new pane, repaint to a `✓ Started` confirmation."""
        cwd_str = event.value.get("cwd", "")
        if not cwd_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        cwd = Path(cwd_str)
        # `claude --session-id` validates against RFC 4122 — needs the
        # `8-4-4-4-12` dashed form. `uuid.uuid4().hex` strips dashes
        # and claude rejects with "Invalid session ID. Must be a valid
        # UUID." `str(uuid.uuid4())` keeps them.
        run_id = str(uuid.uuid4())
        try:
            pane = await self._ctx.multiplexer.create_pane(
                name=cwd.name,
                cwd=cwd if cwd.exists() else Path.home(),
                command=f"claude --session-id {run_id}",
            )
        except Exception as e:
            logger.exception("create_pane failed for %s: %s", cwd, e)
            await self._ctx.channel.ack(event, "Failed to spawn pane — see logs")
            return
        await self._ctx.registry.register_run(pane.pane_id, run_id, cwd)
        await self._ctx.registry.bind(event.sender, event.conversation, pane.pane_id)
        await self._ctx.channel.ack(event, f"🚀 {cwd.name}")
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
        self._ctx.outbox.enqueue_edit(event.sender, event.card_anchor, confirmation)

    async def on_dormant_delete(self, event: ActionEvent) -> None:
        """Unlink the JSONL transcript and re-render the chooser into
        the same anchor so the deleted row disappears in place."""
        file_path_str = event.value.get("file_path", "")
        if not file_path_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        ok = await asyncio.to_thread(
            delete_dormant_session, Path(file_path_str), projects_root=self._ctx.projects_root
        )
        if not ok:
            await self._ctx.channel.ack(event, "Delete failed — see logs")
            return
        await self._chooser.render_chooser_into_anchor(event)
        await self._ctx.channel.ack(event, "🗑 Deleted")

    async def on_dormant_archive(self, event: ActionEvent) -> None:
        """Move the dormant JSONL into the sibling archive root and
        re-render the chooser into the same anchor. Mirrors the
        delete flow but soft — the file is recoverable via the Archive
        sub-pane's ♻ Restore."""
        file_path_str = event.value.get("file_path", "")
        if not file_path_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        ok = await asyncio.to_thread(
            archive_dormant_session, Path(file_path_str), projects_root=self._ctx.projects_root
        )
        if not ok:
            await self._ctx.channel.ack(event, "Archive failed — see logs")
            return
        await self._chooser.render_chooser_into_anchor(event)
        await self._ctx.channel.ack(event, "📦 Archived")

    async def on_archive_restore(self, event: ActionEvent) -> None:
        """Move an archived JSONL back to ~/.claude/projects and
        repaint the Archive sub-pane in place — restored rows then
        appear under Resume on the next chooser open."""
        file_path_str = event.value.get("file_path", "")
        if not file_path_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        ok = await asyncio.to_thread(
            restore_archived_session,
            Path(file_path_str),
            projects_root=self._ctx.projects_root,
        )
        if not ok:
            await self._ctx.channel.ack(event, "Restore failed — see logs")
            return
        archives = await self._chooser.fetch_and_cache_archives(event.sender, event.conversation)
        await self._ctx.edit_anchor(event, self._chooser.render_archive_page(archives, page=0))
        await self._ctx.channel.ack(event, "♻ Restored")

    async def on_bind(self, event: ActionEvent) -> None:
        pane_id = event.value.get("pane_id", "")
        if not pane_id:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        pane = await self._ctx.multiplexer.find_pane(pane_id)
        if pane is None:
            await self._ctx.channel.ack(event, "Pane not found — refresh /sessions")
            return
        await self._ctx.registry.bind(event.sender, event.conversation, pane_id)
        await self._ctx.channel.ack(event, f"Bound to {pane.pane_name}")
        # CardContent (not TextContent) so FeishuChannel's inline-
        # refresh fires and Feishu repaints the clicked card atomically
        # via the click response. TextContent on a card anchor would
        # PATCH cross-type and Feishu rejects with 400.
        confirmation = Outbound(
            conversation=event.conversation,
            content=CardContent(
                card=Card(
                    text=f"✓ Bound to *{pane.pane_name}*. Send messages to interact.",
                    header_title="✓ Bound",
                    header_color="green",
                )
            ),
        )
        self._ctx.outbox.enqueue_edit(event.sender, event.card_anchor, confirmation)

    async def on_resume(self, event: ActionEvent) -> None:
        sid = event.value.get("sid", "")
        cwd_str = event.value.get("cwd", "")
        if not sid or not cwd_str:
            await self._ctx.channel.ack(event, "Invalid action")
            return
        cwd = Path(cwd_str)
        # We don't pre-create the cwd — claude --resume reads the cwd
        # from the JSONL itself. Pass our best-effort decoded path as
        # a hint; tmux will start the shell there if the path exists,
        # claude will recover the real cwd otherwise.
        try:
            pane = await self._ctx.multiplexer.create_pane(
                name=cwd.name or sid[:8],
                cwd=cwd if cwd.exists() else Path.home(),
                command=f"claude --resume {sid}",
            )
        except Exception as e:
            logger.exception("resume create_pane failed for sid=%s: %s", sid, e)
            await self._ctx.channel.ack(event, "Failed to spawn pane — see logs")
            return

        await self._ctx.registry.bind(event.sender, event.conversation, pane.pane_id)
        await self._ctx.channel.ack(event, f"Resuming {cwd.name or sid[:8]}")
        # See on_bind for the CardContent rationale.
        confirmation = Outbound(
            conversation=event.conversation,
            content=CardContent(
                card=Card(
                    text=(
                        f"▶ Resuming session in *{cwd.name or '~'}* "
                        f"(pane `{pane.pane_id}`). "
                        "Claude will pick up where it left off."
                    ),
                    header_title="✓ Bound",
                    header_color="green",
                )
            ),
        )
        self._ctx.outbox.enqueue_edit(event.sender, event.card_anchor, confirmation)
