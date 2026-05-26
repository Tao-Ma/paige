"""text → Feishu `post` envelope (pure renderer).

Feishu's `post` message type is a tree-shaped rich text format. The
JSON payload has language-keyed dicts of {title, content}, where
`content` is a list of paragraphs and each paragraph is a list of
inline element dicts:

    {"zh_cn": {
       "title": "",
       "content": [
         [{"tag": "text", "text": "hello "},
          {"tag": "a",    "text": "link", "href": "https://..."}],
         [{"tag": "text", "text": ""}],          # blank line
       ]
    }}

This module converts paige's neutral text dialect (same shape
`render_block` produces — paragraphs split by blank lines, plus
inline `*bold*` / `**bold**` / `_italic_` / `` `code` `` / links /
fenced code blocks / # headings) into that structure.

Pure, stateless, no I/O, no SDK dep. The channel adapter feeds
the output dict to `im.v1.message.create`'s `content` field after
`json.dumps`.

Subset rendered:
    paragraphs (blank-line split)
    headings #/##/### → bold (Feishu post has no native heading tag)
    inline bold **X** and *X*
    inline italic _X_
    inline code `X`
    links [text](url)
    fenced code blocks ``` ... ```

Deliberately skipped:
    nested lists, tables — out of scope; the upstream renderer
    produces flat content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Inline patterns. Order in `_inline_specs` matters: links bind first
# (their delimiters can't nest), then **double** before *single*
# bold, then italic, then code. The overlap check below ensures inner
# matches inside an outer match are skipped.

# Links: [text](url). url cannot contain ).
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Bold: **X** (CommonMark) and *X* (single-star dialect). The
# single-star variant matched after **double** so the double's inner
# `*X*` doesn't double-match. First + last captured char must be
# non-whitespace to avoid matching arithmetic like `2*3 = 6*4`.
_BOLD_DBL_RE = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_SGL_RE = re.compile(r"\*(\S(?:[^*\n]*\S)?)\*")

# Italic: _X_ (avoid matching identifiers like `snake_case_var` —
# require non-word boundary outside, non-whitespace inside ends).
_ITALIC_RE = re.compile(r"(?<![A-Za-z0-9_])_(\S(?:[^_\n]*\S)?)_(?![A-Za-z0-9_])")

# Inline code: `X`. Backticks delimit; X cannot itself contain a backtick.
_CODE_RE = re.compile(r"`([^`]+)`")


# Type aliases for clarity at the read site.
Element = dict[str, Any]
Paragraph = list[Element]
Post = dict[str, Any]


@dataclass(frozen=True)
class _InlineSpec:
    """One pattern's contribution: a regex + a function that turns
    a match into a Feishu element dict."""

    pattern: re.Pattern[str]
    build: Any  # Callable[[re.Match[str]], Element] — pyright wants Any


def _build_link(m: re.Match[str]) -> Element:
    return {"tag": "a", "text": m.group(1), "href": m.group(2)}


def _build_bold(m: re.Match[str]) -> Element:
    return {"tag": "text", "text": m.group(1), "style": ["bold"]}


def _build_italic(m: re.Match[str]) -> Element:
    return {"tag": "text", "text": m.group(1), "style": ["italic"]}


def _build_code(m: re.Match[str]) -> Element:
    return {"tag": "text", "text": m.group(1), "style": ["code_inline"]}


# Highest-priority pattern first; later overlaps with earlier are dropped.
_INLINE_SPECS: tuple[_InlineSpec, ...] = (
    _InlineSpec(_LINK_RE, _build_link),
    _InlineSpec(_BOLD_DBL_RE, _build_bold),
    _InlineSpec(_BOLD_SGL_RE, _build_bold),
    _InlineSpec(_ITALIC_RE, _build_italic),
    _InlineSpec(_CODE_RE, _build_code),
)


# ── public surface ──────────────────────────────────────────────


def to_post(text: str, *, title: str = "") -> Post:
    """Wrap a body in the Feishu post envelope."""
    return {"zh_cn": {"title": title, "content": _to_paragraphs(text)}}


def to_post_json(text: str, *, title: str = "") -> str:
    """JSON-encoded `to_post(...)` for direct use in
    `im.v1.message.*` `content` fields."""
    return json.dumps(to_post(text, title=title), ensure_ascii=False)


# ── block-level parsing ─────────────────────────────────────────


def _to_paragraphs(body: str) -> list[Paragraph]:
    """Split `body` on lines, group into paragraphs + fenced code
    blocks, render each as a list of Feishu elements."""
    paragraphs: list[Paragraph] = []
    in_code = False
    code_buf: list[str] = []

    for line in body.split("\n"):
        stripped = line.lstrip()

        if stripped.startswith("```"):
            if in_code:
                paragraphs.append(_code_block_paragraph(code_buf))
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_buf.append(line)
            continue

        if not line.strip():
            paragraphs.append([{"tag": "text", "text": ""}])
            continue

        heading = _try_heading(stripped)
        if heading is not None:
            paragraphs.append(heading)
        else:
            paragraphs.append(_parse_inline(line))

    # Unclosed fence: render what we collected anyway. Better than
    # silently swallowing content.
    if in_code and code_buf:
        paragraphs.append(_code_block_paragraph(code_buf))

    if not paragraphs:
        paragraphs.append([{"tag": "text", "text": ""}])
    return paragraphs


def _code_block_paragraph(lines: list[str]) -> Paragraph:
    return [
        {
            "tag": "text",
            "text": "\n".join(lines),
            "style": ["code_block"],
        }
    ]


def _try_heading(stripped: str) -> Paragraph | None:
    """`#`, `##`, `###` headings → single bold paragraph. Feishu
    post has no native heading; bold is the closest visible
    equivalent."""
    for prefix in ("### ", "## ", "# "):
        if stripped.startswith(prefix):
            return [
                {
                    "tag": "text",
                    "text": stripped[len(prefix) :],
                    "style": ["bold"],
                }
            ]
    return None


# ── inline parsing ──────────────────────────────────────────────


def _parse_inline(line: str) -> Paragraph:
    """One line → list of Feishu inline elements.

    Strategy: collect `(start, end, element)` for every pattern's
    matches in priority order, drop overlaps with already-claimed
    spans, sort by position, then weave plain text between matches.
    """
    spans: list[tuple[int, int, Element]] = []
    for spec in _INLINE_SPECS:
        for m in spec.pattern.finditer(line):
            if not _overlaps(m.start(), m.end(), spans):
                spans.append((m.start(), m.end(), spec.build(m)))

    if not spans:
        return [{"tag": "text", "text": line}]

    spans.sort(key=lambda s: s[0])

    out: Paragraph = []
    cursor = 0
    for start, end, elem in spans:
        if start > cursor:
            plain = line[cursor:start]
            if plain:
                out.append({"tag": "text", "text": plain})
        out.append(elem)
        cursor = end
    if cursor < len(line):
        trailing = line[cursor:]
        if trailing:
            out.append({"tag": "text", "text": trailing})
    return out


def _overlaps(start: int, end: int, spans: list[tuple[int, int, Element]]) -> bool:
    """Half-open [start, end) overlap test against existing spans."""
    return any(start < e and s < end for s, e, _ in spans)


__all__ = ["Element", "Paragraph", "Post", "to_post", "to_post_json"]
