"""markdown_safe — fence-balancing, safe clipping, literal wrapping."""

from __future__ import annotations

from paige.infrastructure.markdown_safe import (
    close_unbalanced_fence,
    demote_headings,
    fenced,
    inline_safe,
    literal_md,
    safe_clip,
)


def test_inline_safe_collapses_newlines() -> None:
    # A newline could otherwise start a `# heading` on the next line.
    assert inline_safe("refactor\n# not a heading") == "refactor # not a heading"


def test_inline_safe_drops_backticks() -> None:
    # An odd backtick would open an inline-code span that bleeds.
    assert inline_safe("refactor `foo") == "refactor foo"


def test_inline_safe_collapses_whitespace_runs() -> None:
    assert inline_safe("a   b\t c") == "a b c"


def test_inline_safe_leaves_plain_text() -> None:
    assert inline_safe("find the auth middleware") == "find the auth middleware"


def test_close_fence_noop_on_balanced() -> None:
    text = "```\ncode\n```"
    assert close_unbalanced_fence(text) == text


def test_close_fence_noop_on_plain_text() -> None:
    assert close_unbalanced_fence("just prose") == "just prose"


def test_close_fence_appends_when_open() -> None:
    assert close_unbalanced_fence("```\ncode") == "```\ncode\n```"


def test_safe_clip_short_text_untouched() -> None:
    assert safe_clip("hello", 100) == "hello"


def test_safe_clip_appends_marker() -> None:
    out = safe_clip("abcdefghij", 5)
    assert out == "abcde…"


def test_safe_clip_closes_severed_fence() -> None:
    # A fence opens before the cut point but its closing fence is past it.
    text = "intro\n```python\nprint('hello world, this is long')\n```\noutro"
    out = safe_clip(text, 20)
    # The result must have balanced fences so it can't leak.
    assert out.count("```") % 2 == 0
    assert out.endswith("…")


def test_fenced_plain_uses_triple() -> None:
    assert fenced("ls -la", "bash") == "```bash\nls -la\n```"


def test_fenced_escalates_past_inner_fence() -> None:
    # Content contains a triple fence — wrapper must be longer so the
    # inner fence can't close it.
    content = "before\n```\ninner\n```\nafter"
    out = fenced(content)
    assert out.startswith("````")  # 4 backticks, longer than the inner 3
    assert out.endswith("````")
    # The original content is preserved verbatim inside.
    assert content in out


def test_fenced_escalates_past_long_run() -> None:
    out = fenced("a ```` b")  # inner run of 4
    assert out.startswith("`````")  # 5 backticks


def test_literal_inline_for_simple_single_line() -> None:
    assert literal_md('{"command": "ls"}') == '`{"command": "ls"}`'


def test_literal_fenced_for_multiline() -> None:
    out = literal_md("line1\nline2")
    assert out == "```\nline1\nline2\n```"


def test_literal_fenced_when_backticks_present() -> None:
    out = literal_md("has `code` inline")
    assert out.startswith("```")


def test_literal_empty_returns_empty() -> None:
    assert literal_md("   ") == ""


def test_literal_clips_before_wrapping() -> None:
    out = literal_md("x" * 100, limit=10)
    assert out == "`" + "x" * 9 + "…`"


def test_demote_h1_to_bold() -> None:
    assert demote_headings("# /should-start-new-session — Decide") == (
        "**/should-start-new-session — Decide**"
    )


def test_demote_all_heading_levels() -> None:
    src = "## Two\n### Three\n###### Six"
    assert demote_headings(src) == "**Two**\n**Three**\n**Six**"


def test_demote_leaves_prose_and_inline_hash() -> None:
    src = "see issue #42 here\nplain line"
    assert demote_headings(src) == src


def test_demote_skips_hash_inside_fence() -> None:
    src = "# Title\n```bash\n# a shell comment\nls\n```\n# After"
    out = demote_headings(src)
    assert out == "**Title**\n```bash\n# a shell comment\nls\n```\n**After**"


def test_demote_noop_without_hash() -> None:
    assert demote_headings("no headings here") == "no headings here"


def test_literal_clip_then_fence_stays_balanced() -> None:
    # Multi-line content clipped mid-fence still wraps safely: the outer
    # fence escalates past the severed inner ``` so it can't leak.
    content = "```\n" + "y" * 100
    out = literal_md(content, limit=20)
    assert out.startswith("````")
    assert out.endswith("````")
