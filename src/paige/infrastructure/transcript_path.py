"""Resolve a Claude Code transcript JSONL path from `(run_id, cwd)`.

Claude Code stores transcripts under a fixed layout:

    ~/.claude/projects/<encoded_cwd>/<run_id>.jsonl

`encoded_cwd` is the absolute cwd with every non-alphanumeric
character (except `-`) replaced by `-`. So `/home/u/proj` becomes
`-home-u-proj`. RunDiscovery learns the live JSONL via /proc/<pid>/fd,
but that's only available while the process is running. Anything
that needs to read a transcript by its run_id (history, dormant
sessions) goes through this helper.

Pure path math — no I/O. Returns a `Path` even if the file doesn't
exist; callers `Path.is_file()` themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

_NON_DASH_ALNUM = re.compile(r"[^a-zA-Z0-9-]")


def encode_cwd(cwd: Path) -> str:
    """Encode `cwd` to Claude Code's project-directory naming.

    `/home/user_name/Code/project` → `-home-user-name-Code-project`.
    """
    return _NON_DASH_ALNUM.sub("-", str(cwd))


def transcript_path(run_id: str, cwd: Path, *, projects_root: Path | None = None) -> Path:
    """Build the JSONL path for a (run_id, cwd) pair.

    `projects_root` defaults to `~/.claude/projects`. Tests can pass
    a `tmp_path` to keep the resolver pure.
    """
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"
    return projects_root / encode_cwd(cwd) / f"{run_id}.jsonl"


__all__ = ["encode_cwd", "transcript_path"]
