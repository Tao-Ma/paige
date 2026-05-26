"""WatcherRouter — host_id → Watcher adapter dispatch.

Same shape as `MultiplexerRouter`: implements the `Watcher`
Protocol itself, holds a `dict[host_id, Watcher]`, dispatches
`track(...)` by host. Today there's only one entry, `local`,
wrapping the JSONL-on-local-fs adapter; the SSH adapter slice
will register an `ssh-tail`-backed Watcher under each remote
host_id.

Lifecycle (`start / stop / flush`) and handler registration
(`on_event`) fan out to every wrapped watcher — events from any
host reach every subscribed handler, with `flush` summing the
per-host event counts. `untrack(run_id)` also fans out: we don't
keep the run_id → host_id mapping at the router (would be a
small extra state file to persist), and untrack on a watcher that
doesn't have the run is a no-op anyway, so fan-out is the
simplest contract.

Constructor invariant: a `local` entry is required. It's the
unknown-host fallback (handles stale registry pointers referring
to a host removed from `~/.paige/hosts.toml`) and the home of
paige's own machinery.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.host import LOCAL_HOST_ID
from ..ports.watcher import EventHandler, Watcher


class WatcherRouter:
    """Watcher impl that dispatches `track` by `host_id` and fans
    out lifecycle to every wrapped adapter."""

    def __init__(self, adapters: dict[str, Watcher]) -> None:
        if LOCAL_HOST_ID not in adapters:
            raise ValueError(
                f"WatcherRouter requires a {LOCAL_HOST_ID!r} entry "
                "(the local-host adapter is the unknown-host fallback "
                "and is also where paige's own transcripts are tailed)."
            )
        self._adapters: dict[str, Watcher] = dict(adapters)

    def for_host(self, host_id: str) -> Watcher:
        """Resolve the adapter for `host_id`, falling back to the
        local adapter on unknown ids. Symmetric with
        `MultiplexerRouter.for_host`."""
        return self._adapters.get(host_id, self._adapters[LOCAL_HOST_ID])

    # ── lifecycle (fanned out across every wrapped watcher) ─────

    async def start(self) -> None:
        for watcher in self._adapters.values():
            await watcher.start()

    async def stop(self) -> None:
        # Best-effort stop everything; one failed adapter shouldn't
        # leave the others running. Any exception from a backend
        # surfaces after the rest are stopped.
        first_exc: BaseException | None = None
        for watcher in self._adapters.values():
            try:
                await watcher.stop()
            except Exception as e:  # noqa: BLE001
                if first_exc is None:
                    first_exc = e
        if first_exc is not None:
            raise first_exc

    async def flush(self) -> int:
        total = 0
        for watcher in self._adapters.values():
            total += await watcher.flush()
        return total

    def on_event(self, handler: EventHandler) -> None:
        # Register the handler with every backend so events from any
        # host reach the same handler chain.
        for watcher in self._adapters.values():
            watcher.on_event(handler)

    # ── per-run subscription (host-aware) ───────────────────────

    def track(
        self,
        run_id: str,
        transcript_path: Path,
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> None:
        self.for_host(host_id).track(run_id, transcript_path)

    def untrack(self, run_id: str) -> None:
        # Fan out: untrack is idempotent on watchers that don't
        # have the run, and it saves us from persisting a
        # router-level run_id → host_id table.
        for watcher in self._adapters.values():
            watcher.untrack(run_id)


__all__ = ["WatcherRouter"]
