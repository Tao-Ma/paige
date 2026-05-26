"""PsutilPrimitives — single-impl ProcPrimitives over `psutil`.

Tests inject a stub `iter_factory` that yields synthetic
`psutil.Process`-shaped objects so we never touch real psutil or the
host's process table.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import psutil

from paige.infrastructure.psutil_primitives import PsutilPrimitives


class _FakeProcess:
    """Minimal `psutil.Process` lookalike. Only the methods
    PsutilPrimitives actually calls are populated. Each accessor can
    raise `psutil.NoSuchProcess` to exercise the error-suppression
    path."""

    def __init__(
        self,
        pid: int,
        *,
        ppid: int = 1,
        name: str = "claude",
        rss: int | None = 4096 * 1024,
        cwd: str | None = "/proj",
        files: list[str] | None = None,
        raise_on: tuple[str, ...] = (),
    ) -> None:
        self.pid = pid
        self._ppid = ppid
        self._name = name
        self._rss = rss
        self._cwd = cwd
        self._files = files or []
        self._raise_on = raise_on

    def ppid(self) -> int:
        self._maybe_raise("ppid")
        return self._ppid

    def name(self) -> str:
        self._maybe_raise("name")
        return self._name

    def memory_info(self) -> _FakeMemInfo:
        self._maybe_raise("memory_info")
        return _FakeMemInfo(rss=self._rss or 0)

    def cwd(self) -> str | None:
        self._maybe_raise("cwd")
        return self._cwd

    def open_files(self) -> list[_FakeOpenFile]:
        self._maybe_raise("open_files")
        return [_FakeOpenFile(path=p, fd=21 + i) for i, p in enumerate(self._files)]

    def _maybe_raise(self, name: str) -> None:
        if name in self._raise_on:
            raise psutil.NoSuchProcess(self.pid)


class _FakeMemInfo:
    def __init__(self, rss: int) -> None:
        self.rss = rss


class _FakeOpenFile:
    def __init__(self, path: str, fd: int) -> None:
        self.path = path
        self.fd = fd


def _make(
    procs: list[_FakeProcess],
    *,
    cache_ttl: float = 0.0,
) -> tuple[PsutilPrimitives, list[int]]:
    """Build a primitives instance whose iter_factory yields `procs`,
    and a counter list that records each factory invocation (asserts
    on snapshot caching)."""
    calls: list[int] = []

    def factory() -> Iterable[_FakeProcess]:
        calls.append(1)
        return iter(procs)

    return PsutilPrimitives(iter_factory=factory, cache_ttl=cache_ttl), calls  # type: ignore[arg-type]


# ── basic field plumbing ────────────────────────────────────────


def test_get_comm_returns_process_name() -> None:
    p, _ = _make([_FakeProcess(100, name="claude")])
    assert p.get_comm(100) == "claude"


def test_get_rss_bytes_passes_through() -> None:
    p, _ = _make([_FakeProcess(100, rss=12345)])
    assert p.get_rss_bytes(100) == 12345


def test_get_cwd_wraps_string_in_path() -> None:
    p, _ = _make([_FakeProcess(100, cwd="/Users/alice/proj")])
    assert p.get_cwd(100) == Path("/Users/alice/proj")


def test_get_children_finds_by_ppid() -> None:
    p, _ = _make(
        [
            _FakeProcess(100, ppid=1),
            _FakeProcess(200, ppid=100),
            _FakeProcess(201, ppid=100),
            _FakeProcess(300, ppid=1),
        ]
    )
    assert sorted(p.get_children(100)) == [200, 201]
    assert p.get_children(300) == []


def test_unknown_pid_returns_none() -> None:
    p, _ = _make([_FakeProcess(100)])
    assert p.get_comm(404) is None
    assert p.get_rss_bytes(404) is None
    assert p.get_cwd(404) is None
    assert p.get_open_task_uuids(404) == []


# ── tasks-dir uuid extraction ───────────────────────────────────


def test_extracts_uuid_from_open_lock_file() -> None:
    """Real Claude Code holds `<tasks_dir>/.lock` open; psutil reports
    that regular file in `open_files()`."""
    uuid = "a3262957-d0e4-4031-b258-13c210fd371a"
    p, _ = _make([_FakeProcess(100, files=[f"/Users/x/.claude/tasks/{uuid}/.lock"])])
    assert p.get_open_task_uuids(100) == [uuid]


def test_dedups_uuid_seen_in_multiple_paths() -> None:
    """A claude process may have several fds under the same task dir
    (e.g. `.lock` + `1.json`); they collapse to one uuid entry."""
    uuid = "11111111-1111-4111-8111-111111111111"
    p, _ = _make(
        [
            _FakeProcess(
                100,
                files=[
                    f"/Users/x/.claude/tasks/{uuid}/.lock",
                    f"/Users/x/.claude/tasks/{uuid}/1.json",
                ],
            )
        ]
    )
    assert p.get_open_task_uuids(100) == [uuid]


def test_collects_multiple_uuids_for_one_pid() -> None:
    """`/clear` leak: claude ends up with fds under both the source
    and the rotated task dir."""
    a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    p, _ = _make(
        [
            _FakeProcess(
                100,
                files=[
                    f"/Users/x/.claude/tasks/{a}/.lock",
                    f"/Users/x/.claude/tasks/{b}/.lock",
                ],
            )
        ]
    )
    assert set(p.get_open_task_uuids(100)) == {a, b}


def test_ignores_non_tasks_paths() -> None:
    p, _ = _make(
        [
            _FakeProcess(
                100,
                files=[
                    "/Users/x/.claude/history.jsonl",
                    "/Users/x/.claude/settings.json",
                    "/usr/lib/whatever.so",
                ],
            )
        ]
    )
    assert p.get_open_task_uuids(100) == []


def test_rejects_non_uuid_segment_under_tasks() -> None:
    """`tasks/archived/junk` is not a uuid; skip."""
    p, _ = _make([_FakeProcess(100, files=["/Users/x/.claude/tasks/archived/junk"])])
    assert p.get_open_task_uuids(100) == []


# ── error suppression ──────────────────────────────────────────


def test_no_such_process_during_walk_drops_just_that_pid() -> None:
    """One process raises `NoSuchProcess` mid-walk (it exited between
    the iterator yielding and our accessor) — its entry gets None
    fields; everyone else is fine."""
    p, _ = _make(
        [
            _FakeProcess(100, name="claude"),
            _FakeProcess(
                200,
                raise_on=("name", "cwd", "memory_info", "open_files", "ppid"),
            ),
            _FakeProcess(300, name="bash"),
        ]
    )
    assert p.get_comm(100) == "claude"
    assert p.get_comm(200) is None  # all accessors raised → None
    assert p.get_comm(300) == "bash"


def test_access_denied_on_open_files_falls_through_to_no_uuids() -> None:
    p, _ = _make(
        [
            _FakeProcess(
                100,
                raise_on=("open_files",),
                files=["/Users/x/.claude/tasks/a/.lock"],
            )
        ]
    )
    assert p.get_open_task_uuids(100) == []
    # Other fields still readable.
    assert p.get_comm(100) == "claude"


# ── snapshot caching ───────────────────────────────────────────


def test_ttl_cache_amortises_iter_calls() -> None:
    """A burst of method calls inside one tick should walk processes
    exactly once — that's the entire reason for the snapshot."""
    p, calls = _make([_FakeProcess(100)], cache_ttl=60.0)
    p.get_comm(100)
    p.get_cwd(100)
    p.get_rss_bytes(100)
    p.get_children(100)
    p.get_open_task_uuids(100)
    assert len(calls) == 1


def test_invalidate_forces_resnapshot() -> None:
    p, calls = _make([_FakeProcess(100)], cache_ttl=60.0)
    p.get_comm(100)
    p.invalidate()
    p.get_comm(100)
    assert len(calls) == 2


def test_zero_ttl_rebuilds_every_call() -> None:
    """ttl=0 disables caching — convenient for tests that want to
    swap canned process lists mid-stream."""
    p, calls = _make([_FakeProcess(100)], cache_ttl=0.0)
    p.get_comm(100)
    p.get_comm(100)
    assert len(calls) == 2


# ── cgroup memory (Linux-only; macOS path returns (None, None)) ─


def test_cgroup_memory_returns_a_pair() -> None:
    """We don't assert specific values (depends on host kernel /
    container), only the shape: a 2-tuple of int|None."""
    p, _ = _make([])
    used, limit = p.get_cgroup_memory()
    assert used is None or isinstance(used, int)
    assert limit is None or isinstance(limit, int)


# ── default iter_factory smoke ─────────────────────────────────


def test_default_iter_walks_real_processes() -> None:
    """Sanity check that the default factory is wired correctly —
    `psutil.process_iter()` always returns at least the current pid."""
    import os

    p = PsutilPrimitives()  # uses default iter_factory
    me = os.getpid()
    # Our own process should be visible. comm varies (`python3`, `pytest`,
    # `python3.12`); just assert non-None rather than equality.
    assert p.get_comm(me) is not None
    assert p.get_rss_bytes(me) is not None
