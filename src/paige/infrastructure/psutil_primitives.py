"""PsutilPrimitives — single `ProcPrimitives` impl over `psutil`.

Cross-platform by construction: `psutil` ships its own per-platform
backends (procfs on Linux, libproc on macOS, NtQuery* on Windows,
sysctl on BSDs) and exposes one Python API. One impl, one set of
tests, one e2e — the Linux box's e2e validates exactly the same code
path that runs on macOS.

Why psutil instead of the lsof+ps subprocess pair we shipped first
(commit 68d0cd2): no system-binary dep on the deploy host, no
subprocess fork+exec overhead per snapshot, typed exceptions
(`NoSuchProcess`, `AccessDenied`), and we don't have to parse
`lsof -Fpcfn` text. The trade-off is a ~3 MB compiled wheel dep —
paige already pulls many similar-size deps, so the extra weight is
rounding error.

Per-tick discipline
-------------------
Each Protocol method (`get_cwd`, `get_open_task_uuids`, …) is a single
dict lookup against a snapshot. The snapshot is built lazily on first
call and reused for `cache_ttl` seconds — typically 1 s — so a burst
of accesses inside one RunDiscovery tick costs exactly one
`psutil.process_iter()` pass regardless of how many panes / pids the
tick inspects.

Snapshot composition
--------------------
A single `psutil.process_iter()` walks every running process. We
query `pid`, `ppid`, `name`, `cwd`, `memory_info`, and `open_files`
on each — psutil reads those fields lazily from the platform's
process API. We then post-filter `open_files()` for paths containing
`/.claude/tasks/<uuid>/` and pull the uuid segment.

`open_files()` returns regular files only — the directory fd Claude
holds against `~/.claude/tasks/<sid>/` itself doesn't show up, but
the `.lock` regular file under that directory does, and that's what
we extract the uuid from. (Real Claude Code holds both fds open;
`lsof` saw both, psutil sees the regular-file one. Equivalent for
discovery.)

Cgroup memory
-------------
`get_cgroup_memory` reads `/sys/fs/cgroup/memory.current` +
`/sys/fs/cgroup/memory.max` directly on Linux; macOS has no
equivalent and returns `(None, None)` so the `/server` Storage card
renders that row as `—`. This is the *only* platform-aware branch.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

_TASKS_MARKER = "/.claude/tasks/"
_UUID_LEN = 36
_DEFAULT_CACHE_TTL = 1.0

IterFactory = Callable[[], Iterable[psutil.Process]]


@dataclass(frozen=True)
class _ProcInfo:
    """Per-pid snapshot data. All fields optional so partial coverage
    (e.g. AccessDenied on cwd or open_files) doesn't poison a record."""

    pid: int
    ppid: int | None = None
    comm: str | None = None
    rss_bytes: int | None = None
    cwd: Path | None = None
    task_uuids: tuple[str, ...] = field(default_factory=tuple)


class PsutilPrimitives:
    """`ProcPrimitives` over `psutil`. See module docstring.

    `iter_factory` is overridable so unit tests pass a stub that
    yields synthetic process objects without touching real psutil;
    production uses `psutil.process_iter`."""

    def __init__(
        self,
        iter_factory: IterFactory | None = None,
        cache_ttl: float = _DEFAULT_CACHE_TTL,
    ) -> None:
        self._iter = iter_factory or _default_iter
        self._ttl = cache_ttl
        self._snapshot: dict[int, _ProcInfo] | None = None
        self._snapshot_at: float = 0.0

    # ── Protocol methods (per-pid) ──────────────────────────────────

    def get_cwd(self, pid: int) -> Path | None:
        info = self._lookup(pid)
        return info.cwd if info is not None else None

    def get_children(self, pid: int) -> list[int]:
        snap = self._snap()
        return [info.pid for info in snap.values() if info.ppid == pid]

    def get_rss_bytes(self, pid: int) -> int | None:
        info = self._lookup(pid)
        return info.rss_bytes if info is not None else None

    def get_comm(self, pid: int) -> str | None:
        info = self._lookup(pid)
        return info.comm if info is not None else None

    def get_cgroup_memory(self) -> tuple[int | None, int | None]:
        if sys.platform != "linux":
            return (None, None)
        used: int | None = None
        limit: int | None = None
        try:
            with open("/sys/fs/cgroup/memory.current") as f:
                used = int(f.read().strip())
        except (OSError, ValueError):
            pass
        try:
            with open("/sys/fs/cgroup/memory.max") as f:
                raw = f.read().strip()
                limit = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            pass
        return used, limit

    def get_open_task_uuids(self, pid: int) -> list[str]:
        info = self._lookup(pid)
        return list(info.task_uuids) if info is not None else []

    # ── snapshot machinery ──────────────────────────────────────────

    def invalidate(self) -> None:
        """Drop the cached snapshot. Tests use this; production code
        relies on the TTL."""
        self._snapshot = None
        self._snapshot_at = 0.0

    def _lookup(self, pid: int) -> _ProcInfo | None:
        return self._snap().get(pid)

    def _snap(self) -> dict[int, _ProcInfo]:
        now = time.monotonic()
        if self._snapshot is None or (now - self._snapshot_at) > self._ttl:
            self._snapshot = self._build_snapshot()
            self._snapshot_at = now
        return self._snapshot

    def _build_snapshot(self) -> dict[int, _ProcInfo]:
        """Walk every process via the iter factory; harvest per-pid
        info while suppressing per-process errors (a zombie or a
        permission-denied process shouldn't poison the whole snapshot).
        """
        infos: dict[int, _ProcInfo] = {}
        for proc in self._iter():
            try:
                pid = proc.pid
            except psutil.Error:
                continue
            ppid = _safe(proc.ppid)
            comm = _safe(proc.name)
            mem = _safe(proc.memory_info)
            rss = mem.rss if mem is not None else None
            cwd_str = _safe(proc.cwd)
            files = _safe(proc.open_files) or []
            cwd = Path(cwd_str) if cwd_str else None
            uuids = _extract_task_uuids(f.path for f in files)
            infos[pid] = _ProcInfo(
                pid=pid,
                ppid=ppid,
                comm=comm,
                rss_bytes=rss,
                cwd=cwd,
                task_uuids=uuids,
            )
        return infos


def _default_iter() -> Iterable[psutil.Process]:
    """Production iter_factory: every running process. psutil's
    `process_iter` is lazy — per-process metadata is fetched on
    attribute access, so requesting only the fields we use keeps the
    walk cheap."""
    return psutil.process_iter()


def _safe[T](call: Callable[[], T]) -> T | None:
    """Run `call()` and suppress psutil's per-process errors —
    `NoSuchProcess` (process exited mid-walk), `AccessDenied`
    (sandboxed peer), `ZombieProcess` (terminated, not reaped). We'd
    rather have partial data than abort the whole snapshot when one
    process is unreadable."""
    try:
        return call()
    except (psutil.Error, OSError):
        return None


def _extract_task_uuids(paths: Iterable[str]) -> tuple[str, ...]:
    """Pull `<uuid>` from any path containing `/.claude/tasks/<UUID>/...`.
    Dedup while preserving first-seen order. Loose UUID check (length
    + has-dash) keeps non-uuid task subdirs (e.g. `archived/`) out."""
    seen: dict[str, None] = {}
    for p in paths:
        if _TASKS_MARKER not in p:
            continue
        tail = p.split(_TASKS_MARKER, 1)[1]
        uuid = tail.split("/", 1)[0]
        if len(uuid) != _UUID_LEN or "-" not in uuid:
            continue
        seen[uuid] = None
    return tuple(seen.keys())


__all__ = ["IterFactory", "PsutilPrimitives"]
