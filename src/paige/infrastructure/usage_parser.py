"""Parse Claude Code's `/usage` modal pane output.

The TUI shows a Settings overlay with a "Usage" tab — progress bars,
percentages, reset times. Capture happens after we send `/usage` +
Enter and wait long enough for the modal to render. We snip the
content between the `Settings: … Usage` header and the `Esc to …`
footer, strip Unicode block-drawing characters that progress bars
use, and return the cleaned lines.

Pure: no I/O, no state. Returns `None` when the header isn't found
(captured a non-modal pane) so callers can fall back to a raw dump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# U+2580–U+259F is the "Block Elements" range — what progress bars
# (`█▌▎` etc.) draw with. Strip leading runs to keep the percentage.
_LEADING_BLOCK_RUN = re.compile(r"^[▀-▟\s]+")


@dataclass(frozen=True)
class UsageInfo:
    """Cleaned content lines from the /usage modal."""

    lines: tuple[str, ...]


def parse_usage(pane_text: str) -> UsageInfo | None:
    """Extract the modal's content lines, or None if not visible."""
    if not pane_text:
        return None

    raw_lines = pane_text.strip().split("\n")
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if start_idx is None:
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(raw_lines)

    cleaned: list[str] = []
    for line in raw_lines[start_idx:end_idx]:
        stripped = line.strip()
        if not stripped:
            continue
        stripped = _LEADING_BLOCK_RUN.sub("", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if not cleaned:
        return None
    return UsageInfo(lines=tuple(cleaned))


__all__ = ["UsageInfo", "parse_usage"]
