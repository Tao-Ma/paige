"""Multiplexer Protocol — compliance + minimal stub."""

from __future__ import annotations

from pathlib import Path

from paige.domain.pane import Pane
from paige.ports.multiplexer import Multiplexer


class _StubMux:
    def __init__(self) -> None:
        self._panes: dict[str, Pane] = {}
        self.last_keys: tuple[str, str, bool, bool] | None = None

    async def list_panes(self) -> list[Pane]:
        return list(self._panes.values())

    async def find_pane(self, pane_id: str) -> Pane | None:
        return self._panes.get(pane_id)

    async def create_pane(self, name: str, cwd: Path, command: str = "") -> Pane:
        pane = Pane(pane_id=f"@{len(self._panes)}", pane_name=name, cwd=cwd)
        self._panes[pane.pane_id] = pane
        return pane

    async def kill_pane(self, pane_id: str) -> bool:
        return self._panes.pop(pane_id, None) is not None

    async def rename_pane(self, pane_id: str, new_name: str) -> bool:
        return pane_id in self._panes

    async def send_keys(
        self,
        pane_id: str,
        text: str,
        *,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        self.last_keys = (pane_id, text, enter, literal)
        return True

    async def capture(self, pane_id: str) -> str | None:
        return "" if pane_id in self._panes else None

    async def capture_with_ansi(self, pane_id: str) -> str | None:
        return "" if pane_id in self._panes else None

    async def get_foreground_pid(self, pane_id: str) -> int | None:
        return 1234 if pane_id in self._panes else None


def test_stub_satisfies_multiplexer_protocol() -> None:
    assert isinstance(_StubMux(), Multiplexer)


async def test_create_then_find_round_trips() -> None:
    mux: Multiplexer = _StubMux()
    pane = await mux.create_pane("proj", Path("/proj"))
    assert pane.pane_name == "proj"
    found = await mux.find_pane(pane.pane_id)
    assert found == pane


async def test_send_keys_carries_flags() -> None:
    mux = _StubMux()
    pane = await mux.create_pane("p", Path("/"))
    ok = await mux.send_keys(pane.pane_id, "Up", enter=False, literal=False)
    assert ok
    assert mux.last_keys == (pane.pane_id, "Up", False, False)
