"""JsonlWatcher — file-polling adapter implementing `paige.ports.watcher.Watcher`.

Each tracked transcript has a byte-offset cursor; on every poll the
watcher reads new bytes since the cursor, parses them via
`JsonlParser`, and emits one `TranscriptEvent` per parsed entry to
every subscribed handler.

Cursors are persisted via the `Storage` port under a single key,
so a restart doesn't re-deliver events the previous process already
emitted. Truncation (file shrunk — Claude Code's `/clear` rewrites
the JSONL) is detected (`offset > size`) and the cursor is reset.

When tracking a brand-new run (no persisted offset) whose JSONL
already has bytes on disk, the watcher does NOT replay history
(would flood multi-MB sessions). It does, however, scan once for
`tool_use` blocks whose `tool_id` has no matching `tool_result`
later in the file — these are unanswered tool calls (typically
`AskUserQuestion`) where Claude is blocked waiting on the user. The
unanswered ones get seeded into the event stream so the IM surface
shows the question; everything else is treated as history and
skipped. The cursor is then set to current EOF for normal tailing.

`flush()` is the application-layer's "have you read everything
Claude has written?" hook — used before deciding whether a session
is idle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..domain.host import LOCAL_HOST_ID
from ..domain.transcript import Block, BlockKind, TranscriptEvent
from ..infrastructure.jsonl_parser import JsonlParser
from ..ports.storage import Storage
from ..ports.watcher import EventHandler

_STATE_KEY = "jsonl_watcher_state"

logger = logging.getLogger(__name__)


class JsonlWatcher:
    """Polling JSONL watcher; implements `paige.ports.watcher.Watcher`."""

    def __init__(
        self,
        storage: Storage,
        poll_interval: float = 2.0,
    ) -> None:
        self._storage = storage
        self._poll_interval = poll_interval
        self._tracked: dict[str, Path] = {}
        self._offsets: dict[str, int] = {}
        # run_id → file size at the moment track() was called. The
        # first `_read_one` after track() consults this: if there's
        # no carry-forward cursor (a brand-new attach), bytes in
        # [0, attach_size] are pre-existence history — seed-scanned
        # for unanswered tool_use blocks, then skipped. If a saved
        # offset was loaded by `_load_state`, the run_id has been
        # seen before and there's no history-skip semantics; we just
        # tail from the saved cursor. Cleared after first read.
        self._attach_size: dict[str, int] = {}
        self._handlers: list[EventHandler] = []
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._poll_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._load_state()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

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
                await self.flush()
            except Exception as e:
                logger.warning("JsonlWatcher poll error: %s", e)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    # ── tracking ─────────────────────────────────────────────────

    def track(
        self,
        run_id: str,
        transcript_path: Path,
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> None:
        # `host_id` is part of the Watcher Protocol so the
        # WatcherRouter can dispatch to the right backend. This
        # adapter is the local-filesystem concrete impl — it
        # ignores the parameter and always reads the local fs.
        del host_id
        if run_id in self._tracked:
            return
        self._tracked[run_id] = transcript_path
        # Snapshot the file size now. The first `_read_one` decides
        # what to do with [0, attach_size] based on whether
        # `_load_state` populated `_offsets[run_id]` in the meantime.
        try:
            size = transcript_path.stat().st_size
        except OSError:
            size = 0
        self._attach_size[run_id] = size
        self._offsets.setdefault(run_id, 0)
        logger.info(
            "watcher: tracking run %s offset=%d size=%d path=%s",
            run_id,
            self._offsets[run_id],
            size,
            transcript_path,
        )

    def untrack(self, run_id: str) -> None:
        was_tracked = run_id in self._tracked
        self._tracked.pop(run_id, None)
        self._offsets.pop(run_id, None)
        self._attach_size.pop(run_id, None)
        if was_tracked:
            logger.info("watcher: untracking run %s", run_id)

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    # ── flush ────────────────────────────────────────────────────

    async def flush(self) -> int:
        async with self._poll_lock:
            count = 0
            for run_id, path in list(self._tracked.items()):
                count += await self._read_one(run_id, path)
            await self._save_state()
            return count

    async def _read_one(self, run_id: str, path: Path) -> int:
        try:
            stat = await asyncio.to_thread(path.stat)
        except FileNotFoundError:
            return 0
        size = stat.st_size
        seed_count = 0
        if run_id in self._attach_size:
            # First read since track() was called. If we have no
            # carry-forward cursor, [0, attach_size] is pre-existence
            # history — seed-scan it for unanswered tool_use blocks
            # (e.g. AskUserQuestion the session is blocked on), then
            # jump the cursor past it. If `_load_state` already
            # populated a saved cursor, this run_id is one we've
            # tailed before and there's no history to skip.
            attach_size = min(self._attach_size.pop(run_id), size)
            if self._offsets.get(run_id, 0) == 0 and attach_size > 0:
                seed_count = await self._seed_unanswered(run_id, path, attach_size)
                self._offsets[run_id] = attach_size
        offset = self._offsets.get(run_id, 0)
        if offset > size:
            # File was truncated (e.g. Claude Code's /clear rewrote
            # the JSONL). Reset and re-read from the top.
            logger.warning(
                "watcher: run %s file truncated (offset=%d, size=%d); resetting cursor",
                run_id,
                offset,
                size,
            )
            offset = 0
        if offset == size:
            return seed_count
        new_text, new_offset = await asyncio.to_thread(_read_from, path, offset)
        self._offsets[run_id] = new_offset
        events = JsonlParser.parse(new_text)
        for ev in events:
            await self._emit(run_id, ev)
        if events or seed_count:
            logger.info(
                "watcher: run %s read %d bytes (offset %d→%d), emitted %d events%s",
                run_id,
                new_offset - offset,
                offset,
                new_offset,
                len(events),
                f" (+{seed_count} seeded)" if seed_count else "",
            )
        return seed_count + len(events)

    async def _seed_unanswered(self, run_id: str, path: Path, end_offset: int) -> int:
        """Scan bytes [0, end_offset] and emit only `tool_use` blocks
        whose `tool_id` has no matching `tool_result` later in that
        range. One-shot at first read — covers the case where Claude
        is blocked on `AskUserQuestion` when paige attaches.
        """
        text, _ = await asyncio.to_thread(_read_range, path, 0, end_offset)
        events = JsonlParser.parse(text)
        seeds = _select_unanswered_tool_uses(events)
        for ev in seeds:
            await self._emit(run_id, ev)
        return len(seeds)

    async def _emit(self, run_id: str, ev: TranscriptEvent) -> None:
        for handler in self._handlers:
            try:
                await handler(run_id, ev)
            except Exception as e:
                logger.warning("JsonlWatcher handler error: %s", e)

    # ── state persistence ────────────────────────────────────────

    async def _load_state(self) -> None:
        state = await self._storage.load(_STATE_KEY)
        if state is None:
            return
        for run_id, val in state.items():
            if isinstance(val, int):
                self._offsets[run_id] = val

    async def _save_state(self) -> None:
        snapshot: dict[str, Any] = dict(self._offsets)
        await self._storage.save(_STATE_KEY, snapshot)


def _read_from(path: Path, offset: int) -> tuple[str, int]:
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
        new_offset = f.tell()
    return data.decode("utf-8", errors="replace"), new_offset


def _read_range(path: Path, start: int, end: int) -> tuple[str, int]:
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(end - start)
    return data.decode("utf-8", errors="replace"), end


def _select_unanswered_tool_uses(events: list[TranscriptEvent]) -> list[TranscriptEvent]:
    """Return derived events containing only `tool_use` blocks whose
    `tool_id` has no matching `tool_result` later in `events`.

    Other block kinds are stripped. Events with no surviving blocks
    are dropped. Order is preserved so emit order matches JSONL order.
    """
    answered: set[str] = set()
    for ev in events:
        for block in ev.blocks:
            if block.kind is BlockKind.TOOL_RESULT and block.tool_id is not None:
                answered.add(block.tool_id)
    out: list[TranscriptEvent] = []
    for ev in events:
        kept: tuple[Block, ...] = tuple(
            b
            for b in ev.blocks
            if b.kind is BlockKind.TOOL_USE and b.tool_id is not None and b.tool_id not in answered
        )
        if kept:
            out.append(replace(ev, blocks=kept))
    return out
