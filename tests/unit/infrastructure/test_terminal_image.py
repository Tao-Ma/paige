"""terminal_image — text → PNG smoke tests.

We don't pixel-diff (font rendering varies across PIL/freetype
versions); we assert on the byte-level shape (PNG magic, dimensions
that scale with the input) and that the bundled font is found.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from paige.infrastructure.terminal_image import render

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def test_render_returns_png_bytes() -> None:
    out = render("hello")
    assert out.startswith(PNG_MAGIC)


def test_render_handles_empty_string() -> None:
    # Empty input should still produce a (small) valid PNG, not crash.
    out = render("")
    assert out.startswith(PNG_MAGIC)
    img = _open(out)
    # One "line" of padding-sized whitespace, even for empty text.
    assert img.size[0] > 0
    assert img.size[1] > 0


def test_render_height_scales_with_lines() -> None:
    one_line = render("just one line")
    five_lines = render("\n".join(["line"] * 5))
    assert _open(five_lines).size[1] > _open(one_line).size[1]


def test_render_width_scales_with_longest_line() -> None:
    short = render("ab")
    wide = render("abcdefghijklmnopqrstuvwxyz" * 4)
    assert _open(wide).size[0] > _open(short).size[0]


@pytest.mark.parametrize("font_size", [12, 28, 48])
def test_render_respects_font_size(font_size: int) -> None:
    small = render("X", font_size=12)
    sized = render("X", font_size=font_size)
    if font_size == 12:
        assert _open(sized).size == _open(small).size
    else:
        assert _open(sized).size[1] > _open(small).size[1]


def test_render_dark_background() -> None:
    """Top-left padding pixel should be the dark theme background."""
    img = _open(render("text"))
    assert img.getpixel((0, 0)) == (30, 30, 30)
