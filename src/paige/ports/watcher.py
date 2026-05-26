"""Watcher — the JSONL transcript watcher port.

A `Watcher` polls (or otherwise observes) Claude Code's JSONL
transcript files and emits `TranscriptEvent`s as new entries land.
The implementation handles incremental reads (byte offsets,
truncation detection) so subscribers see each entry exactly once.

`flush()` exists for test ergonomics + cross-loop synchronization
in the application layer (status detection wants "have you read
everything Claude has written yet?" before deciding idle).

**Host-awareness.** `track()` accepts a `host_id` kwarg (default
`"local"`). Single-host adapters (`JsonlWatcher`, `FakeWatcher`)
accept and ignore it — they always tail the local filesystem.
The `WatcherRouter` impl uses `host_id` to dispatch to the right
adapter when multi-host config is in play. `untrack()` doesn't
take `host_id` because the router fans the call out to every
wrapped adapter (idempotent when the run isn't tracked there) —
saves call sites from threading the id through.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.host import LOCAL_HOST_ID
from ..domain.transcript import TranscriptEvent

# (run_id, event) — handlers tracking multiple transcripts need the
# run_id to route the event to its conversation. TranscriptEvent
# itself only carries content, not identity.
EventHandler = Callable[[str, TranscriptEvent], Awaitable[None]]


@runtime_checkable
class Watcher(Protocol):
    """JSONL transcript watcher."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def flush(self) -> int:
        """Force a synchronous read of all tracked transcripts.
        Returns the number of new events emitted. Subscribers'
        handlers are awaited before flush returns."""
        ...

    def on_event(self, handler: EventHandler) -> None: ...

    def track(
        self,
        run_id: str,
        transcript_path: Path,
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> None:
        """Begin watching this transcript. Idempotent: tracking the
        same run_id twice is a no-op. `host_id` selects which
        backend reads the file (the local fs reader, an SSH-tail
        in a future slice, etc.); single-host adapters ignore it."""
        ...

    def untrack(self, run_id: str) -> None: ...
