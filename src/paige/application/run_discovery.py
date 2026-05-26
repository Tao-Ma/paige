"""RunDiscovery — periodic scan of tmux panes for live Claude runs.

For each pane the multiplexer reports, the foreground pid is read,
then `discover_run(pid)` (defaults to the /proc fd-walk in
`paige.application.proc_scan`) returns a `(run_id, cwd)` tuple
when the process has an open JSONL under `~/.claude/projects/`.

Discovered runs are written into RunRegistry via `register_run`,
making them visible to /sessions and routing the watcher's events
to bound conversations.

This is paige's session-discovery mechanism — it replaces an
install-time Claude Code hook with a periodic /proc scan. /proc
is authoritative while the process is alive, and re-running the
scan picks up session-id changes (e.g. after `/clear`) without
coordination. The trade-off is a poll interval of latency vs. zero
setup; for a chat bot this is the right shape.

Idempotency: registering the same `(pane, run_id, cwd)` twice is a
no-op for the registry. We deliberately re-register every tick — if
sid rotated (e.g. /clear), the registry adopts the new pointer
immediately.

The scanner does NOT decide what runs ARE — it only identifies
processes' currently-open JSONL. Runs that have already exited
(JSONL on disk, no claude attached) won't be discovered here; a
JSONL-walk is needed for that, deferred.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from ..ports.multiplexer import Multiplexer
from ..ports.watcher import Watcher
from .proc_scan import discover_run as _default_discover
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

# Injected for tests; production uses the /proc-based default.
# Returns (run_id, cwd, jsonl_path). `exclude_uuids` lets the
# caller rule out run_ids already attributed to other panes this
# tick — see `tick`'s per-tick exclusion set below.
DiscoverFn = Callable[[int, frozenset[str]], "tuple[str, Path, Path] | None"]


def _default_discover_with_exclude(
    pid: int, exclude_uuids: frozenset[str]
) -> tuple[str, Path, Path] | None:
    return _default_discover(pid, exclude_uuids=exclude_uuids)


class RunDiscovery:
    """Periodic pane → /proc → registry + watcher sweep.

    For each tmux pane the multiplexer reports, this looks up the
    foreground pid, asks the discover function whether a Claude JSONL
    is open, and if so:
      - registers the run in `RunRegistry`
      - calls `Watcher.track(run_id, jsonl_path)` so transcript events
        flow into the Dispatcher.
    """

    def __init__(
        self,
        *,
        multiplexer: Multiplexer,
        registry: RunRegistry,
        watcher: Watcher,
        poll_interval: float = 10.0,
        discover: DiscoverFn = _default_discover_with_exclude,
        miss_threshold: int = 3,
    ) -> None:
        self._multiplexer = multiplexer
        self._registry = registry
        self._watcher = watcher
        self._poll_interval = poll_interval
        self._discover = discover
        # Per-pane miss counter for the "claude exited but pane
        # still exists" path. We don't drop the run pointer on the
        # first failed discovery — a freshly-spawned pane has a
        # startup-race window where claude isn't yet holding its
        # JSONL fd open, and we'd otherwise flicker the pane out of
        # /sessions Active until claude finished bootstrapping.
        # `miss_threshold` consecutive failed ticks → drop. With the
        # default 10 s poll, threshold=3 ≈ 30 s grace.
        self._misses: dict[str, int] = {}
        self._miss_threshold = miss_threshold
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="run-discovery")

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
                logger.warning("RunDiscovery tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue

    async def tick(self) -> int:
        """One scan cycle. Returns count of panes that resolved to a run.

        Public so tests drive it deterministically.

        Two cleanup paths beyond the basic register-on-success:

        - **claude exited, pane still in tmux** — the foreground
          reverts to the shell; `discover_run` returns None. After
          `miss_threshold` consecutive misses we clear the pointer
          (and untrack the watcher) so the pane stops appearing in
          /sessions → Active. Bindings stay — the user can /unbind
          explicitly or re-spawn via /sessions.

        - **pane gone entirely** — registry has a pointer for a
          pane the multiplexer no longer reports. Cascade-unbind
          via `RunRegistry.remove_pane` so the binding doesn't
          point at a phantom pane.
        """
        panes = await self._multiplexer.list_panes()
        live_pane_ids = {p.pane_id for p in panes}
        registered = 0
        # Per-tick exclusion set: run_ids already attributed to a
        # pane in this sweep. Threaded into `discover_run` so the
        # project-dir mtime fallback for the *next* pane doesn't
        # pick the same JSONL (two claudes sharing a cwd → both
        # candidate sets contain the same uuids; without this, both
        # panes flip-flop onto the most-recently-written one).
        attributed: set[str] = set()
        for pane in panes:
            pid = await self._multiplexer.get_foreground_pid(pane.pane_id)
            result = self._discover(pid, frozenset(attributed)) if pid is not None else None
            if result is None:
                await self._handle_miss(pane.pane_id)
                continue
            # Success → reset the miss counter for this pane.
            self._misses.pop(pane.pane_id, None)
            run_id, cwd, jsonl_path = result
            attributed.add(run_id)
            # Capture the prior pointer BEFORE we overwrite — if the
            # pane rotated to a new sid (i.e. the user ran /clear),
            # we need to stop the watcher tailing the abandoned JSONL.
            old_ptr = self._registry.get_run_pointer(pane.pane_id)
            await self._registry.register_run(pane.pane_id, run_id, cwd)
            self._watcher.track(run_id, jsonl_path)
            if old_ptr is None:
                logger.info(
                    "discovery: pane %s registered run %s (cwd=%s)", pane.pane_id, run_id, cwd
                )
            elif old_ptr.run_id != run_id:
                logger.info(
                    "discovery: pane %s run rotated %s → %s (likely /clear)",
                    pane.pane_id,
                    old_ptr.run_id,
                    run_id,
                )
                self._watcher.untrack(old_ptr.run_id)
            registered += 1
        # Vanished panes: drop pointer + cascade-unbind. Cheap loop —
        # the registry's pane list is bounded by the user's actual
        # session count.
        for pane_id in list(self._registry.list_panes()):
            if pane_id in live_pane_ids:
                continue
            await self._cleanup_gone_pane(pane_id)
        return registered

    async def _handle_miss(self, pane_id: str) -> None:
        """Discovery turned up nothing this tick. If the pane has a
        registered run pointer, count it as a miss; once we hit
        `_miss_threshold` consecutive misses, clear the pointer
        (claude has exited; the pane is just a shell)."""
        if self._registry.get_run_pointer(pane_id) is None:
            return  # nothing to clear
        n = self._misses.get(pane_id, 0) + 1
        self._misses[pane_id] = n
        if n < self._miss_threshold:
            return
        ptr = self._registry.get_run_pointer(pane_id)
        if ptr is not None:
            logger.info(
                "discovery: pane %s cleared (%d misses); claude likely exited; run was %s",
                pane_id,
                n,
                ptr.run_id,
            )
            self._watcher.untrack(ptr.run_id)
        await self._registry.clear_run(pane_id)
        self._misses.pop(pane_id, None)

    async def _cleanup_gone_pane(self, pane_id: str) -> None:
        """The multiplexer no longer reports this pane (tmux window
        was killed, the host rebooted, etc.). Cascade-unbind via
        `remove_pane` so any sticky bindings drop too."""
        ptr = self._registry.get_run_pointer(pane_id)
        if ptr is not None:
            self._watcher.untrack(ptr.run_id)
        await self._registry.remove_pane(pane_id)
        self._misses.pop(pane_id, None)


__all__ = ["DiscoverFn", "RunDiscovery"]
