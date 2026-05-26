"""terminal_parser — pure pane snapshot → PaneStatus."""

from __future__ import annotations

from paige.infrastructure.terminal_parser import (
    PaneStatus,
    extract_interactive_content,
    extract_options,
    extract_prompt_suggestion,
    is_interactive_ui,
    parse_status,
)


def test_empty_pane_idle() -> None:
    assert parse_status("") == PaneStatus(spinner=False)


def test_no_chrome_idle() -> None:
    """A pane without ─── separators has no status line to find."""
    pane = "just some text\nmore text\n"
    assert parse_status(pane) == PaneStatus(spinner=False)


def test_basic_thinking_with_chrome() -> None:
    pane = "\n".join(
        [
            "previous output",
            "✻ Thinking… (12s · 4.1k tokens)",
            "─────────────────────────────────",
            "> ",
        ]
    )
    out = parse_status(pane)
    assert out.spinner is True
    assert out.text is not None
    assert "Thinking" in out.text


def test_two_chrome_separators_finds_spinner() -> None:
    """Claude 2.x sometimes renders two chrome lines around the prompt."""
    pane = "\n".join(
        [
            "✻ Working on it…",
            "─────────────────────────────────",
            "> typed input",
            "─────────────────────────────────",
        ]
    )
    out = parse_status(pane)
    assert out.spinner is True


def test_skips_progress_hints() -> None:
    """`⎿` lines are tool progress hints; spinner is the next non-hint
    line above them."""
    pane = "\n".join(
        [
            "✶ Thinking… (3s)",
            "  ⎿ Reading file.txt",
            "  ⎿ 5kb read",
            "─────────────────────────────────",
            "> ",
        ]
    )
    out = parse_status(pane)
    assert out.spinner is True
    assert out.text is not None and "Thinking" in out.text


def test_idle_when_no_spinner_above_chrome() -> None:
    """Prose above the chrome but no spinner glyph means idle."""
    pane = "\n".join(
        [
            "Done. Final answer here.",
            "─────────────────────────────────",
            "> ",
        ]
    )
    assert parse_status(pane).spinner is False


def test_recognizes_all_spinner_glyphs() -> None:
    for glyph in ("·", "✻", "✽", "✶", "✳", "✢"):
        pane = f"{glyph} Thinking…\n─────\n> "
        assert parse_status(pane).spinner is True, f"glyph {glyph!r} not detected"


def test_spinner_too_far_from_chrome_is_ignored() -> None:
    """If the spinner is buried behind 4+ non-hint useful lines above
    the chrome, it's prose containing a spinner-shaped char, not a
    status line."""
    pane = "\n".join(
        [
            "✻ this is just prose mentioning a spinner char",
            "line 2",
            "line 3",
            "line 4",
            "line 5 (this is line 5 — too far from chrome)",
            "─────────────────────────────────",
        ]
    )
    assert parse_status(pane).spinner is False


def test_extracts_status_text_after_spinner() -> None:
    pane = "\n".join(
        [
            "✻ Crafting (45s · 12.3k tokens)",
            "─────────────────────────────────",
        ]
    )
    out = parse_status(pane)
    assert out.spinner is True
    assert out.text == "Crafting (45s · 12.3k tokens)"


def test_spinner_only_no_text() -> None:
    pane = "✻\n─────\n> "
    out = parse_status(pane)
    assert out.spinner is True
    assert out.text is None


def test_blank_lines_above_chrome_are_skipped() -> None:
    pane = "\n".join(
        [
            "✻ Thinking…",
            "",
            "",
            "─────────────────────────────────",
        ]
    )
    out = parse_status(pane)
    assert out.spinner is True


def test_indented_spinner_is_recognized() -> None:
    """Some Claude Code TUIs indent the status line."""
    pane = "\n".join(
        [
            "    ✻ Thinking… (3s)",
            "─────────────────────────────────",
        ]
    )
    out = parse_status(pane)
    assert out.spinner is True
    assert out.text is not None and "Thinking" in out.text


def test_chrome_far_above_visible_window_ignored() -> None:
    """If chrome is beyond the lookback window (15 lines), treat as
    idle — we're scrolled past the status area."""
    lines = ["─" * 30] + ["scrollback line"] * 20
    pane = "\n".join(lines)
    assert parse_status(pane).spinner is False


def test_chrome_with_padding_is_recognized() -> None:
    """A chrome line surrounded by whitespace still counts."""
    pane = "\n".join(["✻ Thinking…", "   ─────────────────   ", "> "])
    out = parse_status(pane)
    assert out.spinner is True


# ── Interactive UI detection ────────────────────────────────────────


def test_no_interactive_ui_in_idle_pane() -> None:
    pane = "Just regular output\nNo overlay here\n"
    assert extract_interactive_content(pane) is None
    assert is_interactive_ui(pane) is False


def test_extract_bash_approval() -> None:
    pane = "\n".join(
        [
            "previous output",
            "Bash command",
            "  $ rm -rf /tmp/cache",
            "Do you want to allow this?",
            "❯ 1. Yes",
            "  2. Yes, and don't ask again for `rm` commands",
            "  3. No",
            "Esc to cancel",
        ]
    )
    ui = extract_interactive_content(pane)
    assert ui is not None
    assert ui.name == "BashApproval"
    assert "rm -rf" in ui.content


def test_extract_permission_prompt() -> None:
    pane = "\n".join(
        [
            "Do you want to make this edit to config.yaml?",
            "❯ 1. Yes",
            "  2. Yes, allow all edits during this session",
            "  3. No",
            "Esc to cancel",
        ]
    )
    ui = extract_interactive_content(pane)
    assert ui is not None
    assert ui.name == "PermissionPrompt"


def test_extract_exit_plan_mode() -> None:
    pane = "\n".join(
        [
            "Claude has written up a plan",
            "step 1",
            "step 2",
            "Would you like to proceed?",
            "❯ 1. Yes",
            "  2. No",
            "Esc to cancel",
        ]
    )
    ui = extract_interactive_content(pane)
    assert ui is not None
    assert ui.name == "ExitPlanMode"


def test_extract_options_from_numbered_menu() -> None:
    content = "\n".join(
        [
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. Yes, and remember",
            "  3. No",
        ]
    )
    opts = extract_options(content)
    assert opts == [
        (1, "Yes"),
        (2, "Yes, and remember"),
        (3, "No"),
    ]


def test_extract_options_returns_empty_for_text_input_ui() -> None:
    content = "Type your reply here:\n> "
    assert extract_options(content) == []


def test_is_interactive_ui_true_when_pattern_matches() -> None:
    pane = "\n".join(
        [
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. No",
            "  3. No (cancel)",
            "Esc to cancel",
        ]
    )
    assert is_interactive_ui(pane) is True


def test_long_separators_shortened_in_extracted_content() -> None:
    """The capture's `─` runs of 5+ chars get squashed to ─────
    so mobile cards don't horizontally scroll."""
    pane = "\n".join(
        [
            "Do you want to proceed?",
            "─" * 80,
            "❯ 1. Yes",
            "  2. No",
            "  3. No (cancel)",
            "Esc to cancel",
        ]
    )
    ui = extract_interactive_content(pane)
    assert ui is not None
    assert "─" * 80 not in ui.content
    assert "─────" in ui.content


# ── extract_prompt_suggestion ───────────────────────────────────────
#
# Fixtures are the three real prompt-line captures from a live pane
# (see the module docstring in terminal_parser). ESC is \x1b; the
# chrome separators and a spinner line are included so the
# bottom-up prompt-line search runs against a realistic shape.

_CHROME = "\x1b[38;5;246m" + "─" * 60


def _pane(prompt_line: str) -> str:
    return "\n".join(
        [
            "  …and so on. Want me to commit & push as one or split it?",
            "",
            "\x1b[38;5;241m✻\x1b[39m \x1b[38;5;241mSautéed for 3m 9s\x1b[39m",
            "",
            _CHROME,
            prompt_line,
            _CHROME,
            "\x1b[39m  \x1b[38;5;241m? for shortcuts\x1b[39m",
        ]
    )


def test_ghost_suggestion_extracted() -> None:
    """Faint run after the marker → the ghost text, cursor char included."""
    pane = _pane("\x1b[39m❯ \x1b[7ml\x1b[0;2meave it, commit & push as one commit\x1b[0m")
    assert extract_prompt_suggestion(pane) == "leave it, commit & push as one commit"


def test_typed_text_is_not_a_ghost() -> None:
    """Default-fg typed text (no faint) must not be read as a suggestion."""
    pane = _pane("\x1b[39m❯ update lazytui\x1b[7m \x1b[0m")
    assert extract_prompt_suggestion(pane) is None


def test_empty_prompt_has_no_ghost() -> None:
    """Bare prompt — cursor block on a space, nothing faint."""
    pane = _pane("\x1b[39m❯ \x1b[7m \x1b[0m")
    assert extract_prompt_suggestion(pane) is None


def test_empty_input_returns_none() -> None:
    assert extract_prompt_suggestion("") is None


def test_no_prompt_line_returns_none() -> None:
    assert extract_prompt_suggestion("just some assistant prose\nwith no prompt\n") is None


def test_extended_color_index_two_not_mistaken_for_faint() -> None:
    """A 256-color index of 2 (`38;5;2`) is colour, not the faint
    attribute — typed text wearing it is still not a ghost."""
    pane = _pane("\x1b[39m❯ \x1b[38;5;2mgreen typed text\x1b[7m \x1b[0m")
    assert extract_prompt_suggestion(pane) is None
