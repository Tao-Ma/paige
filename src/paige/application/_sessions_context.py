"""Shared deps + helpers for the /sessions sub-handlers.

`SessionsContext` bundles the services every sub-handler needs
(registry, multiplexer, outbox, channel, message_seq, hosts, paths).
Each sub-handler (`ChooserHandlers`, `LifecycleHandlers`,
`ManageHandlers`) takes a context instance — keeps the constructor
surface terse and gives a single place to add cross-cutting helpers
like `edit_anchor`.

The context is frozen so a sub-handler can't accidentally rebind a
service mid-flight; the wrapped services are themselves stateful.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.card import ActionEvent, Card
from ..domain.outbound import CardContent, Outbound
from ..infrastructure.sessions_index import (
    DormantSession,
    count_archived_sessions,
    count_dormant_sessions,
    list_archived_sessions,
    list_dormant_sessions,
)
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .hosts import HostsService
from .message_seq import MessageSeqService
from .outbox import Outbox
from .run_registry import RunRegistry

# Async hook so tests can swap the JSONL walk without touching the
# filesystem; production `default_dormant_index` reads ~/.claude/projects.
DormantIndexFn = Callable[[Path, frozenset[str]], Awaitable[list[DormantSession]]]

# Cheap counterpart — counts all dormants, no summary read or cap.
# Used by the chooser body so the count reflects reality even when
# the listing reaches its summary-read limit.
DormantCountFn = Callable[[Path, frozenset[str]], Awaitable[int]]

# Archive variants take only `archive_root` — archived sessions can
# never be live, so there's no exclude-run-ids parameter.
ArchiveIndexFn = Callable[[Path], Awaitable[list[DormantSession]]]
ArchiveCountFn = Callable[[Path], Awaitable[int]]


async def default_dormant_index(
    projects_root: Path, exclude_run_ids: frozenset[str]
) -> list[DormantSession]:
    # `limit=None` returns every dormant so the Resume sub-pane can
    # paginate across the full set. The chooser body uses
    # `default_dormant_count` for the count display — cheap stat-only
    # walk — so this expensive summary-read path only runs when the
    # user actually opens Resume.
    return await asyncio.to_thread(
        list_dormant_sessions,
        projects_root,
        exclude_run_ids=exclude_run_ids,
        limit=None,
    )


async def default_dormant_count(projects_root: Path, exclude_run_ids: frozenset[str]) -> int:
    return await asyncio.to_thread(
        count_dormant_sessions,
        projects_root,
        exclude_run_ids=exclude_run_ids,
    )


async def default_archive_index(archive_root: Path) -> list[DormantSession]:
    return await asyncio.to_thread(list_archived_sessions, archive_root, limit=None)


async def default_archive_count(archive_root: Path) -> int:
    return await asyncio.to_thread(count_archived_sessions, archive_root)


@dataclass
class SessionsContext:
    """Shared deps for the /sessions sub-handlers.

    Constructed once by `SessionsService.__init__` and passed by
    reference into each sub-handler. `edit_anchor` lives here because
    every sub-handler needs the message-seq-stamped channel edit and
    duplicating the helper would drift.

    Not frozen — tests that need to swap a dep (e.g. `dormant_index`)
    rebind the field on the live context, matching the pre-refactor
    pattern where the same fields lived directly on `SessionsService`.
    """

    registry: RunRegistry
    multiplexer: Multiplexer
    outbox: Outbox
    channel: Channel
    message_seq: MessageSeqService
    hosts: HostsService | None
    projects_root: Path
    new_projects_root: Path
    archive_root: Path
    dormant_index: DormantIndexFn
    dormant_count: DormantCountFn
    archive_index: ArchiveIndexFn
    archive_count: ArchiveCountFn

    async def edit_anchor(self, event: ActionEvent, card: Card) -> None:
        """Edit `event.card_anchor` to render `card`, with msg-seq
        stamping applied so debug footers chain properly."""
        outbound = Outbound(conversation=event.conversation, content=CardContent(card=card))
        outbound, _ = self.message_seq.stamp_edit(
            event.sender, event.conversation, event.card_anchor, outbound
        )
        await self.channel.edit(event.card_anchor, outbound)
