"""Walk Claude Code's projects directory for dormant sessions.

A "session" here is one JSONL transcript file. paige's `RunRegistry`
holds pointers to *live* sessions — the ones with a tmux pane and
JSONL fd open. Everything else under `~/.claude/projects/` is dormant:
the user finished or backgrounded the session, the pane is gone, but
the transcript is still on disk and `claude --resume <sid>` can
revive it.

Pure-ish: filesystem reads, no asyncio. Caller wraps in
`asyncio.to_thread` if they care about loop-blocking; for a few
dozen JSONLs the walk takes ~ms even synchronously.

Cwd recovery is intentionally lossy: the encoded dir name (`-home-u-proj`)
maps non-alnum chars → `-` one-way, so we can't unambiguously recover
slashes. We swap leading-dashes-to-slash and inner-dashes-to-slash —
good enough for `claude --resume`'s working-dir hint and for human
display in the picker. The encode is the source of truth; never
reverse for any non-display purpose.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20
_SUMMARY_MAX = 80


@dataclass(frozen=True)
class DormantSession:
    """One dormant session row. `cwd` is best-effort decoded."""

    session_id: str
    cwd: Path
    summary: str  # first user-message text, truncated
    mtime: float  # seconds since epoch — for sort + display
    message_count: int
    file_path: Path


def list_dormant_sessions(
    projects_root: Path,
    *,
    exclude_run_ids: frozenset[str] = frozenset(),
    limit: int | None = _DEFAULT_LIMIT,
) -> list[DormantSession]:
    """Find dormant sessions under `projects_root`. Sorted newest-first.

    `exclude_run_ids` skips sessions whose run_id is currently live
    (so /sessions doesn't double-list). Pass the registry's tracked
    run_ids to keep active and dormant sets disjoint.

    `limit=None` returns every match — the Resume sub-pane uses this
    so its pagination can reach older sessions. The default cap exists
    only for callers that want a snapshot for a tight listing.
    """
    if not projects_root.is_dir():
        return []

    candidates: list[tuple[Path, float]] = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        try:
            entries = list(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        for f in entries:
            if f.stem == "sessions-index":
                continue  # claude bookkeeping file
            if f.stem in exclude_run_ids:
                continue
            try:
                candidates.append((f, f.stat().st_mtime))
            except OSError:
                continue

    candidates.sort(key=lambda t: t[1], reverse=True)

    out: list[DormantSession] = []
    for file_path, mtime in candidates:
        if limit is not None and len(out) >= limit:
            break
        info = _read_summary(file_path)
        if info is None:
            continue
        summary, count = info
        if count == 0:
            continue
        cwd = _decode_cwd(file_path.parent.name)
        out.append(
            DormantSession(
                session_id=file_path.stem,
                cwd=cwd,
                summary=summary,
                mtime=mtime,
                message_count=count,
                file_path=file_path,
            )
        )
    return out


def delete_dormant_session(file_path: Path, *, projects_root: Path) -> bool:
    """Unlink a dormant JSONL transcript. Returns True on success.

    Safety: refuses to unlink anything outside `projects_root` or
    that doesn't end in `.jsonl`. The caller (a click handler) gets
    `file_path` from the action value, which we set ourselves — but
    a tampered click could carry an arbitrary path. The containment
    check is defense-in-depth, not the primary trust boundary.
    """
    try:
        resolved = file_path.resolve()
        root = projects_root.resolve()
    except OSError as e:
        logger.warning("delete_dormant_session resolve failed: %s", e)
        return False
    if resolved.suffix != ".jsonl":
        logger.warning("delete_dormant_session refused non-jsonl: %s", resolved)
        return False
    try:
        resolved.relative_to(root)
    except ValueError:
        logger.warning("delete_dormant_session refused out-of-root: %s", resolved)
        return False
    try:
        resolved.unlink()
    except FileNotFoundError:
        return True  # already gone — same end state
    except OSError as e:
        logger.warning("delete_dormant_session unlink %s failed: %s", resolved, e)
        return False
    return True


def archive_root_for(projects_root: Path) -> Path:
    """Where archived JSONLs live, given a projects_root.
    `~/.claude/projects` → `~/.claude/archive`."""
    return projects_root.parent / "archive"


def archive_dormant_session(file_path: Path, *, projects_root: Path) -> bool:
    """Move a dormant JSONL into the sibling archive root, preserving
    its project subdir. Returns True on success.

    `~/.claude/projects/<encoded>/<sid>.jsonl`
        → `~/.claude/archive/<encoded>/<sid>.jsonl`

    Safety: refuses to move anything outside `projects_root` or that
    doesn't end in `.jsonl`. Refuses to overwrite an existing file at
    the destination — uuids should be unique, so a collision indicates
    something unexpected (e.g. a manual restore left a stale archive
    copy behind); loud-fail beats silent clobber. Missing source is
    treated as already-archived (same end state, mirrors how
    `delete_dormant_session` handles FileNotFoundError).
    """
    try:
        resolved = file_path.resolve()
        root = projects_root.resolve()
    except OSError as e:
        logger.warning("archive_dormant_session resolve failed: %s", e)
        return False
    if resolved.suffix != ".jsonl":
        logger.warning("archive_dormant_session refused non-jsonl: %s", resolved)
        return False
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        logger.warning("archive_dormant_session refused out-of-root: %s", resolved)
        return False
    dest = archive_root_for(root) / rel
    if dest.exists():
        logger.warning("archive_dormant_session refused collision at %s", dest)
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resolved.rename(dest)
    except FileNotFoundError:
        return True  # source already gone
    except OSError as e:
        logger.warning("archive_dormant_session move %s → %s failed: %s", resolved, dest, e)
        return False
    return True


def restore_archived_session(file_path: Path, *, projects_root: Path) -> bool:
    """Move an archived JSONL back to its place under `projects_root`,
    preserving the project subdir. Returns True on success.

    `~/.claude/archive/<encoded>/<sid>.jsonl`
        → `~/.claude/projects/<encoded>/<sid>.jsonl`

    Safety mirrors `archive_dormant_session`: must live under the
    archive root and end in `.jsonl`; refuses to overwrite an existing
    destination. Missing source treated as success (already restored).
    """
    archive_root = archive_root_for(projects_root)
    try:
        resolved = file_path.resolve()
        root = archive_root.resolve()
        proj = projects_root.resolve()
    except OSError as e:
        logger.warning("restore_archived_session resolve failed: %s", e)
        return False
    if resolved.suffix != ".jsonl":
        logger.warning("restore_archived_session refused non-jsonl: %s", resolved)
        return False
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        logger.warning("restore_archived_session refused out-of-root: %s", resolved)
        return False
    dest = proj / rel
    if dest.exists():
        logger.warning("restore_archived_session refused collision at %s", dest)
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resolved.rename(dest)
    except FileNotFoundError:
        return True  # source already gone
    except OSError as e:
        logger.warning("restore_archived_session move %s → %s failed: %s", resolved, dest, e)
        return False
    return True


def list_archived_sessions(
    archive_root: Path,
    *,
    limit: int | None = _DEFAULT_LIMIT,
) -> list[DormantSession]:
    """List archived sessions under `archive_root`. Same shape as
    `list_dormant_sessions` (returns `DormantSession` — archived is
    just dormant-stashed-elsewhere from the UI's perspective). No
    `exclude_run_ids` parameter: archived sessions are never live.
    """
    return list_dormant_sessions(archive_root, limit=limit)


def count_archived_sessions(archive_root: Path) -> int:
    """Cheap count of archived JSONLs under `archive_root`. Mirrors
    `count_dormant_sessions` but on the archive tree."""
    return count_dormant_sessions(archive_root)


def _read_summary(path: Path) -> tuple[str, int] | None:
    """First user-message text + total line-count. None on read error."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("read summary %s failed: %s", path, e)
        return None

    summary = ""
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        count += 1
        if not summary:
            summary = _extract_first_user_text(line)

    if not summary:
        summary = "(no summary)"
    elif len(summary) > _SUMMARY_MAX:
        summary = summary[:_SUMMARY_MAX] + "…"
    return summary, count


def _extract_first_user_text(line: str) -> str:
    try:
        raw: Any = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(raw, dict):
        return ""
    record = cast("dict[str, Any]", raw)
    if record.get("type") != "user":
        return ""
    raw_msg = record.get("message")
    if not isinstance(raw_msg, dict):
        return ""
    msg = cast("dict[str, Any]", raw_msg)
    content: Any = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    # Tool-result-only user turns aren't useful summary fodder; skip.
    return ""


def _decode_cwd(encoded: str) -> Path:
    """Reverse Claude Code's cwd encoding as best as possible.

    Encoding does `[^a-zA-Z0-9-] → -`, so the reverse can only guess.
    The two leading dashes map to `/`, then we map inner dashes to
    slashes. This breaks for paths with intentional dashes (e.g.
    `my-project`) but the caller uses the result for display + a
    cwd hint to `claude --resume`, both of which tolerate the lossy
    decode.
    """
    s = encoded.lstrip("-")
    return Path("/" + s.replace("-", "/"))


def count_dormant_sessions(
    projects_root: Path,
    *,
    exclude_run_ids: frozenset[str] = frozenset(),
) -> int:
    """Count dormant `.jsonl` transcripts under `projects_root`.

    Cheap counterpart to `list_dormant_sessions` — only stats files
    (no read, no summary parse) so the /sessions chooser body can
    surface the *total* dormant count, including any beyond the
    listing's read-with-summary limit. Returning a number > the
    listing length is the signal to the Resume sub-pane that it
    should render a "Showing N of M most recent" hint.
    """
    if not projects_root.is_dir():
        return 0
    total = 0
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        try:
            entries = list(project_dir.glob("*.jsonl"))
        except OSError:
            continue
        for f in entries:
            if f.stem == "sessions-index":
                continue
            if f.stem in exclude_run_ids:
                continue
            total += 1
    return total


__all__ = [
    "DormantSession",
    "archive_dormant_session",
    "archive_root_for",
    "count_archived_sessions",
    "count_dormant_sessions",
    "delete_dormant_session",
    "list_archived_sessions",
    "list_dormant_sessions",
    "restore_archived_session",
]
