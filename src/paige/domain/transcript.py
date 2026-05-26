"""Transcript + TranscriptEvent + Block — the JSONL stream Claude writes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Role(StrEnum):
    """Who authored a transcript event."""

    USER = "user"
    ASSISTANT = "assistant"


class BlockKind(StrEnum):
    """The kind of content block within an assistant turn.

    Mirrors Anthropic's content block types — Claude Code writes
    these as the `type` field on each block in its JSONL.
    """

    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class Block:
    """One content block within a transcript event.

    `tool_id` pairs `tool_use` ↔ `tool_result` blocks across two
    transcript events (the assistant emits a `tool_use`, then a
    later user-role event contains the `tool_result` referencing
    the same id).
    """

    kind: BlockKind
    text: str
    tool_id: str | None = None
    tool_name: str | None = None


class StopReason(StrEnum):
    """The `stop_reason` field on an assistant transcript event.

    Mirrors Anthropic's API stop_reason values — Claude Code carries
    them through to the JSONL on every assistant record. `END_TURN`
    is the *only* value that means "claude is waiting for the next
    user input"; everything else means the agent loop is still in
    motion (will continue once the right input lands).
    """

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


@dataclass(frozen=True)
class TranscriptEvent:
    """One JSONL entry — a user message or one assistant turn.

    A turn can contain multiple `Block`s (text + tool_use + thinking,
    in order). User messages are usually one TEXT block but can
    carry tool_result blocks back to the assistant.

    `stop_reason` is set only on assistant events and only when the
    underlying record carried a recognized `stop_reason`. Unknown
    or absent values surface as None.
    """

    role: Role
    blocks: tuple[Block, ...]
    timestamp_ms: int = 0
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class Transcript:
    """A Claude Code transcript file (JSONL on disk)."""

    run_id: str
    file_path: Path
