"""Render terminal text to a PNG.

Plain text only — ANSI escape parsing is deferred polish. A single
monospace font (JetBrains Mono, OFL-1.1) ships with the wheel so the
output is identical regardless of the host's installed fonts.
Glyphs the font lacks (CJK, exotic symbols) render as Pillow's
default fallback box; users who need full coverage can install
NotoSansMonoCJK / Symbola separately and we'll re-add a fallback
chain if it turns out to matter in practice.

Public surface: `render(text, *, font_size=14) -> bytes` — synchronous
PIL work; callers should `await asyncio.to_thread(render, ...)` to
keep the event loop free.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import ImageFont

logger = logging.getLogger(__name__)

_FONT_PATH = Path(__file__).parent / "fonts" / "JetBrainsMono-Regular.ttf"

_BG = (30, 30, 30)
_FG = (212, 212, 212)
_PADDING = 16


def render(text: str, *, font_size: int = 28) -> bytes:
    """Render `text` onto a dark background. Returns PNG bytes.

    Pillow is imported lazily so the `[screenshot]` extra stays
    truly optional — `paige` installs without it, and the cost is
    only paid the first time `/screenshot` runs.
    """
    from PIL import Image, ImageDraw

    font = _load_font(font_size)
    lines = text.split("\n")
    line_height = int(font_size * 1.4)

    dummy = Image.new("RGB", (1, 1))
    measure = ImageDraw.Draw(dummy)
    max_width = 0
    for line in lines:
        bbox = measure.textbbox((0, 0), line, font=font)
        max_width = max(max_width, bbox[2] - bbox[0])

    img_width = int(max_width) + _PADDING * 2
    img_height = line_height * max(len(lines), 1) + _PADDING * 2
    img = Image.new("RGB", (img_width, img_height), _BG)
    draw = ImageDraw.Draw(img)

    y = _PADDING
    for line in lines:
        draw.text((_PADDING, y), line, fill=_FG, font=font)
        y += line_height

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(_FONT_PATH), size)
    except OSError:
        # Bundled font missing or unreadable — fall back to the
        # default bitmap font so we still produce *something*. This
        # only happens in an actively broken install (deleted artifact
        # or pyproject.toml missing the fonts artifacts entry).
        logger.warning("Bundled font %s unreadable; using PIL default", _FONT_PATH)
        return ImageFont.load_default()


__all__ = ["render"]
