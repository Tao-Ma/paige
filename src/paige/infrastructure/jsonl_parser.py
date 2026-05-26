"""JsonlParser — pure JSONL line → `TranscriptEvent` transformation.

Stateless and side-effect-free. One line → one `TranscriptEvent`
or `None` (malformed JSON, blank lines, sidecar metadata records
the parser doesn't recognize).

Top-level shape we consume (Claude Code 2.x):

    {"type": "user" | "assistant",
     "uuid": "...",
     "timestamp": "2026-04-25T...Z",
     "message": {"role": "...", "content": "..." | [...]}}

Inside `message.content`:
- assistant turns are always a list of typed blocks
  ({"type": "text"|"thinking"|"tool_use", ...})
- user turns are usually a string, occasionally a list of
  `tool_result` blocks when the user is replying to a tool_use.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

from ..domain.transcript import Block, BlockKind, Role, StopReason, TranscriptEvent

_BLOCK_KIND_MAP: dict[str, BlockKind] = {
    "text": BlockKind.TEXT,
    "thinking": BlockKind.THINKING,
    "tool_use": BlockKind.TOOL_USE,
    "tool_result": BlockKind.TOOL_RESULT,
}


class JsonlParser:
    """Pure transcript-line parser; no I/O, no state."""

    @staticmethod
    def parse_line(line: str) -> TranscriptEvent | None:
        s = line.strip()
        if not s:
            return None
        try:
            data: Any = json.loads(s)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return _parse_record(cast("dict[str, Any]", data))

    @staticmethod
    def parse(text: str) -> list[TranscriptEvent]:
        out: list[TranscriptEvent] = []
        for raw in text.split("\n"):
            ev = JsonlParser.parse_line(raw)
            if ev is not None:
                out.append(ev)
        return out


def _parse_record(data: dict[str, Any]) -> TranscriptEvent | None:
    type_field = data.get("type")
    if type_field == "user":
        role = Role.USER
    elif type_field == "assistant":
        role = Role.ASSISTANT
    else:
        return None

    raw_msg = data.get("message")
    if not isinstance(raw_msg, dict):
        return None
    msg = cast("dict[str, Any]", raw_msg)

    content: Any = msg.get("content")
    blocks: tuple[Block, ...]
    if isinstance(content, str):
        blocks = (Block(kind=BlockKind.TEXT, text=content),)
    elif isinstance(content, list):
        items = cast("list[Any]", content)
        parsed: list[Block] = []
        for item in items:
            if isinstance(item, dict):
                block = _parse_block(cast("dict[str, Any]", item))
                if block is not None:
                    parsed.append(block)
        blocks = tuple(parsed)
    else:
        return None

    return TranscriptEvent(
        role=role,
        blocks=blocks,
        timestamp_ms=_parse_timestamp(data.get("timestamp")),
        stop_reason=_parse_stop_reason(msg.get("stop_reason")) if role is Role.ASSISTANT else None,
    )


def _parse_stop_reason(raw: Any) -> StopReason | None:
    """Map the JSONL's `message.stop_reason` field to our enum.
    Returns None when absent, not a string, or carries a value we
    don't model (forward-compatible — new API stop reasons land as
    None rather than crashing the parser)."""
    if not isinstance(raw, str):
        return None
    try:
        return StopReason(raw)
    except ValueError:
        return None


def _parse_block(block: dict[str, Any]) -> Block | None:
    kind = _BLOCK_KIND_MAP.get(str(block.get("type")))
    if kind is None:
        return None

    if kind is BlockKind.TEXT:
        return Block(kind=kind, text=str(block.get("text", "")))
    if kind is BlockKind.THINKING:
        return Block(kind=kind, text=str(block.get("thinking", "")))
    if kind is BlockKind.TOOL_USE:
        try:
            input_repr = json.dumps(block.get("input", {}))
        except (TypeError, ValueError):
            input_repr = ""
        tool_id = block.get("id")
        name = block.get("name")
        return Block(
            kind=kind,
            text=input_repr,
            tool_id=str(tool_id) if tool_id is not None else None,
            tool_name=str(name) if name is not None else None,
        )
    # BlockKind.TOOL_RESULT
    tool_id = block.get("tool_use_id")
    raw_content: Any = block.get("content", "")
    text = _tool_result_text(raw_content)
    return Block(
        kind=kind,
        text=text,
        tool_id=str(tool_id) if tool_id is not None else None,
    )


def _tool_result_text(raw_content: Any) -> str:
    """Extract the readable text from a `tool_result.content` field.

    Claude Code's transcript can store tool_result content in three
    shapes:

    - **str** — already the rendered text. Return it verbatim.
    - **list of content blocks** — typically
      `[{"type": "text", "text": "..."}]`. Extract each `text`
      field and join with newlines. The previous codepath
      `json.dumps`-ed the whole list, so the live card displayed
      a literal `[{"type":"text","text":"...\\n..."}]` string with
      backslash-escaped newlines and `\\u2014` unicode codepoints
      instead of the actual prose.
    - **anything else** — fall back to `json.dumps` so the body
      isn't blank; better to surface the raw shape than to
      silently swallow it.
    """
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        items = cast("list[Any]", raw_content)
        parts: list[str] = []
        for item in items:
            if isinstance(item, dict):
                item_dict = cast("dict[str, Any]", item)
                if item_dict.get("type") == "text":
                    t = item_dict.get("text", "")
                    if isinstance(t, str):
                        parts.append(t)
            elif isinstance(item, str):
                parts.append(item)
            # Other block kinds (image, etc.) → fall through; we
            # don't try to render them in the text channel.
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(raw_content)
    except (TypeError, ValueError):
        return ""


def _parse_timestamp(ts: Any) -> int:
    if not isinstance(ts, str):
        return 0
    try:
        # `Z` suffix isn't accepted by `fromisoformat` until 3.11+
        # in some forms; replace defensively.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(dt.timestamp() * 1000)
