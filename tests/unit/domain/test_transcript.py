"""Transcript / TranscriptEvent / Block / Role / BlockKind."""

from pathlib import Path

from paige.domain.transcript import (
    Block,
    BlockKind,
    Role,
    Transcript,
    TranscriptEvent,
)


def test_block_kinds() -> None:
    """Four kinds, matching Anthropic's content block `type` field."""
    assert BlockKind.TEXT.value == "text"
    assert BlockKind.THINKING.value == "thinking"
    assert BlockKind.TOOL_USE.value == "tool_use"
    assert BlockKind.TOOL_RESULT.value == "tool_result"


def test_role_values() -> None:
    assert Role.USER.value == "user"
    assert Role.ASSISTANT.value == "assistant"


def test_block_text_only() -> None:
    b = Block(kind=BlockKind.TEXT, text="hello")
    assert b.tool_id is None
    assert b.tool_name is None


def test_block_tool_use_pairing_id() -> None:
    """tool_use blocks carry a `tool_id` so the matching
    tool_result can be paired up later."""
    b = Block(
        kind=BlockKind.TOOL_USE,
        text="**Bash**(echo hi)",
        tool_id="toolu_xyz",
        tool_name="Bash",
    )
    assert b.tool_id == "toolu_xyz"
    assert b.tool_name == "Bash"


def test_transcript_event_assistant_with_blocks() -> None:
    blocks = (
        Block(kind=BlockKind.THINKING, text="reasoning..."),
        Block(kind=BlockKind.TEXT, text="here is the answer"),
    )
    ev = TranscriptEvent(role=Role.ASSISTANT, blocks=blocks)
    assert ev.role == Role.ASSISTANT
    assert len(ev.blocks) == 2
    assert ev.blocks[0].kind == BlockKind.THINKING
    assert ev.timestamp_ms == 0


def test_transcript_event_user_text_message() -> None:
    ev = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TEXT, text="what's up?"),),
        timestamp_ms=1700000000000,
    )
    assert ev.timestamp_ms == 1700000000000


def test_transcript_holds_run_id_and_path() -> None:
    p = Path("/tmp/sessions/xyz.jsonl")
    t = Transcript(run_id="abc-uuid", file_path=p)
    assert t.run_id == "abc-uuid"
    assert t.file_path == p
