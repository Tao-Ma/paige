"""parse_usage — Settings:Usage modal scraping."""

from __future__ import annotations

from paige.infrastructure.usage_parser import parse_usage


def test_returns_none_for_empty_pane() -> None:
    assert parse_usage("") is None


def test_returns_none_when_modal_not_visible() -> None:
    assert parse_usage("$ ls\nfile.txt\nREADME.md\n") is None


def test_extracts_modal_lines_between_header_and_footer() -> None:
    pane = "\n".join(
        [
            "Some preamble",
            "Settings: Usage",
            "Daily quota",
            "█████▋   38% used",
            "Resets in 6h 12m",
            "",
            "Esc to cancel",
            "trailing junk",
        ]
    )
    info = parse_usage(pane)
    assert info is not None
    assert info.lines == ("Daily quota", "38% used", "Resets in 6h 12m")


def test_returns_none_when_only_header_no_content() -> None:
    pane = "\n".join(["Settings: Usage", "  ", "Esc to cancel"])
    assert parse_usage(pane) is None


def test_handles_missing_footer_with_remaining_lines() -> None:
    # Footer scrolled off — we still emit content up to EOF.
    pane = "\n".join(["Settings: Usage", "Daily quota", "█▌  10% used"])
    info = parse_usage(pane)
    assert info is not None
    assert info.lines == ("Daily quota", "10% used")


def test_strips_block_chars_but_keeps_inline_percent() -> None:
    pane = "\n".join(["Settings: Usage", "███▌▎▏  17% used", "Esc to cancel"])
    info = parse_usage(pane)
    assert info is not None
    assert info.lines == ("17% used",)
