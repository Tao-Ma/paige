"""Markdown-safe truncation + literal-rendering helpers.

Card bodies are rendered as Lark-native markdown (`tag: markdown`).
Two failure modes show up when arbitrary text (assistant prose, tool
arguments, tool output, captured transcripts) flows into a card body:

1. **Severed fences.** A hard char-count clip can cut a fenced code
   block (```` ``` ````) before its closing fence. The card renderer
   then treats every following element as part of the open block — one
   truncated code block swallows the rest of the card. `safe_clip`
   closes any fence the cut left open.

2. **Raw text misread as markdown.** Tool arguments / output aren't
   markdown, but stray `*`, `_`, backticks, or a bare `[N]` (the Lark
   link-reference quirk) render as formatting. `literal_md` wraps such
   text so it renders verbatim.

Pure, no I/O, no SDK dependency.
"""

from __future__ import annotations

import re

_FENCE_RUN = re.compile(r"`+")
_ATX_HEADING = re.compile(r"^[ \t]*(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def demote_headings(text: str) -> str:
    """Turn ATX markdown headings (`# Title` … `###### Title`) into
    bold lines (`**Title**`), outside fenced code blocks.

    Transcript prose — especially skill bodies injected after a slash
    command — often opens with a big `# Heading`, which renders as an
    oversized header line dominating a packed history card. Bold keeps
    the emphasis without the size. A `#` inside a ```` ``` ```` fence is
    a code comment, not a heading, so fenced regions are left alone.
    """
    if "#" not in text:
        return text
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else _ATX_HEADING.sub(r"**\2**", line))
    return "\n".join(out)


def inline_safe(text: str) -> str:
    """Flatten free text for use as an inline label inside a card line
    (e.g. an agent description or a task subject that sits next to other
    markup on the same line).

    Collapses all whitespace — including newlines — to single spaces so
    a stray newline can't start a `# heading`, and drops backticks so an
    odd one can't open an inline-code span that bleeds across the whole
    card body. Other inline markers (`*`, `_`) are left: at worst they
    render as stray emphasis, which is cosmetic, not card-breaking.
    """
    return " ".join(text.split()).replace("`", "")


def close_unbalanced_fence(text: str) -> str:
    """If `text` contains an odd number of ```` ``` ```` fence markers,
    append a closing fence on its own line. Idempotent on balanced
    input. This is the minimum needed to stop a severed/embedded code
    block from leaking into following card content.
    """
    if text.count("```") % 2 == 1:
        return text + "\n```"
    return text


def safe_clip(text: str, limit: int, marker: str = "…") -> str:
    """Clip `text` to `limit` characters, then close any code fence the
    cut left open and append `marker`. Use for markdown-intended text
    (prose, assistant replies) — markdown structure is preserved, only
    a dangling fence is repaired.
    """
    if len(text) <= limit:
        return text
    return close_unbalanced_fence(text[:limit]) + marker


def fenced(content: str, lang: str = "") -> str:
    """Wrap `content` in a fenced code block whose fence is longer than
    any backtick run inside `content`, so the content can never close
    the wrapper early (CommonMark variable-length-fence rule). Normal
    content with no backtick runs uses a plain triple fence — identical
    to a hand-written ```` ``` ```` block, so no rendering risk for the
    common case; the escalation only kicks in for adversarial content
    (e.g. editing a Markdown file that itself contains fences).
    """
    longest = max((len(m.group()) for m in _FENCE_RUN.finditer(content)), default=0)
    bar = "`" * max(3, longest + 1)
    return f"{bar}{lang}\n{content}\n{bar}"


def literal_md(content: str, *, limit: int | None = None) -> str:
    """Render `content` so it shows verbatim in a markdown body.

    Single-line, backtick-free content becomes an inline code span
    (compact); anything multi-line or containing backticks gets a
    `fenced` block (which neutralises inner fences). Empty content
    returns an empty string so callers can drop it.

    `limit`, when set, clips the *raw* content first — clipping before
    wrapping is safe because `fenced` / inline-code re-establish
    balance regardless of where the cut landed.
    """
    content = content.strip()
    if limit is not None and len(content) > limit:
        content = content[: limit - 1] + "…"
    if not content:
        return ""
    if "\n" not in content and "`" not in content:
        return f"`{content}`"
    return fenced(content)


__all__ = [
    "close_unbalanced_fence",
    "demote_headings",
    "fenced",
    "inline_safe",
    "literal_md",
    "safe_clip",
]
