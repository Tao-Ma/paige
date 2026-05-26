"""AskUserService — buttoned card UX for the `AskUserQuestion` tool.

`AskUserQuestion` is a Claude Code TUI-interactive tool: the model
emits a `tool_use` with structured `{questions:[{question, options}]}`
input, Claude Code draws an arrow-key picker, the user picks an
option, and Claude Code synthesizes the `tool_result`. paige sits
between Claude and the user — the generic tool_use renderer
(`🔧 *AskUserQuestion*({json…})`) is unreadable, and even if read
the picker can't be satisfied by typed text via send_keys.

This service handles both halves:

* **Render**: `parse_questions` parses the JSON-encoded input into
  a structured form; `build_card` renders the first question as a
  Card with question text + one button per option. Multi-question
  payloads are rare — only the first is buttoned, with a tail note
  for any remaining ones (the TUI picker presents them
  sequentially anyway).
* **Click**: register an `on_action` handler for the option-pick
  buttons. On click, look up the bound pane via `RunRegistry` and
  drive the TUI picker by sending `Down × N` followed by `Enter`,
  one keystroke per send_keys call (avoids any
  whitespace-splitting assumption about tmux's named keys).

Dispatcher integration
----------------------
Detection lives in `Dispatcher._dispatch_block`: when a TOOL_USE
block's `tool_name` matches `TOOL_NAME` here, the dispatcher uses
`build_card` instead of `render_block` and bypasses
`VerbosityService.maybe_truncate` (the question + options are
exactly the actionable content that BRIEF clipping would hide).
The tool_use → tool_result anchor pairing is preserved so the
card edits in place once Claude Code writes the user's answer to
JSONL.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from ..domain.card import Action, ActionEvent, Card
from ..domain.outbound import CardContent, Outbound
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AllowList
from .message_seq import MessageSeqService
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

ACTION_PICK = "askq:pick"
TOOL_NAME = "AskUserQuestion"

# Header color reused for both the original and post-click card so
# the patch doesn't visually re-style the card on tap.
_HEADER_COLOR = "wathet"


@dataclass(frozen=True)
class _Option:
    label: str
    description: str = ""


@dataclass(frozen=True)
class _Question:
    question: str
    header: str
    options: tuple[_Option, ...]
    multi_select: bool


def parse_questions(input_json: str) -> tuple[_Question, ...] | None:
    """Parse the JSON-encoded `input` field of an `AskUserQuestion`
    tool_use block.

    Returns `None` if the shape doesn't match — the dispatcher then
    falls back to the generic tool_use render so we never silently
    drop an event.
    """
    try:
        data: Any = json.loads(input_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_questions = cast("dict[str, Any]", data).get("questions")
    if not isinstance(raw_questions, list):
        return None
    out: list[_Question] = []
    for q in cast("list[Any]", raw_questions):
        if not isinstance(q, dict):
            return None
        q_dict = cast("dict[str, Any]", q)
        opts_raw = q_dict.get("options")
        if not isinstance(opts_raw, list):
            return None
        opts: list[_Option] = []
        for o in cast("list[Any]", opts_raw):
            if not isinstance(o, dict):
                return None
            o_dict = cast("dict[str, Any]", o)
            opts.append(
                _Option(
                    label=str(o_dict.get("label", "")),
                    description=str(o_dict.get("description", "")),
                )
            )
        out.append(
            _Question(
                question=str(q_dict.get("question", "")),
                header=str(q_dict.get("header", "")),
                options=tuple(opts),
                multi_select=bool(q_dict.get("multiSelect", False)),
            )
        )
    return tuple(out)


def _normalize_header(raw: str) -> str:
    """Claude Code's `AskUserQuestion` tool spec calls for tag-style
    headers — `[CONFIRM_PROJECT_DETAILS]`, `RESTORE_PLAN`, etc. That
    raw form is fine as a transcript anchor but reads like demo
    code in a colored card strip. Normalize to sentence case:
    strip wrapping brackets, swap underscores / dashes for spaces,
    and drop ALL CAPS to a single capitalized form. Already-human
    headers ("Next slice") pass through untouched.
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] in "[(<" and s[-1] in "])>":
        s = s[1:-1].strip()
    if "_" in s or "-" in s:
        s = s.replace("_", " ").replace("-", " ")
        s = " ".join(s.split())
    if s and any(c.isalpha() for c in s) and s == s.upper():
        s = s.lower().capitalize()
    return s or "Question"


def build_card(tool_id: str, questions: tuple[_Question, ...]) -> Card:
    """Render the parsed questions as a Card with one button per
    option on the first question.

    Multi-question payloads are extremely rare — Claude Code's
    picker presents them sequentially anyway. The remaining
    questions get a brief tail note so the user knows more are
    coming after they answer this one in the TUI.

    Each button's `value` carries enough state to rebuild the
    post-click card without the buttons (`body`, `header`, `label`),
    since `Action.value` is the only payload routed back to the
    handler — the original Card object isn't.
    """
    if not questions:
        return Card(
            text=f"*{TOOL_NAME}* (no questions)",
            header_title="❓ Question (empty)",
            header_color=_HEADER_COLOR,
        )
    first = questions[0]
    body_parts: list[str] = []
    if first.question:
        body_parts.append(first.question)
    # Render each option's description (if Claude provided one) as
    # its own paragraph so the user sees the picker context without
    # peeking at the TUI. Cards.py emits each \n\n-separated chunk
    # as a distinct markdown element, which sidesteps Lark's
    # per-element truncation when descriptions are verbose. Empty
    # descriptions are skipped — the button label alone suffices.
    for idx, opt in enumerate(first.options):
        label = opt.label or f"Option {idx + 1}"
        if opt.description:
            body_parts.append(f"**{idx + 1}. {label}** — {opt.description}")
        else:
            body_parts.append(f"**{idx + 1}. {label}**")
    if len(questions) > 1:
        body_parts.append(f"_(+{len(questions) - 1} more question(s) follow in TUI)_")
    body_text = "\n\n".join(body_parts) if body_parts else f"*{TOOL_NAME}*"
    # Prefix every AskUserQuestion header with ❓ so it can never
    # collide with iui's humanized overlay names (e.g. Claude
    # emitting `[BASH_APPROVAL]` would normalize into "Bash
    # approval", which would otherwise be indistinguishable from
    # the iui Bash approval card's header).
    header_title = f"❓ {_normalize_header(first.header)}"
    rows: list[tuple[Action, ...]] = []
    for idx, opt in enumerate(first.options):
        label = opt.label or f"Option {idx + 1}"
        # Pack only the question text (typically short) plus per-
        # option metadata into the click value. Verbose option
        # descriptions stay out of the value to keep Lark's click-
        # event payload comfortably under its size limits — the
        # post-click card rebuilds body from `question` alone.
        rows.append(
            (
                Action(
                    label=label,
                    action_id=ACTION_PICK,
                    value={
                        "tool_id": tool_id,
                        "idx": str(idx),
                        "label": label,
                        "question": first.question,
                        "header": header_title,
                    },
                ),
            )
        )
    return Card(
        text=body_text,
        rows=tuple(rows),
        header_title=header_title,
        header_color=_HEADER_COLOR,
        is_status_carrier=True,
    )


def _picked_card(event: ActionEvent, option_idx: int) -> Card:
    """Build the post-click card: keep the question + header from
    the original card, drop the buttons + option list, and append a
    "✓ Picked: <label>" footer. Skipping the option enumeration
    here keeps the post-click card compact (and well under Lark's
    per-element truncation threshold) — the user already saw the
    full list when they clicked, and the choice is what matters
    going forward."""
    label = event.value.get("label", f"Option {option_idx + 1}")
    question = event.value.get("question", "")
    header = event.value.get("header", "❓ Question")
    text = f"{question}\n\n✓ Picked: {label}" if question else f"✓ Picked: {label}"
    return Card(
        text=text,
        rows=(),
        header_title=header,
        header_color=_HEADER_COLOR,
    )


class AskUserService:
    """Click handler for `AskUserQuestion` option-pick buttons.

    The card itself is built by the Dispatcher (which has the
    transcript event in hand and owns the tool_use → tool_result
    anchor pairing). This service is purely the inbound side: it
    listens for `ACTION_PICK` clicks and drives the TUI picker.
    """

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        channel: Channel,
        allow_list: AllowList,
        message_seq: MessageSeqService,
    ) -> None:
        self._registry = registry
        self._multiplexer = multiplexer
        self._channel = channel
        self._allow_list = allow_list
        self._message_seq = message_seq

    def install(self, channel: Channel) -> None:
        channel.on_action(self._allow_list.guard_action(self._handle_action))

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id != ACTION_PICK:
            return  # not ours
        try:
            option_idx = int(event.value.get("idx", ""))
        except ValueError:
            await self._channel.ack(event, "Invalid option")
            return
        if option_idx < 0:
            await self._channel.ack(event, "Invalid option")
            return
        pane_id = self._registry.get_pane(event.sender, event.conversation)
        if pane_id is None:
            await self._channel.ack(event, "No bound pane — refresh /sessions")
            return
        # Picker starts on option 0; Down moves down, Enter selects.
        # One key per call: don't rely on tmux's whitespace splitting
        # of named keys, which differs across libtmux versions.
        for _ in range(option_idx):
            ok = await self._multiplexer.send_keys(pane_id, "Down", enter=False, literal=False)
            if not ok:
                await self._channel.ack(event, "send_keys failed")
                return
        ok = await self._multiplexer.send_keys(pane_id, "Enter", enter=False, literal=False)
        if not ok:
            await self._channel.ack(event, "send_keys failed")
            return
        # Patch the card: keep question + header, drop buttons, show
        # what was picked. The dispatcher will edit the card again
        # when the tool_result arrives via JSONL with Claude's
        # synthesized answer; this interim state gives instant
        # feedback so the user doesn't keep tapping. Inside a click
        # dispatch, channel.edit rides the inline-refresh slot —
        # the swap lands atomically with the click ack.
        outbound = Outbound(
            conversation=event.conversation,
            content=CardContent(card=_picked_card(event, option_idx)),
        )
        outbound, _ = self._message_seq.stamp_edit(
            event.sender, event.conversation, event.card_anchor, outbound
        )
        await self._channel.edit(event.card_anchor, outbound)
        await self._channel.ack(event, f"Picked option {option_idx + 1}")


__all__ = [
    "ACTION_PICK",
    "TOOL_NAME",
    "AskUserService",
    "build_card",
    "parse_questions",
]
