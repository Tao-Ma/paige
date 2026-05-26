"""Multiplexer — the tmux port.

We model a `Pane` as the unit of independent text I/O. Maps to a
tmux *window* in the libtmux backend (libtmux uses "window" for
what we call "pane" — a top-level container with its own cwd +
process). Tmux's *internal* panes (split-screen) are not modelled.

`send_keys` / `capture` operate on a pane by id. The port is
intentionally narrow — no layout / split / attach concepts.

**Host-awareness.** Every operation accepts a `host_id` kwarg
(default `"local"`). Single-host adapters (e.g. `LibtmuxMultiplexer`)
accept and ignore it — they always operate on the box paige is
running on. The `MultiplexerRouter` impl uses `host_id` to
dispatch to the right adapter when multi-host config is in play.
This shape lets services that don't care about hosts call the
existing 1-arg / 2-arg methods unchanged; host-aware callers pass
`host_id=binding.host_id` at the call site. See `doc/multi-host.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.host import LOCAL_HOST_ID
from ..domain.pane import Pane


@runtime_checkable
class Multiplexer(Protocol):
    """Operations on a tmux-like multiplexer."""

    async def list_panes(self, *, host_id: str = LOCAL_HOST_ID) -> list[Pane]: ...
    async def find_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> Pane | None: ...

    async def create_pane(
        self,
        name: str,
        cwd: Path,
        command: str = "",
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> Pane:
        """Spawn a new pane named `name` in `cwd`, optionally running
        `command` (else interactive shell)."""
        ...

    async def kill_pane(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> bool: ...
    async def rename_pane(
        self, pane_id: str, new_name: str, *, host_id: str = LOCAL_HOST_ID
    ) -> bool: ...

    async def send_keys(
        self,
        pane_id: str,
        text: str,
        *,
        enter: bool = True,
        literal: bool = True,
        host_id: str = LOCAL_HOST_ID,
    ) -> bool:
        """Send `text` to the pane.

        `enter`   press Enter after the text (submit a prompt).
        `literal` send characters as-is (True) vs interpret special
                  key names like `Up`, `Escape`, `Tab` (False).
        """
        ...

    async def capture(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        """Capture the pane's visible text. Returns None on failure."""
        ...

    async def capture_with_ansi(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> str | None:
        """Capture the pane with ANSI escape sequences preserved.

        Lets callers see colors, backgrounds, and other styling that
        plain `capture` strips. Used by `/livepane` to surface TUI
        cues that rely on background-color highlights (e.g. Claude
        Code's active-tab indicator in multi-tab AskUserQuestion
        prompts). Returns None on failure."""
        ...

    async def get_foreground_pid(self, pane_id: str, *, host_id: str = LOCAL_HOST_ID) -> int | None:
        """Return the foreground process pid in the pane, or None."""
        ...
