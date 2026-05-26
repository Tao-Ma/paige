"""FakeStorage — observable in-memory storage for tests."""

from __future__ import annotations

from paige.ports.storage import Storage
from paige.testing.fakes import FakeStorage


def test_satisfies_storage_protocol() -> None:
    assert isinstance(FakeStorage(), Storage)


async def test_save_records_calls_in_order() -> None:
    s = FakeStorage()
    await s.save("a", {"v": "1"})
    await s.save("b", {"v": "2"})
    assert s.saves == [("a", {"v": "1"}), ("b", {"v": "2"})]


async def test_load_returns_a_copy_not_a_reference() -> None:
    """Mutating the loaded dict must NOT affect storage —
    callers can't accidentally corrupt state."""
    s = FakeStorage()
    await s.save("k", {"x": "1"})
    loaded = await s.load("k")
    assert loaded == {"x": "1"}
    loaded["x"] = "tampered"  # type: ignore[index]
    fresh = await s.load("k")
    assert fresh == {"x": "1"}


async def test_save_records_a_copy_not_a_reference() -> None:
    """Mutating the saved value AFTER save must NOT change what
    storage holds — saves are snapshots."""
    s = FakeStorage()
    val = {"x": "1"}
    await s.save("k", val)
    val["x"] = "tampered"
    fresh = await s.load("k")
    assert fresh == {"x": "1"}


async def test_delete_removes_value_and_records() -> None:
    s = FakeStorage()
    await s.save("k", {"v": "x"})
    await s.delete("k")
    assert await s.load("k") is None
    assert s.deletes == ["k"]


async def test_delete_missing_is_idempotent() -> None:
    s = FakeStorage()
    await s.delete("never-saved")  # no error
    await s.delete("never-saved")  # still no error
    assert s.deletes == ["never-saved", "never-saved"]
