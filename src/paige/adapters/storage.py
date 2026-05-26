"""FileStorage — atomic JSON file storage.

Each key maps to one file under `root_dir`. Writes are atomic:
write to a sibling `<name>.tmp` and `os.replace()` it onto the
target. A crash mid-write leaves either the previous full file
or no temp file (cleanup) — never a half-written state file.

Filenames are constructed from `key` after stripping path
separators (so a key like `bindings` → `bindings.json` directly).
Keys with `/` are not allowed; the storage is flat, not nested.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, cast


class FileStorage:
    """Atomic JSON key-value storage on disk.

    Implements `paige.ports.storage.Storage`.
    """

    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if "/" in key or "\\" in key or ".." in key or not key:
            raise ValueError(f"invalid storage key: {key!r}")
        return self._root / f"{key}.json"

    async def load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        return await asyncio.to_thread(self._load_sync, path)

    @staticmethod
    def _load_sync(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8") as f:
                data: object = json.load(f)
        except FileNotFoundError:
            return None
        if not isinstance(data, dict):
            raise ValueError(f"storage value at {path} is not a JSON object")
        # JSON object keys are always strings; values are arbitrary JSON.
        # Pyright can't narrow `dict` to its type params from isinstance,
        # so cast at the boundary.
        return cast("dict[str, Any]", data)

    async def save(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        await asyncio.to_thread(self._save_sync, path, value)

    @staticmethod
    def _save_sync(path: Path, value: dict[str, Any]) -> None:
        # Unique-per-save temp suffix so two concurrent saves to the
        # same key don't collide on `<key>.json.tmp`. The first to
        # `os.replace()` wins; the loser's rename target already
        # exists (replace overwrites atomically).
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(self._delete_sync, path)

    @staticmethod
    def _delete_sync(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
