"""Storage — atomic key-value persistence for state files.

Each `key` maps to one file on disk (the impl chooses the path).
Values are arbitrary JSON-serializable dicts. Writes are atomic
(write to temp + rename) so a crash mid-write never leaves a
corrupt state file — readers see either the old or new full
contents.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Storage(Protocol):
    """Atomic JSON key-value storage."""

    async def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored dict for `key`, or None if absent."""
        ...

    async def save(self, key: str, value: dict[str, Any]) -> None:
        """Atomically replace `key`'s contents with `value`."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the stored dict for `key`. No-op if absent."""
        ...
