"""Run — a Claude Code conversation, identified by its session_id (UUID)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .transcript import Transcript


@dataclass(frozen=True)
class Run:
    """A Claude Code run.

    `run_id` is the session UUID Claude assigns at start. `cwd`
    is where Claude was invoked. `transcript` points at the
    on-disk JSONL.

    Summary fields (`summary`, `message_count`, `total_tokens`)
    are derived data — kept here for easy display in pickers
    without re-reading the JSONL on every render.
    """

    run_id: str
    cwd: Path
    transcript: Transcript
    summary: str = ""
    message_count: int = 0
    total_tokens: int = 0
    last_modified_ms: int = 0
