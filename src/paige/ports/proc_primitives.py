"""ProcPrimitives — the per-platform process-introspection port.

Run discovery and `/server` stat reading both need a small set of
"look at a pid" operations. On Linux they're served by reading
`/proc/<pid>/...`; on macOS by shelling out to `lsof`/`pgrep`/`ps`.
The shape of those operations is the same on both — only the
implementation differs. This Protocol captures the contract; the
concrete impls live in `infrastructure/{linux,macos}_primitives.py`.

Discovery itself (`proc_scan.discover_run` + its `_discover_one`
helper) is platform-free and parameterised on a `ProcPrimitives`
instance. The single signal it relies on is `get_open_task_uuids` —
the live process's fd table tells us which `~/.claude/tasks/<uuid>/`
directory is currently open, and that uuid is the live session id.
New platforms (FreeBSD, container hosts with restricted /proc,
lsof-on-Linux, etc.) only need to provide a primitives impl; the
discovery logic above doesn't change.

All methods return `None` / `[]` / `(None, None)` on missing pids
or unreadable backends — callers treat empty answers as "process
not visible from this side" and degrade gracefully (the `/sessions`
Active list goes empty rather than crashing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProcPrimitives(Protocol):
    """Per-platform pid → fact lookups used by run discovery and /server."""

    def get_cwd(self, pid: int) -> Path | None:
        """Return `pid`'s working directory, or None if the pid is gone
        / unreadable. Used by `discover_run` (it's part of the run
        pointer; combined with the run_id to compute the JSONL path
        via `transcript_path.encode_cwd`) and by `/server`'s Storage
        card."""
        ...

    def get_children(self, pid: int) -> list[int]:
        """Return immediate-child pids of `pid` (one level only).
        Empty list when there are no children or the pid is gone.
        Used by `discover_run`'s child-walk: a tmux pane's foreground
        is the shell; claude is its child."""
        ...

    def get_rss_bytes(self, pid: int) -> int | None:
        """Resident set size for `pid` in bytes. None on missing pid
        or platform without an answer."""
        ...

    def get_comm(self, pid: int) -> str | None:
        """Process basename (e.g. `claude`, `python3.12`) for `pid`.
        None on missing pid. Used by `/server` to confirm a pid is
        actually a claude process."""
        ...

    def get_cgroup_memory(self) -> tuple[int | None, int | None]:
        """Container memory `(used, limit)` in bytes. macOS has no
        cgroups → returns `(None, None)`; the `/server` Storage card
        renders that row as `—`."""
        ...

    def get_open_task_uuids(self, pid: int) -> list[str]:
        """Return every uuid `~/.claude/tasks/<uuid>/...` that `pid`
        currently has an fd open against. The **only** signal
        `discover_run` reads — the per-pid session file
        (`~/.claude/sessions/<pid>.json`) was tried as an alternative
        and dropped because Claude Code 2.1.126 freezes its
        `sessionId` field at process start and never rewrites it on
        `/clear`, so file-based discovery returns the stale uuid
        forever. The fd table, by contrast, reflects what the process
        is currently doing.

        Order is implementation-defined; callers tie-break by JSONL
        mtime. Multiple uuids can be live for one pid simultaneously
        (`claude --resume` keeps both source and forked task dirs
        open; `/clear` opens a new one without closing the old).

        Empty list when no matching fd is open or the backend can't
        introspect (missing pid, container without /proc, lsof not
        installed, etc.) — `discover_run` returns None in that case
        rather than fall back to a stale signal."""
        ...


__all__ = ["ProcPrimitives"]
