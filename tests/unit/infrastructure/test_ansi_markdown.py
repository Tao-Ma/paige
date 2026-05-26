"""ANSI → Lark markdown converter."""

from __future__ import annotations

from paige.infrastructure.ansi_markdown import (
    extract_highlights,
    strip_ansi,
    to_lark_markdown,
)

# ── strip_ansi ───────────────────────────────────────────────────


def test_strip_ansi_removes_sgr_sequences() -> None:
    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_strip_ansi_removes_256_color_sequences() -> None:
    assert strip_ansi("\x1b[38;5;231m\x1b[48;5;105mActive\x1b[39m\x1b[49m") == "Active"


def test_strip_ansi_removes_non_sgr_csi() -> None:
    """Cursor moves, clear-screen, etc. — drop without interpreting."""
    assert strip_ansi("\x1b[2J\x1b[H\x1b[?25hhello") == "hello"


def test_strip_ansi_removes_osc_titles() -> None:
    assert strip_ansi("\x1b]0;window title\x07prompt$ ") == "prompt$ "


def test_strip_ansi_passes_plain_text() -> None:
    assert strip_ansi("no escapes here") == "no escapes here"


# ── to_lark_markdown ─────────────────────────────────────────────


def test_to_lark_markdown_passes_plain_text() -> None:
    """Plain text — no escapes — round-trips with leading-space
    `&nbsp;` and trailing characters intact."""
    out = to_lark_markdown("hello world")
    assert out == "hello world"


def test_to_lark_markdown_wraps_background_color_in_bold_and_font() -> None:
    """The active-tab pattern from Claude Code: 256-color background.
    No native bg in Lark markdown → emit bold + font so the
    highlighted run is visually unmistakable."""
    text = "\x1b[48;5;105mActive\x1b[49m rest"
    out = to_lark_markdown(text)
    assert "**" in out and "Active" in out
    assert "<font" in out
    assert "rest" in out


def test_to_lark_markdown_wraps_foreground_color_in_font() -> None:
    text = "\x1b[31mred text\x1b[0m"
    out = to_lark_markdown(text)
    assert "<font color='red'>" in out
    assert "red text" in out
    assert "</font>" in out


def test_to_lark_markdown_emits_br_between_lines() -> None:
    """Lark's `markdown` element collapses `\\n`. We emit `<br>`
    instead so TUI layout survives the roundtrip."""
    out = to_lark_markdown("line1\nline2")
    assert out == "line1<br>line2"


def test_to_lark_markdown_preserves_leading_spaces_via_nbsp() -> None:
    """Markdown parsers strip leading runs of spaces on a line —
    convert them to `&nbsp;` so the TUI's left-padding doesn't
    collapse into nothing."""
    out = to_lark_markdown("  indented")
    assert out.startswith("&nbsp;&nbsp;")
    assert "indented" in out


def test_to_lark_markdown_escapes_literal_markdown_in_text() -> None:
    """Asterisks / underscores / backticks in TUI content shouldn't
    re-trigger markdown formatting on the Lark side."""
    out = to_lark_markdown("a*star* and _under_ and `tick`")
    assert "\\*" in out
    assert "\\_" in out
    assert "\\`" in out


def test_extract_highlights_pulls_bg_highlighted_label() -> None:
    """The canonical Claude Code multi-tab use case: extract just
    the active-tab label so the caller can surface it as an
    annotation above a monospace code block — no need to render
    the whole capture as markdown."""
    raw = (
        "←  \x1b[48;5;105m ☒ Audience \x1b[49m  ☐ Edit scope  ☐ Save mode  ☐ UX model  ✔ Submit  →"
    )
    assert extract_highlights(raw) == ["☒ Audience"]


def test_extract_highlights_drops_pure_symbol_highlights() -> None:
    """An empty highlight (color reset back-to-back) or pure
    whitespace highlight isn't a meaningful annotation — drop it."""
    raw = "\x1b[48;5;105m \x1b[49m  rest of line"
    assert extract_highlights(raw) == []


def test_extract_highlights_returns_empty_when_no_bg_color() -> None:
    """Plain text — no background-color CSI — returns empty list."""
    assert extract_highlights("hello\nworld") == []


def test_to_lark_markdown_bright_background_color() -> None:
    """Bright bg codes (100–107) map to the same Lark palette as
    standard bg (40–47) via the -10 offset. Catches the regression
    where the offset was accidentally -10 in both branches AND
    the bright fg palette entries (90–97) were unreachable from
    bg parsing."""
    standard = to_lark_markdown("\x1b[44mhello\x1b[49m")
    bright = to_lark_markdown("\x1b[104mhello\x1b[49m")
    assert "<font color='blue'>" in standard
    assert "<font color='blue'>" in bright


def test_to_lark_markdown_balances_open_and_close_tags() -> None:
    """A styled span emits exactly one open / close pair — no
    orphan `</font>**` trailing after the span. Regression test
    for the bug where the outer loop appended `close_tags()` on
    every style transition, duplicating closes already emitted
    by `_render_chunk`."""
    out = to_lark_markdown("\x1b[44mhello\x1b[49m world")
    assert out.count("<font") == 1
    assert out.count("</font>") == 1
    # Bold pair count matches: opening + closing for the styled span.
    assert out.count("**") == 2


def test_to_lark_markdown_no_orphan_close_when_chunk_is_empty() -> None:
    """Back-to-back SGR sequences (no text between them) used to
    leak orphan close tags because the outer loop appended close
    tags even when no opening tags were emitted for the empty
    chunk. With the fix, the empty chunk emits nothing and the
    eventual closing fires via `_render_chunk` on the next non-
    empty chunk."""
    # Empty styled span followed by plain text.
    out = to_lark_markdown("\x1b[44m\x1b[49mhello")
    assert "</font>" not in out
    assert "hello" in out


def test_to_lark_markdown_handles_multi_tab_highlight() -> None:
    """Smoke-tests the canonical Claude Code multi-tab footer: a
    background-color run wrapping ' ☒ Audience ' surrounded by
    other unstyled tabs. The active tab should come out wrapped
    in bold + font tags so it stands out."""
    raw = (
        "←  \x1b[48;5;105m ☒ Audience \x1b[49m  ☐ Edit scope  ☐ Save mode  ☐ UX model  ✔ Submit  →"
    )
    out = to_lark_markdown(raw)
    # The highlighted segment shows up wrapped; the rest is plain.
    assert "**<font color='purple'>" in out or "**<font color='" in out
    assert "Audience" in out
    assert "Edit scope" in out
    assert "Submit" in out
