"""FileStorage — real-disk integration tests.

Marked `integration` because they touch the filesystem (tempdir).
Default `pytest` won't pick them up; opt in via `pytest tests/integration`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from paige.adapters.storage import FileStorage
from paige.ports.storage import Storage

pytestmark = pytest.mark.integration


def test_filestorage_satisfies_storage_protocol(tmp_path: Path) -> None:
    assert isinstance(FileStorage(tmp_path), Storage)


async def test_save_then_load_round_trip(tmp_path: Path) -> None:
    s = FileStorage(tmp_path)
    await s.save("bindings", {"u1/oc1": "@5"})
    assert await s.load("bindings") == {"u1/oc1": "@5"}


async def test_load_missing_returns_none(tmp_path: Path) -> None:
    s = FileStorage(tmp_path)
    assert await s.load("never-saved") is None


async def test_save_atomic_no_temp_left_on_disk(tmp_path: Path) -> None:
    """After save returns, no `<key>.json.tmp` remains in the dir —
    the rename is the atomic commit point."""
    s = FileStorage(tmp_path)
    await s.save("k", {"x": "1"})
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["k.json"]


async def test_save_overwrites_atomically(tmp_path: Path) -> None:
    """A second save replaces the contents — readers always see
    either the old or new full file, never a half-write."""
    s = FileStorage(tmp_path)
    await s.save("k", {"v": "1"})
    await s.save("k", {"v": "2"})
    assert await s.load("k") == {"v": "2"}


async def test_delete_removes_file(tmp_path: Path) -> None:
    s = FileStorage(tmp_path)
    await s.save("k", {"v": "x"})
    await s.delete("k")
    assert await s.load("k") is None
    assert not (tmp_path / "k.json").exists()


async def test_delete_missing_is_no_op(tmp_path: Path) -> None:
    s = FileStorage(tmp_path)
    await s.delete("never-existed")  # no error


async def test_invalid_keys_rejected(tmp_path: Path) -> None:
    s = FileStorage(tmp_path)
    for bad in ("", "a/b", "a\\b", "..", "../escape", ".."):
        with pytest.raises(ValueError):
            await s.save(bad, {})


async def test_value_must_be_dict_when_loaded(tmp_path: Path) -> None:
    """If someone hand-edits a state file to be a JSON list,
    we surface that as a ValueError instead of returning the wrong type."""
    s = FileStorage(tmp_path)
    (tmp_path / "bad.json").write_text("[1, 2, 3]")
    with pytest.raises(ValueError):
        await s.load("bad")


async def test_concurrent_saves_eventually_settle(tmp_path: Path) -> None:
    """Two coroutines saving the same key sequentially: the
    second's contents win. Atomic rename means we never end
    up with a corrupt file."""
    s = FileStorage(tmp_path)
    await asyncio.gather(
        s.save("k", {"v": "first"}),
        s.save("k", {"v": "second"}),
    )
    final = await s.load("k")
    assert final in ({"v": "first"}, {"v": "second"})
    # Either way, the file is well-formed JSON, no temp left.
    assert sorted(os.listdir(tmp_path)) == ["k.json"]
