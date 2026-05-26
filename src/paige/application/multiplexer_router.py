"""MultiplexerRouter — host_id → Multiplexer adapter dispatch.

Today paige's `Multiplexer` was implicitly the local libtmux
adapter. This router makes the local host an explicit registered
adapter, paving the way for SSH (or any future remote backend) to
plug in alongside without disturbing call sites.

Shape: implements the `Multiplexer` Protocol itself by delegating
each method to the adapter for the call's `host_id` (defaulting to
`"local"`). Services that don't yet care about hosts call the
existing 1- or 2-arg signatures unchanged; host-aware code passes
`host_id=binding.host_id` and the router routes it.

Fallback policy: an unknown `host_id` falls through to the local
adapter. This matters when a binding refers to a host that's been
removed from `~/.paige/hosts.toml` between paige runs — better the
operation lands on a working multiplexer (and the user gets some
local result they can react to) than to crash the lookup.

This file deliberately doesn't depend on any concrete adapter — it
takes a `dict[str, Multiplexer]` so tests, live deploys, and
future SSH wiring all share the same construction shape.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.host import LOCAL_HOST_ID
from ..domain.pane import Pane
from ..ports.multiplexer import Multiplexer


class MultiplexerRouter:
    """Multiplexer impl that dispatches by `host_id`.

    Construction: `MultiplexerRouter({"local": libtmux_mux,
    "dev-1": ssh_mux})`. The map MUST contain a `"local"` entry —
    it's the fallback for unknown host_ids and the home of paige's
    own machinery.
    """

    def __init__(self, adapters: dict[str, Multiplexer]) -> None:
        if LOCAL_HOST_ID not in adapters:
            raise ValueError(
                f"MultiplexerRouter requires a {LOCAL_HOST_ID!r} entry "
                "(the local-host adapter is the unknown-host fallback "
                "and is also where paige's own machinery runs)."
            )
        self._adapters = dict(adapters)

    def for_host(self, host_id: str) -> Multiplexer:
        """Resolve the adapter for `host_id`, falling back to the
        local adapter on unknown ids. Exposed so a future host-aware
        caller that wants the adapter directly (e.g. for a series of
        ops on the same host) doesn't have to repeat the dispatch."""
        return self._adapters.get(host_id, self._adapters[LOCAL_HOST_ID])

    # ── Multiplexer Protocol — every method dispatches by host_id ──

    async def list_panes(self, *, host_id: str = LOCAL_HOST_ID) -> list[Pane]:
        return await self.for_host(host_id).list_panes()

    async def find_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> Pane | None:
        return await self.for_host(host_id).find_pane(pane_id)

    async def create_pane(
        self,
        name: str,
        cwd: Path,
        command: str = "",
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> Pane:
        return await self.for_host(host_id).create_pane(name, cwd, command)

    async def kill_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> bool:
        return await self.for_host(host_id).kill_pane(pane_id)

    async def rename_pane(
        self, pane_id: str, new_name: str, *, host_id: str = LOCAL_HOST_ID
    ) -> bool:
        return await self.for_host(host_id).rename_pane(pane_id, new_name)

    async def send_keys(
        self,
        pane_id: str,
        text: str,
        *,
        enter: bool = True,
        literal: bool = True,
        host_id: str = LOCAL_HOST_ID,
    ) -> bool:
        return await self.for_host(host_id).send_keys(pane_id, text, enter=enter, literal=literal)

    async def capture(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        return await self.for_host(host_id).capture(pane_id)

    async def capture_with_ansi(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        return await self.for_host(host_id).capture_with_ansi(pane_id)

    async def get_foreground_pid(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> int | None:
        return await self.for_host(host_id).get_foreground_pid(pane_id)


__all__ = ["MultiplexerRouter"]
