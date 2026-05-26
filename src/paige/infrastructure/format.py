"""Human-readable byte/duration formatters.

Pure, no I/O. Used by `/server` rendering today; lifted to its own
module so future commands can reuse without dragging server-card
imports along.
"""

from __future__ import annotations


def format_bytes(n: int | None) -> str:
    """Compact size with binary-prefix units. None → '—'."""
    if n is None:
        return "—"
    size: float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    # Unreachable — the loop returns on its last iteration.
    return f"{size} ?"


def format_duration(secs: float) -> str:
    """Compact uptime: '5s', '12m 30s', '6h 12m', '3d 4h'."""
    s = int(max(0, secs))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


__all__ = ["format_bytes", "format_duration"]
