"""Convert ANSI-escaped terminal text into Lark-flavoured markdown.

Tmux's `capture-pane -e` returns the visible pane content with CSI
escape sequences preserved. Plain rendering inside a code block
strips them, which loses Claude Code's TUI cues: highlighted tabs
(background-color CSI), selected options (foreground-color CSI),
emphasized labels (bold). This module turns those styled runs into
Lark markdown so the styling survives into the chat surface.

The output is **not** wrapped in a code block — Lark renders
markdown inside backticked code blocks literally, so we'd lose
the conversion. Instead the renderer emits a stream of inline
markdown with explicit `<br>` line breaks; alignment-sensitive
monospace TUI layout is approximated via leading-space rendering.

Mapping rules:

- **Background color** (CSI `48;…m`) → wrap the run in `**bold**`
  AND a `<font color='red'>` tag. Lark's markdown dialect has no
  background-color element, so we surface the highlighted region
  with the most visible combination available.
- **Foreground color** (CSI `38;…m` or `3X`, `9X`) → `<font color>`
  with the closest named color. Indices outside the standard 8/16
  fall back to the dimmest matching named color.
- **Bold** (CSI `1m`) → `**bold**`.
- **Reset** (CSI `0m`, `39m`, `49m`, `22m`) → close the currently
  open inline span.
- All other CSI/OSC/ESC sequences are dropped (cursor moves,
  alternate screen, etc. — they don't affect static text).

Designed for paige's `/livepane`, but pure enough to live in
`infrastructure` and be reusable by any future surface that wants
to render terminal text into Lark markup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Match an ESC `[` … `m` (SGR — Select Graphic Rendition). The
# parameter list is a `;`-separated string of digits; final byte
# is `m`. We deliberately don't match other CSI commands (cursor
# moves, clear-screen, etc.) — those are emitted alongside SGR
# during a tmux redraw and we just want to strip them.
_CSI_SGR = re.compile(r"\x1b\[([\d;]*)m")
# Any other CSI sequence — drop without interpreting.
_CSI_OTHER = re.compile(r"\x1b\[[\d;?]*[ABCDEFGHJKSTfsulnh]")
# OSC sequences (title, hyperlink). Drop entirely.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")

# Standard 8-color palette → Lark `<font color>` named values.
# Lark only supports a small set of named colors in card markdown;
# everything else maps to the closest neighbor. Background colors
# share the same palette — Lark can't render the bg directly, so
# we surface bg-highlighted runs with both `<font>` AND `**` so
# they stand out under any client theme.
_PALETTE: dict[int, str] = {
    30: "grey",
    31: "red",
    32: "green",
    33: "yellow",
    34: "blue",
    35: "purple",
    36: "wathet",
    37: "grey",
    # Bright variants — same colors, treated identically.
    90: "grey",
    91: "red",
    92: "green",
    93: "yellow",
    94: "blue",
    95: "purple",
    96: "wathet",
    97: "grey",
}


# 256-color → named color buckets. We only need rough mapping; the
# goal is "highlighted thing pops" not pixel-perfect color match.
def _color256_to_name(idx: int) -> str:
    if idx < 16:
        return _PALETTE.get(idx + 30 if idx < 8 else idx + 82, "grey")
    if idx < 232:
        # 6x6x6 color cube — pick the dominant channel.
        c = idx - 16
        r = (c // 36) % 6
        g = (c // 6) % 6
        b = c % 6
        if r > g and r > b:
            return "red" if r > 2 else "orange"
        if g > r and g > b:
            return "green"
        if b > r and b > g:
            return "blue"
        if r == g and r > b:
            return "yellow"
        if r == b and r > g:
            return "purple"
        if g == b and g > r:
            return "wathet"
        return "grey"
    # 232–255 — greyscale ramp. Map to grey.
    return "grey"


@dataclass
class _Style:
    """Mutable parser state for one SGR-driven run."""

    bold: bool = False
    fg: str | None = None  # named color
    bg: str | None = None  # named color

    def open_tags(self) -> str:
        """Markdown tags that open this style. Order matters — the
        close tags reverse it (LIFO)."""
        out: list[str] = []
        if self.bg is not None:
            # No native bg in Lark markdown — combine fg color +
            # bold so the run is unmistakable. The bg color we'd
            # have rendered becomes the fg color of the run.
            out.append("**")
            out.append(f"<font color='{self.bg}'>")
        elif self.fg is not None:
            out.append(f"<font color='{self.fg}'>")
        if self.bold and self.bg is None:
            out.append("**")
        return "".join(out)

    def close_tags(self) -> str:
        out: list[str] = []
        if self.bold and self.bg is None:
            out.append("**")
        if self.bg is not None:
            out.append("</font>")
            out.append("**")
        elif self.fg is not None:
            out.append("</font>")
        return "".join(out)

    def is_default(self) -> bool:
        return not self.bold and self.fg is None and self.bg is None


def _apply_sgr(style: _Style, params_str: str) -> _Style:
    """Apply one SGR parameter list to the running style, returning
    the next style. Empty params == `0` (reset)."""
    params = [int(p) for p in params_str.split(";") if p] or [0]
    next_style = _Style(bold=style.bold, fg=style.fg, bg=style.bg)
    i = 0
    while i < len(params):
        p = params[i]
        if p == 0:
            next_style = _Style()
        elif p == 1:
            next_style.bold = True
        elif p == 22:
            next_style.bold = False
        elif 30 <= p <= 37 or 90 <= p <= 97:
            next_style.fg = _PALETTE.get(p)
        elif p == 39:
            next_style.fg = None
        elif 40 <= p <= 47 or 100 <= p <= 107:
            # Map 4X/10X bg codes onto the same palette via -10
            # offset — the palette has both standard (30-37) and
            # bright (90-97) entries, both yielding the same Lark
            # color name, so a single shift covers both ranges.
            next_style.bg = _PALETTE.get(p - 10)
        elif p == 49:
            next_style.bg = None
        elif p == 38 and i + 2 < len(params) and params[i + 1] == 5:
            # 38;5;N — 256-color foreground.
            next_style.fg = _color256_to_name(params[i + 2])
            i += 2
        elif p == 48 and i + 2 < len(params) and params[i + 1] == 5:
            # 48;5;N — 256-color background.
            next_style.bg = _color256_to_name(params[i + 2])
            i += 2
        elif p == 38 and i + 4 < len(params) and params[i + 1] == 2:
            # 38;2;R;G;B — truecolor. Approximate by dominant channel.
            r, g, b = params[i + 2], params[i + 3], params[i + 4]
            next_style.fg = (
                "red" if r > g and r > b else "green" if g > b else "blue" if b > 0 else "grey"
            )
            i += 4
        elif p == 48 and i + 4 < len(params) and params[i + 1] == 2:
            r, g, b = params[i + 2], params[i + 3], params[i + 4]
            next_style.bg = (
                "red" if r > g and r > b else "green" if g > b else "blue" if b > 0 else "grey"
            )
            i += 4
        # else: drop unsupported codes silently
        i += 1
    return next_style


def _escape_md(text: str) -> str:
    """Minimal markdown escaping so literal `*`, `_`, backtick, and
    angle brackets in TUI content don't get interpreted by Lark's
    renderer. Whitespace is left alone — `<br>` insertion is the
    caller's job."""
    return (
        text.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def to_lark_markdown(text: str) -> str:
    """Convert an ANSI-escaped terminal capture into Lark-flavoured
    markdown. Returns markdown ready to drop into a `markdown` card
    body element; no surrounding code block.

    Whitespace is preserved exactly. Line breaks are explicit `<br>`
    (Lark's `markdown` element collapses lone `\\n`) so TUI layout
    survives a roundtrip; leading spaces are rendered as `&nbsp;`
    so the markdown parser doesn't trim them."""
    # Strip non-SGR CSI + OSC entirely — they don't affect text.
    cleaned = _CSI_OTHER.sub("", text)
    cleaned = _OSC.sub("", cleaned)

    out: list[str] = []
    style = _Style()
    cursor = 0
    for m in _CSI_SGR.finditer(cleaned):
        chunk = cleaned[cursor : m.start()]
        if chunk:
            # `_render_chunk` emits matched open + close tags around
            # the styled span itself, so we don't append another
            # close pass at the SGR transition (doing so produced
            # duplicate `</font>**` trails and orphan close tags
            # when chunks between SGRs were empty).
            out.append(_render_chunk(chunk, style))
        style = _apply_sgr(style, m.group(1))
        cursor = m.end()
    tail = cleaned[cursor:]
    if tail:
        out.append(_render_chunk(tail, style))
    return "".join(out)


def _render_chunk(text: str, style: _Style) -> str:
    """Render one stretch of same-style text into markdown:
    open tags + escaped + (for monospace alignment) `<br>` between
    lines + leading-space → `&nbsp;` conversion."""
    lines = text.split("\n")
    rendered_lines: list[str] = []
    for line in lines:
        # Preserve leading whitespace by converting to `&nbsp;` —
        # Lark's markdown otherwise collapses runs of spaces at the
        # start of a line.
        leading = len(line) - len(line.lstrip(" "))
        rest = line[leading:]
        rendered_lines.append("&nbsp;" * leading + _escape_md(rest))
    body = "<br>".join(rendered_lines)
    if style.is_default() or not text.strip():
        # Empty / whitespace-only spans don't need the tag wrap.
        return body
    return f"{style.open_tags()}{body}{style.close_tags()}"


def strip_ansi(text: str) -> str:
    """Drop every CSI / OSC sequence — returns plain text only.
    Helper for callers that want the plain-text path without going
    through markdown conversion."""
    text = _CSI_SGR.sub("", text)
    text = _CSI_OTHER.sub("", text)
    text = _OSC.sub("", text)
    return text


def extract_highlights(text: str) -> list[str]:
    """Return the labels of any background-color highlighted runs in
    `text` — Claude Code's active-tab indicator is the canonical
    case. The list keeps original order, with leading/trailing
    whitespace stripped and empty / pure-symbol runs dropped (so
    `☒` alone doesn't surface as a "highlight"). Lets a caller
    surface the highlight info without rendering the whole text as
    markdown — useful when the body is still rendered as a
    monospace code block (which would swallow the markdown wrap)."""
    cleaned = _CSI_OTHER.sub("", text)
    cleaned = _OSC.sub("", cleaned)
    out: list[str] = []
    style = _Style()
    cursor = 0
    current_run: list[str] = []
    for m in _CSI_SGR.finditer(cleaned):
        chunk = cleaned[cursor : m.start()]
        if style.bg is not None and chunk:
            current_run.append(chunk)
        next_style = _apply_sgr(style, m.group(1))
        # Closing a bg span — flush the run if it has substance.
        if style.bg is not None and next_style.bg is None:
            label = "".join(current_run).strip()
            if label and any(c.isalnum() for c in label):
                out.append(label)
            current_run = []
        style = next_style
        cursor = m.end()
    tail = cleaned[cursor:]
    if style.bg is not None and tail:
        current_run.append(tail)
        label = "".join(current_run).strip()
        if label and any(c.isalnum() for c in label):
            out.append(label)
    return out


__all__ = ["extract_highlights", "strip_ansi", "to_lark_markdown"]
