"""JsonlParser — pure JSONL → TranscriptEvent transformation."""

from __future__ import annotations

import json

from paige.domain.transcript import BlockKind, Role, StopReason
from paige.infrastructure.jsonl_parser import JsonlParser


def test_blank_line_returns_none() -> None:
    assert JsonlParser.parse_line("") is None
    assert JsonlParser.parse_line("   \n") is None


def test_malformed_json_returns_none() -> None:
    assert JsonlParser.parse_line("{not json") is None
    assert JsonlParser.parse_line("[1, 2]") is None  # not a dict


def test_unknown_top_level_type_returns_none() -> None:
    line = json.dumps({"type": "summary", "message": {"role": "x", "content": "y"}})
    assert JsonlParser.parse_line(line) is None


def test_user_text_message() -> None:
    line = json.dumps(
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-04-25T10:00:00Z",
            "message": {"role": "user", "content": "hello world"},
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.role is Role.USER
    assert len(ev.blocks) == 1
    assert ev.blocks[0].kind is BlockKind.TEXT
    assert ev.blocks[0].text == "hello world"
    assert ev.timestamp_ms > 0


def test_assistant_text_block() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi there!"}],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.role is Role.ASSISTANT
    assert ev.blocks[0].kind is BlockKind.TEXT
    assert ev.blocks[0].text == "Hi there!"


def test_assistant_thinking_block() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "let me think…"}],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.blocks[0].kind is BlockKind.THINKING
    assert ev.blocks[0].text == "let me think…"


def test_assistant_tool_use_block() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "Bash",
                        "input": {"command": "ls -la"},
                    }
                ],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    block = ev.blocks[0]
    assert block.kind is BlockKind.TOOL_USE
    assert block.tool_id == "toolu_abc"
    assert block.tool_name == "Bash"
    assert "ls -la" in block.text  # JSON-encoded input


def test_user_tool_result_block() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "file1.txt\nfile2.txt",
                    }
                ],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    block = ev.blocks[0]
    assert block.kind is BlockKind.TOOL_RESULT
    assert block.tool_id == "toolu_abc"
    assert block.text == "file1.txt\nfile2.txt"


def test_user_tool_result_block_with_content_list() -> None:
    """Claude often returns `tool_result.content` as a list of
    content blocks — `[{"type":"text","text":"..."}]`. The parser
    must unwrap each `text` field rather than json.dumps the list,
    otherwise the live card displays the JSON-encoded shape with
    literal `\\n` and `\\u2014` escape sequences."""
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": [
                            {"type": "text", "text": "Excellent.\n\n## Heading\n— bullet"},
                        ],
                    }
                ],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    block = ev.blocks[0]
    assert block.kind is BlockKind.TOOL_RESULT
    # Newlines and em-dashes survive intact; no `[{...}]` JSON wrapper.
    assert block.text == "Excellent.\n\n## Heading\n— bullet"


def test_user_tool_result_block_with_multiple_text_blocks() -> None:
    """Multiple text blocks in the list are joined with newlines."""
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": [
                            {"type": "text", "text": "part one"},
                            {"type": "text", "text": "part two"},
                        ],
                    }
                ],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.blocks[0].text == "part one\npart two"


def test_mixed_blocks_in_one_turn() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "first"},
                    {"type": "text", "text": "Sure!"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    kinds = [b.kind for b in ev.blocks]
    assert kinds == [BlockKind.THINKING, BlockKind.TEXT, BlockKind.TOOL_USE]


def test_unknown_block_type_is_skipped_not_record() -> None:
    """A single unknown block doesn't reject the whole record."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "image", "source": {}},  # unknown to us
                    {"type": "text", "text": "afterwards"},
                ],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert len(ev.blocks) == 1
    assert ev.blocks[0].text == "afterwards"


def test_parse_multi_line_skips_blanks_and_garbage() -> None:
    text = "\n".join(
        [
            json.dumps({"type": "user", "message": {"role": "user", "content": "a"}}),
            "",
            "garbage{",
            json.dumps({"type": "user", "message": {"role": "user", "content": "b"}}),
        ]
    )
    events = JsonlParser.parse(text)
    assert len(events) == 2
    assert events[0].blocks[0].text == "a"
    assert events[1].blocks[0].text == "b"


def test_missing_message_returns_none() -> None:
    line = json.dumps({"type": "user", "uuid": "u1"})
    assert JsonlParser.parse_line(line) is None


def test_missing_timestamp_is_zero() -> None:
    line = json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.timestamp_ms == 0


def test_invalid_timestamp_is_zero() -> None:
    line = json.dumps(
        {
            "type": "user",
            "timestamp": "not-a-date",
            "message": {"role": "user", "content": "hi"},
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.timestamp_ms == 0


# ── stop_reason ──────────────────────────────────────────────────


def test_assistant_end_turn_carries_stop_reason() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.stop_reason is StopReason.END_TURN


def test_assistant_tool_use_carries_stop_reason() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "ok"}],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.stop_reason is StopReason.TOOL_USE


def test_assistant_without_stop_reason_is_none() -> None:
    """Older / malformed assistant records lacking stop_reason
    surface as None — the readiness service treats this as
    NOT_READY (its default) and waits for the next event."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.stop_reason is None


def test_unknown_stop_reason_string_is_none() -> None:
    """Forward-compat: a future API may add a new stop_reason value
    we don't model. Land it as None so the parser doesn't crash."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "something_new",
                "content": [{"type": "text", "text": "hi"}],
            },
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.stop_reason is None


def test_user_event_never_carries_stop_reason() -> None:
    """`stop_reason` is an assistant-only field. Even if a user
    record had it for some reason, we don't surface it."""
    line = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "stop_reason": "end_turn", "content": "hi"},
        }
    )
    ev = JsonlParser.parse_line(line)
    assert ev is not None
    assert ev.role is Role.USER
    assert ev.stop_reason is None
