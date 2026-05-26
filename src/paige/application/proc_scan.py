"""proc_scan — run-discovery + /server stats over a `ProcPrimitives`.

Holds the module-level `PsutilPrimitives` instance paige's runtime
queries (discovery, /server stat reads, dir-size). Discovery itself
is platform-free — it lives here, parameterised on the primitives.

Lives in `application/` because it *consumes* the `ProcPrimitives`
port — the layered-architecture contract forbids `infrastructure →
ports`, and proc_scan is genuinely application-layer coordination
logic that just happens to sit one call away from the OS. The
concrete primitives impl (`PsutilPrimitives`) and helpers it leans
on (`transcript_path`) stay in `infrastructure/`.

Tests stub the primitives by rebinding `_primitives` for the duration
of the test; the module-level re-export shape stays so callers
(`run_discovery.py`, `server.py`) don't have to know about the swap.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..infrastructure.psutil_primitives import PsutilPrimitives
from ..infrastructure.transcript_path import encode_cwd, transcript_path
from ..ports.proc_primitives import ProcPrimitives

logger = logging.getLogger(__name__)

# Module-level instance built once at import. Tests rebind this
# attribute to inject a fake. Production callers use the module-level
# functions below; nobody constructs primitives directly.
_primitives: ProcPrimitives = PsutilPrimitives()

_UUID_LEN = 36

# Per-pid last-logged active sid for the "fd walk disagrees with
# project-dir mtime" canary. Throttles the INFO line to once per
# (pid, sid) change — without this it fires on every 10 s discovery
# tick under the normal post-/clear shape. Tests should call
# `_reset_canary_state()` between cases that share a pid.
_canary_last_sid: dict[int, str] = {}


def _reset_canary_state() -> None:  # pyright: ignore[reportUnusedFunction]
    """Test-only — pyright can't see the caller (tests/ is outside the
    src-only type-check scope). Used by `tests/unit/application/
    test_proc_scan.py::fake` to drop the per-pid throttle state
    between cases that share a pid."""
    _canary_last_sid.clear()


def discover_run(
    pid: int, *, exclude_uuids: frozenset[str] = frozenset()
) -> tuple[str, Path, Path] | None:
    """Find the run pointer for `pid` (or any of its immediate children).

    Two signals are combined:

    1. **Open task-dir fds** (`~/.claude/tasks/<uuid>/.lock`) — the
       primary proof-of-life that this pid is a Claude Code process,
       AND a candidate uuid set.
    2. **Project-dir JSONLs** (`~/.claude/projects/<encoded(cwd)>/*.jsonl`)
       — the source of truth for the *active* session id. Pick the
       JSONL with the most recent mtime; that's whichever session
       claude is currently writing to.

    Why both? Through 2.1.126 each `/clear` opened a new
    `tasks/<sid>/.lock` fd, so signal 1 alone identified the live sid.
    A later release stopped rotating that fd — claude continues to
    hold the boot-time lock open and only the JSONL filename moves —
    so signal 1 returns a *stale* uuid forever after the first /clear.
    Two production regressions ("only Status & Approval cards reach
    Lark") traced to trusting signal 1 alone; this is the third
    iteration of the same shape and the reason the canary log below
    fires when the two signals disagree.

    Some claude installs hold *zero* `tasks/<uuid>/.lock` regular-file
    fds — only directory fds against `~/.claude/tasks` and
    `~/.claude/projects`, which psutil's `open_files()` filters out.
    For those the open-fd uuid set is empty, so we fall back to
    `comm == "claude"` as proof-of-life and rely entirely on signal 2
    (the project-dir mtime scan). False-positive risk is a process
    the user happens to have named `claude` whose cwd is a real
    Claude project dir — vanishingly rare, and a missing JSONL still
    short-circuits with None.

    A tmux pane's foreground is usually the shell, not claude — so we
    recurse one level of children if `pid` itself has nothing. Linux
    serves the fds via `/proc/<pid>/fd`; macOS via `lsof`. Both are
    available by default on supported platforms; environments that
    deliberately restrict fd visibility (locked-down `/proc`, missing
    `lsof`) will see empty `/sessions Active` until the user re-binds
    via the chooser. Better to surface "no run" than to fall back to
    a stale answer.

    `exclude_uuids` is a set of run_ids the caller has already
    attributed to other panes this tick. It rules them out of the
    project-dir mtime scan, so two panes sharing a cwd don't both
    grab the same most-recently-written JSONL. Open-fd uuids (the
    boot-time tasks/<uuid>/.lock signal) are NOT excluded — they're
    a per-pid signal that the caller hasn't seen for any other
    pane yet (would be a real conflict if it happens).

    Returns `(run_id, cwd, jsonl_path)` or None.
    """
    found = _discover_one(pid, exclude_uuids)
    if found is not None:
        return found
    for child in _primitives.get_children(pid):
        found = _discover_one(child, exclude_uuids)
        if found is not None:
            return found
    return None


def _discover_one(pid: int, exclude_uuids: frozenset[str]) -> tuple[str, Path, Path] | None:
    cwd = _primitives.get_cwd(pid)
    if cwd is None:
        return None
    open_uuids = tuple(_primitives.get_open_task_uuids(pid))
    if not open_uuids and _primitives.get_comm(pid) != "claude":
        # No tasks-fd uuids AND not named `claude` → not a live
        # claude process. We accept either signal as proof-of-life
        # because some claude installs hold only directory fds
        # against `~/.claude/tasks` (filtered out by psutil's
        # open_files()), leaving the comm match as the only
        # identifying signal. Discovery still returns None below if
        # the project-dir scan turns up no JSONLs.
        return None
    # Build candidate set = open-fd uuids ∪ project-dir uuids. The
    # project-dir scan is what catches a /clear that rotated only the
    # JSONL (no new lock fd) — see this module's docstring.
    candidates: dict[str, Path] = {u: transcript_path(u, cwd) for u in open_uuids}
    proj_dir = Path.home() / ".claude" / "projects" / encode_cwd(cwd)
    try:
        for entry in proj_dir.iterdir():
            if entry.suffix != ".jsonl" or len(entry.stem) != _UUID_LEN or "-" not in entry.stem:
                continue
            if entry.stem in exclude_uuids:
                # Caller has already attributed this uuid to another
                # pane this tick — skip it so two panes sharing a
                # cwd don't both grab the same most-recently-written
                # JSONL.
                continue
            candidates.setdefault(entry.stem, entry)
    except OSError:
        # Project dir doesn't exist yet (brand-new claude that hasn't
        # written its first JSONL line). Open-fd uuids cover this.
        pass
    best: tuple[float, str, Path] | None = None
    for run_id, jsonl in candidates.items():
        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            # JSONL not on disk yet (very fresh session); score 0.0
            # so it loses to any candidate with a real file but still
            # beats nothing.
            mtime = 0.0
        if best is None or mtime > best[0]:
            best = (mtime, run_id, jsonl)
    if best is None:
        return None
    _, run_id, jsonl = best
    if run_id not in open_uuids and _canary_last_sid.get(pid) != run_id:
        # Canary: the active sid was found via project-dir mtime
        # rather than the open-fd set. Two shapes share this line so
        # future-you can grep them apart in seconds:
        #
        #   stale-lock-fd  — claude held a boot-time `tasks/<sid>/.lock`
        #                    fd that no longer matches the active sid
        #                    (post-/clear under Claude Code 2.1.126+).
        #   no-lock-fd     — claude held zero per-uuid lock fds; only
        #                    directory fds against ~/.claude/tasks
        #                    (filtered by psutil's open_files()).
        #                    Comm-match was the only proof-of-life.
        #
        # Throttled to once per (pid, sid) change to avoid flooding
        # the log on every 10 s discovery tick under either shape.
        shape = "stale-lock-fd" if open_uuids else "no-lock-fd"
        logger.info(
            "discover_run: pid=%d active sid=%s via project-dir mtime [%s]; open-fd uuids=%s",
            pid,
            run_id,
            shape,
            list(open_uuids),
        )
        _canary_last_sid[pid] = run_id
    elif run_id in open_uuids:
        _canary_last_sid.pop(pid, None)
    return (run_id, cwd, jsonl)


def find_cwd_for_pid(pid: int) -> Path | None:
    return _primitives.get_cwd(pid)


def read_rss_bytes_for_pid(pid: int) -> int | None:
    return _primitives.get_rss_bytes(pid)


def read_comm_for_pid(pid: int) -> str | None:
    return _primitives.get_comm(pid)


def read_cgroup_memory() -> tuple[int | None, int | None]:
    return _primitives.get_cgroup_memory()


def dir_size_bytes(path: Path) -> int:
    """Sum file sizes under `path` recursively. Returns 0 on error.

    OS-agnostic — `os.walk` works on every supported platform. Lives
    here (rather than on `ProcPrimitives`) because it's not pid-keyed
    and doesn't differ between backends.
    """
    import os

    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        return 0
    return total


__all__ = [
    "dir_size_bytes",
    "discover_run",
    "find_cwd_for_pid",
    "read_cgroup_memory",
    "read_comm_for_pid",
    "read_rss_bytes_for_pid",
]
