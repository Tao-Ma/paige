"""Card → Feishu card JSON renderer.

Every paige-emitted card uses the JSON 2.0 envelope: a top-level
`schema: "2.0"` field, `body.elements` carrying markdown / table /
input / img / column_set children, action rows wrapped one-per-
column in a `column_set`, and a flat `config` with `update_multi:
True`. The v1 envelope (top-level `elements`,
`config.wide_screen_mode`, `tag:"action"` rows) is no longer
produced — both `to_card` and `image_card` emit v2.
"""

from __future__ import annotations

from paige.adapters.feishu.cards import image_card, to_card
from paige.domain.card import Action, Card, InputSlot

# ── envelope shape ──────────────────────────────────────────────


def test_minimal_card_uses_v2_envelope_with_single_markdown_body() -> None:
    card_json = to_card(Card(text="hello"))
    assert card_json["schema"] == "2.0"
    assert card_json["config"] == {"update_multi": True}
    assert "elements" not in card_json
    elements = card_json["body"]["elements"]
    assert elements == [{"tag": "markdown", "content": "hello"}]


def test_empty_text_renders_as_single_space() -> None:
    """Feishu rejects cards with empty markdown content; we fall
    back to a single space so the card is still valid."""
    card_json = to_card(Card(text=""))
    assert card_json["body"]["elements"][0]["content"] == " "


# ── header ───────────────────────────────────────────────────────


def test_header_title_renders_plain_text() -> None:
    card_json = to_card(Card(text="body", header_title="Pick a project"))
    assert card_json["header"] == {
        "title": {"tag": "plain_text", "content": "Pick a project"},
    }


def test_header_color_renders_template() -> None:
    card_json = to_card(Card(text="body", header_title="X", header_color="blue"))
    assert card_json["header"]["template"] == "blue"


def test_header_color_without_title_is_dropped() -> None:
    """No title → no header (color alone makes no sense)."""
    card_json = to_card(Card(text="body", header_color="blue"))
    assert "header" not in card_json


# ── action rows ──────────────────────────────────────────────────


def test_one_button_renders_as_column_set_with_one_native_button() -> None:
    card_json = to_card(Card(text="t", rows=((Action(label="Yes", action_id="y"),),)))
    elements = card_json["body"]["elements"]
    assert len(elements) == 2
    [md, column_set] = elements
    assert md == {"tag": "markdown", "content": "t"}
    assert column_set["tag"] == "column_set"
    [column] = column_set["columns"]
    assert column["tag"] == "column"
    [button] = column["elements"]
    assert button["tag"] == "button"
    assert button["text"] == {"tag": "plain_text", "content": "Yes"}


def test_button_value_packs_action_id_with_value_dict() -> None:
    """The button's callback value should be a single dict carrying
    both `action_id` and any extra fields from `Action.value`."""
    card_json = to_card(
        Card(
            text="t",
            rows=(
                (
                    Action(
                        label="Bind",
                        action_id="ses:bind",
                        value={"pane_id": "@7"},
                    ),
                ),
            ),
        )
    )
    [_md, column_set] = card_json["body"]["elements"]
    [column] = column_set["columns"]
    [button] = column["elements"]
    assert button["behaviors"][0]["type"] == "callback"
    assert button["behaviors"][0]["value"] == {
        "action_id": "ses:bind",
        "pane_id": "@7",
    }


def test_multi_button_row_renders_one_column_per_button() -> None:
    """Multiple buttons in a row become sibling columns inside the
    same `column_set`, so they sit side-by-side instead of stacking."""
    card_json = to_card(
        Card(
            text="t",
            rows=(
                (
                    Action(label="Yes", action_id="y"),
                    Action(label="No", action_id="n"),
                    Action(label="Maybe", action_id="m"),
                ),
            ),
        )
    )
    [_md, column_set] = card_json["body"]["elements"]
    columns = column_set["columns"]
    assert len(columns) == 3
    labels = [c["elements"][0]["text"]["content"] for c in columns]
    assert labels == ["Yes", "No", "Maybe"]


def test_multiple_rows_render_as_multiple_column_sets() -> None:
    card_json = to_card(
        Card(
            text="t",
            rows=(
                (Action(label="A", action_id="a"),),
                (Action(label="B", action_id="b"),),
            ),
        )
    )
    elements = card_json["body"]["elements"]
    assert len(elements) == 3  # markdown + 2 column_sets
    assert elements[1]["tag"] == "column_set"
    assert elements[2]["tag"] == "column_set"


def test_empty_row_is_skipped() -> None:
    """A row tuple with no buttons shouldn't render as an empty
    column_set (Feishu rejects)."""
    card_json = to_card(Card(text="t", rows=((), (Action(label="X", action_id="x"),))))
    elements = card_json["body"]["elements"]
    # md + the one valid action row
    assert len(elements) == 2


# ── image_card ───────────────────────────────────────────────────


def test_image_card_renders_img_first_then_column_set_rows() -> None:
    rows = (
        (Action(label="↑", action_id="ss:key", value={"k": "up"}),),
        (Action(label="🔄", action_id="ss:rfr"),),
    )
    card_json = image_card(image_key="img_xxx", rows=rows, alt="screenshot.png")
    assert card_json["schema"] == "2.0"
    assert card_json["config"] == {"update_multi": True}
    elements = card_json["body"]["elements"]
    # img + 2 column_set rows
    assert len(elements) == 3
    assert elements[0] == {
        "tag": "img",
        "img_key": "img_xxx",
        "alt": {"tag": "plain_text", "content": "screenshot.png"},
        "mode": "fit_horizontal",
        "preview": True,
    }
    assert elements[1]["tag"] == "column_set"
    assert elements[1]["columns"][0]["elements"][0]["text"]["content"] == "↑"
    assert elements[2]["columns"][0]["elements"][0]["text"]["content"] == "🔄"


def test_image_card_empty_alt_falls_back_to_space() -> None:
    """Feishu rejects empty plain_text content."""
    card_json = image_card(image_key="img_x", alt="")
    assert card_json["body"]["elements"][0]["alt"]["content"] == " "


# ── body paragraph splitting ─────────────────────────────────────


def test_body_split_into_paragraphs_renders_each_as_markdown() -> None:
    """Paragraphs separated by blank lines become separate markdown
    elements so Lark's per-element truncation can't eat the tail of
    a long enumeration."""
    card = Card(text="Question?\n\n**1. Option A**\n\n**2. Option B**")
    elements = to_card(card)["body"]["elements"]
    assert [e["tag"] for e in elements] == ["markdown", "markdown", "markdown"]
    assert elements[0]["content"] == "Question?"
    assert elements[1]["content"] == "**1. Option A**"
    assert elements[2]["content"] == "**2. Option B**"


def test_body_keeps_fenced_code_block_intact() -> None:
    """A fenced code block that contains blank lines must NOT get
    split mid-fence."""
    body = "```\nBash command\n\n$ echo hi\n\n3. No\n```"
    card = Card(text=body)
    elements = to_card(card)["body"]["elements"]
    # Whole fenced block coalesced into one markdown element.
    assert len(elements) == 1
    content = elements[0]["content"]
    assert content.count("```") == 2  # both fence markers preserved
    assert "Bash command" in content
    assert "$ echo hi" in content
    assert "3. No" in content
    # Fenced chunks get a leading + trailing newline margin so Lark
    # renders them as a code block (else the ``` lines render raw and a
    # `#` line inside leaks as a heading).
    assert content.startswith("\n")
    assert content.endswith("\n")


def test_body_fenced_block_with_hash_comment_is_margined() -> None:
    """A bash block whose code contains a `#` comment must stay inside a
    properly-margined fence — regression for `#` rendering as a heading."""
    body = "```bash\necho hi\n# a comment\nls\n```"
    elements = to_card(Card(text=body))["body"]["elements"]
    assert len(elements) == 1
    content = elements[0]["content"]
    assert content.startswith("\n") and content.endswith("\n")
    assert "# a comment" in content  # preserved verbatim inside the fence


def test_body_plain_prose_not_margined() -> None:
    """Non-fenced chunks are left exactly as-is (no spurious margin)."""
    elements = to_card(Card(text="just prose"))["body"]["elements"]
    assert elements[0]["content"] == "just prose"


def test_body_paragraph_around_fenced_block_splits_outside_only() -> None:
    """Paragraphs outside the fence still split; the fence and its
    interior stay intact."""
    body = "Heading\n\n```\nlong\n\ncommand\n```\n\nFooter"
    card = Card(text=body)
    elements = to_card(card)["body"]["elements"]
    assert len(elements) == 3
    assert elements[0]["content"] == "Heading"
    assert "```" in elements[1]["content"]
    assert "long" in elements[1]["content"]
    assert "command" in elements[1]["content"]
    assert elements[2]["content"] == "Footer"


# ── thread / topic round-trip ────────────────────────────────────


def test_image_card_threads_thread_id_into_buttons() -> None:
    """Same round-trip pattern as to_card — clicks must report the
    thread the card was sent under."""
    rows = ((Action(label="🔄", action_id="ss:rfr"),),)
    card_json = image_card(image_key="img_x", rows=rows, thread_id="om_root")
    button = card_json["body"]["elements"][1]["columns"][0]["elements"][0]
    assert button["behaviors"][0]["value"]["_thread_id"] == "om_root"


def test_button_value_round_trips_topic_id_when_set() -> None:
    """In a topic-mode group, the Lark `omt_xxx` topic id is
    round-tripped through every button so a click event recovers it
    even when Feishu's `context.thread_id` is absent."""
    card_json = to_card(
        Card(
            text="t",
            rows=(
                (
                    Action(
                        label="Bind",
                        action_id="ses:bind",
                        value={"pane_id": "@7"},
                    ),
                ),
            ),
        ),
        thread_id="om_root",
        topic_id="omt_topic_xyz",
    )
    [_md, column_set] = card_json["body"]["elements"]
    [column] = column_set["columns"]
    [button] = column["elements"]
    assert button["behaviors"][0]["value"] == {
        "action_id": "ses:bind",
        "pane_id": "@7",
        "_thread_id": "om_root",
        "_topic_id": "omt_topic_xyz",
    }


def test_image_card_round_trips_topic_id_into_buttons() -> None:
    rows = ((Action(label="🔄", action_id="ss:rfr"),),)
    card_json = image_card(image_key="img_x", rows=rows, thread_id="om_root", topic_id="omt_topic")
    button = card_json["body"]["elements"][1]["columns"][0]["elements"][0]
    assert button["behaviors"][0]["value"]["_topic_id"] == "omt_topic"
    assert button["behaviors"][0]["value"]["_thread_id"] == "om_root"


# ── Long-body collapsible_panel ─────────────────────────────────


def test_long_body_below_threshold_stays_flat() -> None:
    body = "\n".join(f"line {i}" for i in range(40))
    card_json = to_card(Card(text=body), collapse_threshold_lines=50)
    tags = [e["tag"] for e in card_json["body"]["elements"]]
    assert "collapsible_panel" not in tags


def test_long_body_above_threshold_wraps_in_collapsible() -> None:
    body = "\n".join(f"line {i}" for i in range(120))
    card_json = to_card(Card(text=body), collapse_threshold_lines=50)
    elements = card_json["body"]["elements"]
    # Exactly one body element — the collapsible_panel — since this
    # card has no buttons.
    assert len(elements) == 1
    panel = elements[0]
    assert panel["tag"] == "collapsible_panel"
    assert panel["expanded"] is False
    assert "120" in panel["header"]["title"]["content"]  # line count advertised
    inner = panel["elements"]
    assert all(e["tag"] == "markdown" for e in inner)


def test_threshold_zero_disables_collapsing() -> None:
    """0 = feature off. Long bodies render as before (flat
    markdown elements)."""
    body = "\n".join(f"line {i}" for i in range(120))
    card_json = to_card(Card(text=body), collapse_threshold_lines=0)
    tags = [e["tag"] for e in card_json["body"]["elements"]]
    assert "collapsible_panel" not in tags


def test_collapsible_keeps_action_rows_outside_panel() -> None:
    """Buttons must remain reachable without expanding the body —
    otherwise the user can't click Refresh / Dismiss / etc. unless
    they tap the panel header first."""
    body = "\n".join(f"line {i}" for i in range(120))
    rows = ((Action(label="🔄", action_id="x:r"),),)
    card_json = to_card(
        Card(text=body, rows=rows),
        collapse_threshold_lines=50,
    )
    elements = card_json["body"]["elements"]
    # First element: the panel; last: the column_set carrying the button.
    assert elements[0]["tag"] == "collapsible_panel"
    assert elements[-1]["tag"] == "column_set"


def test_collapsible_preserves_paragraph_split_inside_panel() -> None:
    """The per-paragraph body split (existing workaround for Lark's
    long-element truncation bug) must still apply inside the panel
    — otherwise the truncation bug returns when the panel is
    expanded."""
    body = "para A\n\n```\ncode\n```\n\npara B" + "\nfiller" * 80
    card_json = to_card(Card(text=body), collapse_threshold_lines=50)
    panel = card_json["body"]["elements"][0]
    inner_contents = [e["content"] for e in panel["elements"]]
    assert any("para A" in c for c in inner_contents)
    assert any("```" in c and "code" in c for c in inner_contents)
    assert any("para B" in c for c in inner_contents)


# ── GFM tables → Lark `table` element ────────────────────────────


_SAMPLE_TABLE = (
    "| Name | Notes | Cost |\n"
    "| --- | --- | --- |\n"
    "| alpha | **spike** | $1,234 |\n"
    "| bravo | `ready`  | $89 |\n"
)


def test_table_only_body_renders_native_table_element() -> None:
    """A body that is exactly a GFM table emits a `table` element
    instead of a `markdown` one."""
    card_json = to_card(Card(text=_SAMPLE_TABLE))
    assert card_json["schema"] == "2.0"
    elements = card_json["body"]["elements"]
    assert len(elements) == 1
    table = elements[0]
    assert table["tag"] == "table"
    # 3 columns from the header row, 2 body rows.
    assert [c["display_name"] for c in table["columns"]] == ["Name", "Notes", "Cost"]
    assert [c["data_type"] for c in table["columns"]] == ["lark_md"] * 3
    assert len(table["rows"]) == 2
    assert table["rows"][0] == {"col0": "alpha", "col1": "**spike**", "col2": "$1,234"}
    assert table["rows"][1] == {"col0": "bravo", "col1": "`ready`", "col2": "$89"}


def test_table_card_keeps_header() -> None:
    card_json = to_card(Card(text=_SAMPLE_TABLE, header_title="Costs", header_color="blue"))
    assert card_json["schema"] == "2.0"
    assert card_json["header"]["title"]["content"] == "Costs"
    assert card_json["header"]["template"] == "blue"


def test_table_with_prose_around_it_emits_mixed_body() -> None:
    """Body with paragraphs around a table: the prose stays as
    `markdown` elements, the table becomes a `table` element."""
    body = f"Summary of work:\n\n{_SAMPLE_TABLE}\nNote: prices in USD."
    card_json = to_card(Card(text=body))
    elements = card_json["body"]["elements"]
    tags = [e["tag"] for e in elements]
    assert tags == ["markdown", "table", "markdown"]
    assert "Summary of work" in elements[0]["content"]
    assert "USD" in elements[2]["content"]


def test_table_with_action_rows_emits_native_buttons() -> None:
    """A card carrying a GFM table AND action buttons: the table
    renders as a native `table` element, and the buttons ride as
    native `tag:"button"` elements inside a `column_set`."""
    card_json = to_card(Card(text=_SAMPLE_TABLE, rows=((Action(label="OK", action_id="ok"),),)))
    tags = [e["tag"] for e in card_json["body"]["elements"]]
    assert tags == ["table", "column_set"]
    column_set = card_json["body"]["elements"][1]
    [column] = column_set["columns"]
    [button] = column["elements"]
    assert button["tag"] == "button"


def test_pseudo_table_without_separator_row_renders_as_markdown() -> None:
    """Pipe-using prose like `| key | value |` without a `---`
    separator row underneath should NOT trigger the table path —
    it'd lose its structure if we tried to coerce it."""
    body = "| Some | text | with pipes |\n| but no separator below."
    card_json = to_card(Card(text=body))
    assert card_json["body"]["elements"][0]["tag"] == "markdown"


# ── editable input slots ────────────────────────────────────────


def test_input_slot_renders_as_v2_input_element() -> None:
    """Lark's `input` element uses the `behaviors` array rather than
    v1's `value` field."""
    card_json = to_card(
        Card(
            text="ready",
            inputs=(
                InputSlot(
                    label="1",
                    default_value="what's next",
                    action_id="ready:slot",
                    value={"slot": "0"},
                    placeholder="edit + Send",
                ),
            ),
        )
    )
    assert card_json["schema"] == "2.0"
    elements = card_json["body"]["elements"]
    # markdown body chunk, then the input.
    tags = [e["tag"] for e in elements]
    assert tags == ["markdown", "input"]
    input_elem = elements[1]
    assert input_elem["default_value"] == "what's next"
    assert input_elem["label"]["content"] == "1"
    assert input_elem["placeholder"]["content"] == "edit + Send"
    # v2: callback payload lives under behaviors[0].value.
    assert "value" not in input_elem
    behaviors = input_elem["behaviors"]
    assert len(behaviors) == 1
    assert behaviors[0]["type"] == "callback"
    assert behaviors[0]["value"]["action_id"] == "ready:slot"
    assert behaviors[0]["value"]["slot"] == "0"


def test_input_slot_callback_value_includes_thread_id_round_trip() -> None:
    """Same round-trip the action buttons use — the inbound parser
    relies on `_thread_id` being in the callback value so a submit
    in a thread reports the same thread the card was sent under."""
    card_json = to_card(
        Card(
            text=" ",
            inputs=(
                InputSlot(
                    label="✏️",
                    default_value="",
                    action_id="ready:free",
                ),
            ),
        ),
        thread_id="om_root_xyz",
    )
    input_elem = card_json["body"]["elements"][1]
    assert input_elem["behaviors"][0]["value"]["_thread_id"] == "om_root_xyz"


def test_input_slot_callback_value_round_trips_topic_id() -> None:
    """End-turn panels rendered inside a Lark topic must round-trip
    `_topic_id` so the submit event keys back to the same topic."""
    card_json = to_card(
        Card(
            text=" ",
            inputs=(
                InputSlot(
                    label="✏️",
                    default_value="",
                    action_id="ready:free",
                ),
            ),
        ),
        thread_id="om_root",
        topic_id="omt_topic_xyz",
    )
    callback_value = card_json["body"]["elements"][1]["behaviors"][0]["value"]
    assert callback_value["_topic_id"] == "omt_topic_xyz"
    assert callback_value["_thread_id"] == "om_root"


def test_multiple_input_slots_render_in_declaration_order() -> None:
    card_json = to_card(
        Card(
            text=" ",
            inputs=(
                InputSlot(label="1", default_value="a", action_id="slot"),
                InputSlot(label="2", default_value="b", action_id="slot"),
                InputSlot(label="3", default_value="c", action_id="slot"),
                InputSlot(label="✏️", default_value="", action_id="free"),
            ),
        )
    )
    inputs = [e for e in card_json["body"]["elements"] if e["tag"] == "input"]
    assert len(inputs) == 4
    assert [e["default_value"] for e in inputs] == ["a", "b", "c", ""]


def test_input_slots_with_action_rows_use_native_button() -> None:
    """A card carrying both inputs AND action rows: inputs as
    `tag:"input"`, action rows as `tag:"column_set"` with one column
    per button containing a native `tag:"button"` with a `callback`
    behavior."""
    card_json = to_card(
        Card(
            text="t",
            inputs=(InputSlot(label="1", default_value="x", action_id="i"),),
            rows=((Action(label="OK", action_id="ok"),),),
        )
    )
    tags = [e["tag"] for e in card_json["body"]["elements"]]
    assert tags == ["markdown", "input", "column_set"]
    column_set = card_json["body"]["elements"][2]
    [column] = column_set["columns"]
    [button] = column["elements"]
    assert button["tag"] == "button"
    assert button["behaviors"][0]["type"] == "callback"
    assert button["behaviors"][0]["value"]["action_id"] == "ok"
