"""Storage Protocol — compliance + in-memory stub."""

from __future__ import annotations

from typing import Any

from paige.ports.storage import Storage


class _StubStorage:
    """In-memory key-value, satisfies Storage."""

    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}

    async def load(self, key: str) -> dict[str, Any] | None:
        v = self._d.get(key)
        return dict(v) if v is not None else None

    async def save(self, key: str, value: dict[str, Any]) -> None:
        self._d[key] = dict(value)

    async def delete(self, key: str) -> None:
        self._d.pop(key, None)


def test_stub_satisfies_storage_protocol() -> None:
    assert isinstance(_StubStorage(), Storage)


async def test_save_and_load_round_trip() -> None:
    s: Storage = _StubStorage()
    await s.save("bindings", {"u1/oc1": "@5"})
    assert await s.load("bindings") == {"u1/oc1": "@5"}


async def test_load_missing_returns_none() -> None:
    s: Storage = _StubStorage()
    assert await s.load("never-saved") is None


async def test_delete_is_idempotent() -> None:
    s: Storage = _StubStorage()
    await s.save("x", {"a": "b"})
    await s.delete("x")
    await s.delete("x")  # no-op
    assert await s.load("x") is None
