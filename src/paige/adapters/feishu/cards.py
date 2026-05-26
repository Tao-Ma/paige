"""Card → Feishu interactive card JSON (pure renderer).

Feishu's interactive card has this shape:

    {
      "config": {
        "wide_screen_mode": true,
        "update_multi": true        # let all clients see edits
      },
      "header": {
        "title": {"tag": "plain_text", "content": "..."},
        "template": "blue"          # optional template color
      },
      "elements": [
        {"tag": "markdown", "content": "..."},
        {"tag": "action", "actions": [...], "layout": "trisection"},
        ...
      ]
    }

This module renders paige's `Card` into that structure. Pure, no
SDK dep. The channel adapter feeds the output to
`im.v1.message.create` (msg_type=interactive) after `json.dumps`.

Action button packing — when paige renders an `Action` to a Feishu
button, it packs both `action_id` and the `value` dict into the
button's `value` field as one merged dict:

    Action(action_id="ses:bind", value={"pane_id": "@7"})
        ↓
    {
      "tag": "button",
      "text": {"tag": "plain_text", "content": "..."},
      "value": {"action_id": "ses:bind", "pane_id": "@7"}
    }

The inverse parse (extract action_id + value back from a tap event)
lives in `inbound.py::to_action_event`.

Action layout: rows with 1 button render full-width, 2 buttons
`bisected`, 3 buttons `trisection`. n>=4 falls through to Feishu's
default flow layout (wraps as needed). The hard constraints come
from v1 (`13a7014`): n=3 buttons in flow mode wrap to 2+1 on narrow
mobile widths, breaking the visual rhythm.
"""

from __future__ import annotations

import re
from typing import Any

from ...domain.card import Action, ActionCell, Card, InputSlot, TextCell

CardJson = dict[str, Any]


# Map paige header_color values onto Feishu template names. These
# are the documented templates; passing any other string falls
# through unmolested for Feishu to validate.


# A GFM pipe table: header row | separator | one or more body rows.
# Trailing newline on the last row is optional. The separator row
# may contain only `-`, `:`, `|`, and whitespace.
_GFM_TABLE_RE = re.compile(
    r"^\s*\|.+\|\s*\n\s*\|[\s\-:|]+\|\s*(\n\s*\|.+\|\s*)+\s*$",
)


def image_card(
    *,
    image_key: str,
    rows: tuple[tuple[Action, ...], ...] = (),
    thread_id: str | None = None,
    topic_id: str | None = None,
    alt: str = "",
) -> CardJson:
    """Build an interactive card carrying an `img` element followed
    by one `column_set` per row of buttons. Emits the JSON 2.0
    envelope to match `to_card` — keeps every paige-emitted card on
    a single schema so anchors never see cross-schema edits.

    Feishu image messages can't carry buttons, so `DocumentContent(
    as_image=True, rows=...)` rides on a card. `image_key` comes
    from a prior `upload_image` call. `alt` lands as the image's
    accessibility label and falls back to a single space because
    Feishu rejects empty plain_text content.
    """
    elements: list[CardJson] = [
        {
            "tag": "img",
            "img_key": image_key,
            "alt": {"tag": "plain_text", "content": alt or " "},
            "mode": "fit_horizontal",
            "preview": True,
        }
    ]
    for row in rows:
        if not row:
            continue
        elements.append(_action_row_v2(row, thread_id=thread_id, topic_id=topic_id))
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": elements},
    }


def to_card(
    card: Card,
    *,
    thread_id: str | None = None,
    topic_id: str | None = None,
    collapse_threshold_lines: int = 0,
) -> CardJson:
    """Render a paige `Card` to Feishu interactive-card JSON.

    `update_multi=True` is set on every card — it's how Feishu
    propagates edits to every viewer's pane (without it, only the
    editor sees the change).

    `thread_id` is round-tripped through every button's value field
    as `_thread_id`. The inbound parser lifts it back out so a click
    event reliably reports the same thread_id the card was sent
    under. Without this, Feishu's `context.thread_id` lands as None
    on p2p clicks and a bind made via click would never match the
    user's typed-text follow-up.

    `collapse_threshold_lines` (>0): when the card body has more
    newlines than this, the body markdown elements are wrapped in a
    `collapsible_panel` (default `expanded: false`) so the user
    sees a tap-to-expand header instead of a wall of text. Action
    rows stay outside the panel — buttons must remain reachable
    without expanding. Requires Lark client v7.9+; older clients
    render an "upgrade your client" placeholder. Set to 0 to
    disable.

    GFM tables in the body get rendered as Lark's native `table`
    element (a JSON 2.0 component).
    """
    body_elements = _body_elements(card.text or "")
    # Cards may opt out of the per-conversation collapse pref (e.g.
    # `/livepane`'s live buffer should never auto-fold mid-stream).
    if card.force_no_collapse:
        collapse_threshold_lines = 0
    # Every card is rendered in the JSON 2.0 envelope. The legacy v1
    # path was removed when the /sessions Back / Delete inline-refresh
    # bugs surfaced — Lark silently drops PATCH / click-response edits
    # when the source anchor's schema differs from the replacement
    # card's, and unifying on v2 closes that whole class of failure.
    return _to_card_v2(
        card,
        body_elements,
        thread_id=thread_id,
        topic_id=topic_id,
        collapse_threshold_lines=collapse_threshold_lines,
    )


def _to_card_v2(
    card: Card,
    body_elements: list[CardJson],
    *,
    thread_id: str | None = None,
    topic_id: str | None = None,
    collapse_threshold_lines: int = 0,
) -> CardJson:
    """JSON 2.0 envelope — paige's only render path.

    Buttons render as native v2 `tag:"button"` elements (with
    `behaviors[].type="callback"` for the payload), wrapped in a
    `column_set` per row so multiple buttons sit side-by-side
    instead of stacking. The v1 `tag:"action"` element shape would
    400 in v2; this is what works.

    `input` elements live in `body.elements` alongside markdown /
    table elements; they use v2's `behaviors` array for callback
    payloads (v1's `value` field is silently ignored under v2).

    `collapse_threshold_lines` (>0): when the body markdown spans
    more newlines than this, body elements are wrapped in a
    `collapsible_panel` so the user sees a tap-to-expand header
    rather than a wall of text. Action rows / inputs / data rows
    stay outside the panel — buttons must remain reachable without
    expanding. Requires Lark client v7.9+; older clients fall back
    to an "upgrade your client" placeholder. Set to 0 to disable.
    """
    body = card.text or ""
    body_line_count = body.count("\n") + 1 if body else 0
    if collapse_threshold_lines > 0 and body_line_count > collapse_threshold_lines:
        elements: list[CardJson] = [_collapsible_panel(body_elements, line_count=body_line_count)]
    else:
        elements = [*body_elements]
    for slot in card.inputs:
        elements.append(_input_element_v2(slot, thread_id=thread_id, topic_id=topic_id))
    # Faux-table data rows go between the body and the action-row
    # controls (pagination, nav) so the table reads as part of the
    # body content and the controls sit beneath it. An `hr` separates
    # them when both are present, mirroring how the /sessions Resume
    # sub-pane already breaks data from controls visually.
    data_rows_rendered = False
    for cs_row in card.column_set_rows:
        if not cs_row:
            continue
        elements.append(_column_set_data_row_v2(cs_row, thread_id=thread_id, topic_id=topic_id))
        data_rows_rendered = True
    if data_rows_rendered and card.rows:
        elements.append({"tag": "hr"})
    for row in card.rows:
        if not row:
            continue
        elements.append(_action_row_v2(row, thread_id=thread_id, topic_id=topic_id))
    if card.status_badge:
        # Lark JSON 2.0 supports the same `markdown` tag we use for
        # body content; render the badge as a small italic line so
        # it reads as a footer note without needing v2's dedicated
        # widget set.
        elements.append({"tag": "markdown", "content": f"_⏱ {card.status_badge}_"})
    out: CardJson = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": elements},
    }
    header = _maybe_header(card)
    if header is not None:
        out["header"] = header
    return out


def _input_element_v2(slot: InputSlot, *, thread_id: str | None, topic_id: str | None) -> CardJson:
    """Render an editable text input under JSON 2.0. Submit fires a
    `callback` behavior carrying our static value payload; the user's
    typed text lands in the event payload as `input_value`.

    Submission is via Enter — Lark's v2 `input` schema rejects an
    inline submit-button property (verified live: `confirm_button`
    triggers a `200621 unknown property` rejection from the
    card-parse path). `slot.submit_label` is informational only
    until we find / test a working button-on-input property."""
    value: dict[str, Any] = {"action_id": slot.action_id}
    value.update(slot.value)
    if thread_id is not None:
        value["_thread_id"] = thread_id
    if topic_id is not None:
        value["_topic_id"] = topic_id
    return {
        "tag": "input",
        "label": {"tag": "plain_text", "content": slot.label},
        "label_position": "left",
        "default_value": slot.default_value,
        "placeholder": {"tag": "plain_text", "content": slot.placeholder or " "},
        "width": "fill",
        "behaviors": [
            {"type": "callback", "value": value},
        ],
    }


def _action_row_v2(
    row: tuple[Action, ...], *, thread_id: str | None, topic_id: str | None
) -> CardJson:
    """One row of buttons in a JSON 2.0 card. Each button is a v2
    `tag:"button"` element with a `callback` behavior; rows render
    via `column_set` with one column per button so they sit
    side-by-side instead of stacking."""
    columns: list[CardJson] = []
    for action in row:
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_button_v2(action, thread_id=thread_id, topic_id=topic_id)],
            }
        )
    return {"tag": "column_set", "columns": columns}


def _column_set_data_row_v2(
    row: tuple[TextCell | ActionCell, ...],
    *,
    thread_id: str | None,
    topic_id: str | None,
) -> CardJson:
    """Render one faux-table data row as a `column_set`. `TextCell`
    cells become weighted markdown columns; `ActionCell` cells become
    auto-width columns holding a small primary button. Sizing/spacing
    knobs are baked in — small horizontal_spacing, vertical_align
    center, modest padding — live-validated against Lark JSON 2.0."""
    columns: list[CardJson] = []
    for cell in row:
        if isinstance(cell, TextCell):
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": cell.weight,
                    "vertical_align": "center",
                    "padding": "6px 8px",
                    "elements": [{"tag": "markdown", "content": cell.content}],
                }
            )
        else:
            columns.append(
                {
                    "tag": "column",
                    "width": "auto",
                    "vertical_align": "center",
                    "padding": "4px 8px",
                    "elements": [
                        _column_set_button_v2(
                            cell.action,
                            thread_id=thread_id,
                            topic_id=topic_id,
                        )
                    ],
                }
            )
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "small",
        "columns": columns,
    }


def _column_set_button_v2(
    action: Action, *, thread_id: str | None, topic_id: str | None
) -> CardJson:
    """Compact button variant for faux-table cells: `size: "small"`,
    `type: "primary"`, no `width: "fill"` so the button hugs its
    label. Same `_thread_id` / `_topic_id` round-trip as `_button_v2`."""
    value: dict[str, Any] = {"action_id": action.action_id}
    value.update(action.value)
    if thread_id is not None:
        value["_thread_id"] = thread_id
    if topic_id is not None:
        value["_topic_id"] = topic_id
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": action.label},
        "type": "primary",
        "size": "small",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _button_v2(action: Action, *, thread_id: str | None, topic_id: str | None) -> CardJson:
    """Native v2 button — `tag:"button"` with `behaviors[].type=
    "callback"` carrying the value payload. Same `_thread_id` /
    `_topic_id` round-trip the v1 button uses, so click events
    recover the topic / chain identifiers the card was sent under."""
    value: dict[str, Any] = {"action_id": action.action_id}
    value.update(action.value)
    if thread_id is not None:
        value["_thread_id"] = thread_id
    if topic_id is not None:
        value["_topic_id"] = topic_id
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": action.label},
        "type": "default",
        "size": "medium",
        "width": "fill",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _body_elements(body: str) -> list[CardJson]:
    """Split the body into a list of Lark card elements — `markdown`
    for prose paragraphs, `table` for any chunk that's a GFM pipe
    table. Empty body collapses to a single space (Feishu rejects
    cards with zero elements)."""
    chunks = _split_paragraphs(body)
    if not chunks:
        return [{"tag": "markdown", "content": " "}]
    elements: list[CardJson] = []
    for chunk in chunks:
        if _is_gfm_table(chunk):
            elements.append(_markdown_table_to_lark(chunk))
        else:
            # Lark's JSON 2.0 `markdown` element only recognises a fenced
            # code block when the fence carries a leading + trailing
            # newline margin *inside* the element content. Without it the
            # ``` lines render literally and any `# comment` inside the
            # block renders as a heading. `_split_paragraphs` strips each
            # chunk, so re-add the margin here for chunks that contain a
            # fence. Plain prose is left untouched.
            content = f"\n{chunk}\n" if "```" in chunk else chunk
            elements.append({"tag": "markdown", "content": content})
    return elements


# ── header ──────────────────────────────────────────────────────


def _maybe_header(card: Card) -> CardJson | None:
    """Build the `header` dict if `header_title` is set. Color is
    optional; when set it's a Feishu template name (blue, turquoise,
    green, yellow, orange, red, carmine, violet, purple, indigo,
    grey, wathet)."""
    if not card.header_title:
        return None
    header: CardJson = {
        "title": {"tag": "plain_text", "content": card.header_title},
    }
    if card.header_color:
        header["template"] = card.header_color
    return header


# ── elements ────────────────────────────────────────────────────


def _is_gfm_table(chunk: str) -> bool:
    """True if `chunk` looks like a GitHub-flavored markdown pipe
    table: a header row, a `---|---|---` separator row, and one or
    more body rows. We deliberately don't try to validate semantic
    width agreement — Claude's output is well-formed, and an
    almost-table that fails our regex degrades to a markdown
    element which is exactly what we'd render today."""
    return bool(_GFM_TABLE_RE.match(chunk))


def _split_table_row(line: str) -> list[str]:
    """Trim leading/trailing pipes and per-cell whitespace from one
    table row. `| a | b | c |` → `["a", "b", "c"]`."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _markdown_table_to_lark(chunk: str) -> CardJson:
    """Convert a GFM markdown table chunk to a Lark `table` element.

    Header row → columns with `display_name = cell text`,
    `name = col<i>`, `data_type = "lark_md"` so inline markdown
    (`**bold**`, `` `code` ``, `[link](...)`, emoji) renders inside
    cells. Width defaults to `"auto"` per column — Lark picks
    reasonable widths from the content.

    Schema live-validated against Lark JSON 2.0 (code=0)."""
    lines = [line for line in chunk.splitlines() if line.strip()]
    headers = _split_table_row(lines[0])
    body_rows = [_split_table_row(line) for line in lines[2:]]  # skip header + separator
    width = len(headers)
    columns: list[CardJson] = [
        {
            "name": f"col{i}",
            "display_name": header,
            "data_type": "lark_md",
            "width": "auto",
        }
        for i, header in enumerate(headers)
    ]
    rows: list[CardJson] = []
    for row in body_rows:
        padded = (row + [""] * width)[:width]
        rows.append({f"col{i}": cell for i, cell in enumerate(padded)})
    return {
        "tag": "table",
        "row_height": "low",
        "header_style": {
            "background_style": "grey",
            "bold": True,
            "lines": 1,
        },
        "columns": columns,
        "rows": rows,
    }


def _collapsible_panel(body_elements: list[CardJson], *, line_count: int) -> CardJson:
    """Wrap `body_elements` in a Lark `collapsible_panel` (v7.9+).
    Header advertises the line count so the user knows what they're
    expanding into. Default `expanded: false` — caller wants the
    summary view first; the verbose body opens on tap.

    Schema kept minimal — earlier probe confirmed that custom
    padding/border fields are easy to mis-format. The default
    rendering is fine."""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "markdown",
                "content": f"📋 {line_count} lines (tap to expand)",
            },
            "icon": {
                "tag": "standard_icon",
                "token": "down-bold_outlined",
                "color": "grey",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": body_elements,
    }


def _split_paragraphs(body: str) -> list[str]:
    """Split on `\\n\\n` while preserving fenced code blocks. A
    paragraph with an odd ``` count is glued to the following
    paragraphs until the fence closes."""
    out: list[str] = []
    buffer = ""
    in_fence = False
    for raw in body.split("\n\n"):
        chunk = raw.strip()
        if not chunk and not in_fence:
            continue
        if not in_fence:
            buffer = chunk
        elif buffer:
            buffer = f"{buffer}\n\n{raw}"
        else:
            buffer = raw
        # ``` count parity flips for every fence marker; odd parity
        # means we're still inside an open block.
        if buffer.count("```") % 2 == 1:
            in_fence = True
            continue
        in_fence = False
        if buffer.strip():
            out.append(buffer)
        buffer = ""
    if buffer.strip():
        out.append(buffer)
    return out


__all__ = ["CardJson", "image_card", "to_card"]
