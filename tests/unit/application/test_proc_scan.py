"""proc_scan — platform-free `discover_run` + `/server` re-exports.

Discovery logic is exercised here against a fake `ProcPrimitives`
(no /proc, no lsof). The real Linux/macOS impls are tested
separately in `test_linux_primitives.py` / `test_macos_primitives.py`.

Discovery's only signal is the live process's open
`~/.claude/tasks/<uuid>/...` fds — see `discover_run`'s docstring
for why path 0 (the per-pid session file) was dropped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from paige.application import proc_scan
from paige.infrastructure.transcript_path import transcript_path


class _FakePrimitives:
    """In-memory `ProcPrimitives` for discovery tests. Only the
    methods discovery actually calls are populated."""

    def __init__(self) -> None:
        self.cwds: dict[int, Path] = {}
        self.children: dict[int, list[int]] = {}
        self.rss: dict[int, int] = {}
        self.comm: dict[int, str] = {}
        self.task_uuids: dict[int, list[str]] = {}

    def get_cwd(self, pid: int) -> Path | None:
        return self.cwds.get(pid)

    def get_children(self, pid: int) -> list[int]:
        return self.children.get(pid, [])

    def get_rss_bytes(self, pid: int) -> int | None:
        return self.rss.get(pid)

    def get_comm(self, pid: int) -> str | None:
        return self.comm.get(pid)

    def get_cgroup_memory(self) -> tuple[int | None, int | None]:
        return (None, None)

    def get_open_task_uuids(self, pid: int) -> list[str]:
        return self.task_uuids.get(pid, [])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakePrimitives:
    """Swap proc_scan's primitives for a fake."""
    fp = _FakePrimitives()
    monkeypatch.setattr(proc_scan, "_primitives", fp)
    proc_scan._reset_canary_state()
    return fp


@pytest.fixture
def projects_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Sandbox `~/.claude/projects` to a tmp dir so `transcript_path`
    resolves there. proc_scan calls `transcript_path(run_id, cwd)` with
    the default projects_root, which reads from $HOME — monkeypatch
    HOME to redirect."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home / ".claude" / "projects"


def _write_jsonl(projects_root: Path, run_id: str, cwd: Path, *, mtime: float) -> Path:
    """Materialise `<projects_root>/<encoded_cwd>/<run_id>.jsonl` with
    a known mtime so the discoverer's mtime tie-break is deterministic."""
    jsonl = transcript_path(run_id, cwd, projects_root=projects_root)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text("")
    os.utime(jsonl, (mtime, mtime))
    return jsonl


# ── discover_run ────────────────────────────────────────────────


def test_discover_run_uses_tasks_fd(
    fake: _FakePrimitives, projects_root: Path, tmp_path: Path
) -> None:
    """The single-uuid happy path: fd walk yields one uuid → return it."""
    cwd = tmp_path / "proj"
    fake.cwds[555] = cwd
    fake.task_uuids[555] = ["live-uuid"]
    _write_jsonl(projects_root, "live-uuid", cwd, mtime=1000.0)

    result = proc_scan.discover_run(555)
    assert result is not None
    run_id, found_cwd, jsonl = result
    assert run_id == "live-uuid"
    assert found_cwd == cwd
    assert jsonl.name == "live-uuid.jsonl"


def test_discover_run_picks_newest_jsonl_among_task_fds(
    fake: _FakePrimitives, projects_root: Path, tmp_path: Path
) -> None:
    """Multiple task-dir fds (claude --resume / /clear leak) → pick
    the uuid whose JSONL was written-to most recently."""
    cwd = tmp_path / "proj"
    fake.cwds[555] = cwd
    fake.task_uuids[555] = ["older", "newer", "oldest"]
    _write_jsonl(projects_root, "oldest", cwd, mtime=100.0)
    _write_jsonl(projects_root, "older", cwd, mtime=500.0)
    _write_jsonl(projects_root, "newer", cwd, mtime=900.0)

    result = proc_scan.discover_run(555)
    assert result is not None
    assert result[0] == "newer"


def test_discover_run_returns_uuid_when_jsonl_not_yet_on_disk(
    fake: _FakePrimitives, projects_root: Path, tmp_path: Path
) -> None:
    """Brand-new claude that has opened the task-dir lock fd but
    hasn't written its first JSONL line yet — the uuid still wins
    (mtime defaults to 0.0, which is fine when there's only one
    candidate). projects_root unused but listed so the fixture sets
    HOME."""
    del projects_root  # see docstring
    cwd = tmp_path / "proj"
    fake.cwds[555] = cwd
    fake.task_uuids[555] = ["fresh-uuid"]

    result = proc_scan.discover_run(555)
    assert result is not None
    assert result[0] == "fresh-uuid"


def test_discover_run_walks_to_child(
    fake: _FakePrimitives, projects_root: Path, tmp_path: Path
) -> None:
    """Shell pid has nothing; child claude has the open task-dir fd."""
    parent_cwd = tmp_path / "shell"
    child_cwd = tmp_path / "claude"
    fake.cwds[100] = parent_cwd
    fake.cwds[101] = child_cwd
    fake.children[100] = [101]
    fake.task_uuids[101] = ["child-sid"]
    _write_jsonl(projects_root, "child-sid", child_cwd, mtime=1.0)

    result = proc_scan.discover_run(100)
    assert result is not None
    assert result[0] == "child-sid"


def test_discover_run_prefers_self_over_children(
    fake: _FakePrimitives, projects_root: Path, tmp_path: Path
) -> None:
    """If the parent has its own open task-dir fd, don't walk children."""
    parent_cwd = tmp_path / "parent"
    child_cwd = tmp_path / "child"
    fake.cwds[100] = parent_cwd
    fake.cwds[101] = child_cwd
    fake.children[100] = [101]
    fake.task_uuids[100] = ["parent-sid"]
    fake.task_uuids[101] = ["child-sid"]
    _write_jsonl(projects_root, "parent-sid", parent_cwd, mtime=1.0)
    _write_jsonl(projects_root, "child-sid", child_cwd, mtime=1.0)

    result = proc_scan.discover_run(100)
    assert result is not None
    assert result[0] == "parent-sid"


def test_discover_run_none_when_no_task_fds(fake: _FakePrimitives, tmp_path: Path) -> None:
    """No open task-dir fds, no comm match, and no children → None.
    The comm match is what lets the empty-fd path through; without it
    we'd treat any bash/shell pid in a claude project cwd as claude.
    """
    fake.cwds[555] = tmp_path / "proj"
    fake.comm[555] = "bash"
    assert proc_scan.discover_run(555) is None


def test_discover_run_uses_comm_match_when_no_task_fd_held(
    fake: _FakePrimitives,
    projects_root: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Some claude installs hold only directory fds against
    `~/.claude/tasks` — those are filtered out by psutil's
    `open_files()`, so `get_open_task_uuids()` returns []. Discovery
    must still find the run via the project-dir mtime scan, gated on
    `comm == "claude"` so we don't pick up arbitrary processes.

    Asserts the canary log tag is `[no-lock-fd]` so this shape stays
    grep-distinguishable from the `[stale-lock-fd]` shape (where the
    boot-time fd is held but stale)."""
    cwd = tmp_path / "proj"
    fresh = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    fake.cwds[555] = cwd
    fake.comm[555] = "claude"
    fake.task_uuids[555] = []  # the bug shape — no per-uuid lock fd
    _write_jsonl(projects_root, fresh, cwd, mtime=900.0)

    import logging

    with caplog.at_level(logging.INFO, logger="paige.application.proc_scan"):
        result = proc_scan.discover_run(555)

    assert result is not None
    run_id, _, jsonl = result
    assert run_id == fresh
    assert jsonl.name == f"{fresh}.jsonl"
    assert any(
        f"active sid={fresh} via project-dir mtime [no-lock-fd]" in r.message
        for r in caplog.records
    )


def test_discover_run_picks_fresh_jsonl_when_lock_fd_did_not_rotate(
    fake: _FakePrimitives,
    projects_root: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real /clear regression: Claude Code 2.1.126+ doesn't open a new
    `tasks/<sid>/.lock` fd on /clear — it keeps the boot-time lock
    open and only the JSONL filename rotates. The fd-walk uuid stays
    pinned to the OLD sid forever; the active sid only shows up as a
    fresh JSONL under `~/.claude/projects/<encoded(cwd)>/`.

    Discovery must pick the freshest JSONL by mtime, not the fd-walk
    uuid alone, and emit a canary log line so future contract shifts
    are diagnosable in seconds."""
    cwd = tmp_path / "proj"
    boot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    fresh = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    fake.cwds[555] = cwd
    # Only the boot-time uuid surfaces in open fds.
    fake.task_uuids[555] = [boot]
    _write_jsonl(projects_root, boot, cwd, mtime=100.0)
    # The post-/clear sid has no lock fd at all; it's only on disk.
    _write_jsonl(projects_root, fresh, cwd, mtime=900.0)

    import logging

    with caplog.at_level(logging.INFO, logger="paige.application.proc_scan"):
        result = proc_scan.discover_run(555)

    assert result is not None
    run_id, _, jsonl = result
    assert run_id == fresh
    assert jsonl.name == f"{fresh}.jsonl"
    # Canary log: greppable hint that the fd walk is no longer
    # authoritative on its own. The `[stale-lock-fd]` tag distinguishes
    # this shape (boot-time lock fd held, just stale) from the
    # `[no-lock-fd]` shape (zero per-uuid fds, comm-match fallback).
    assert any(
        f"active sid={fresh} via project-dir mtime [stale-lock-fd]" in r.message
        for r in caplog.records
    )


def test_canary_log_is_throttled_per_pid_sid(
    fake: _FakePrimitives,
    projects_root: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The canary fires once per (pid, sid) change, not on every tick.
    Discovery runs every ~10 s in production; without throttling the
    log floods with the same line under the normal post-/clear shape."""
    cwd = tmp_path / "proj"
    boot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    fresh = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    fake.cwds[555] = cwd
    fake.task_uuids[555] = [boot]
    _write_jsonl(projects_root, boot, cwd, mtime=100.0)
    _write_jsonl(projects_root, fresh, cwd, mtime=900.0)

    import logging

    with caplog.at_level(logging.INFO, logger="paige.application.proc_scan"):
        for _ in range(5):
            proc_scan.discover_run(555)

    canary_lines = [r for r in caplog.records if "via project-dir mtime" in r.message]
    assert len(canary_lines) == 1, f"expected 1 canary line, got {len(canary_lines)}"


def test_discover_run_none_for_unknown_pid(fake: _FakePrimitives) -> None:
    """No primitives data at all → None."""
    assert proc_scan.discover_run(99999) is None


# ── /server stat re-exports route through _primitives ───────────


def test_server_stats_route_through_primitives(fake: _FakePrimitives) -> None:
    fake.cwds[42] = Path("/x")
    fake.rss[42] = 1234
    fake.comm[42] = "claude"

    assert proc_scan.find_cwd_for_pid(42) == Path("/x")
    assert proc_scan.read_rss_bytes_for_pid(42) == 1234
    assert proc_scan.read_comm_for_pid(42) == "claude"
    assert proc_scan.read_cgroup_memory() == (None, None)
