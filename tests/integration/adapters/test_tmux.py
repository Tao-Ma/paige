"""TmuxMultiplexer — real-tmux integration tests.

Each test runs against a fresh tmux server on a unique socket
name, so the user's normal tmux is never touched. The fixture
kills the server on teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import libtmux  # type: ignore[import-untyped]
import pytest

from paige.adapters.tmux import TmuxMultiplexer
from paige.ports.multiplexer import Multiplexer

pytestmark = pytest.mark.integration


@pytest.fixture
async def mux() -> AsyncIterator[TmuxMultiplexer]:
    """A TmuxMultiplexer on its own socket; server killed on teardown."""
    socket_name = f"paige-test-{uuid.uuid4().hex[:8]}"
    m = TmuxMultiplexer(default_session="paige-test", socket_name=socket_name)
    yield m
    # Tear down the server fully to free the socket.
    server = libtmux.Server(socket_name=socket_name)
    with contextlib.suppress(Exception):
        server.kill_server()


def test_satisfies_multiplexer_protocol(mux: TmuxMultiplexer) -> None:
    assert isinstance(mux, Multiplexer)


async def test_create_pane_then_find_round_trip(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    pane = await mux.create_pane("proj", tmp_path)
    assert pane.pane_id.startswith("@")
    assert pane.pane_name == "proj"
    found = await mux.find_pane(pane.pane_id)
    assert found is not None
    assert found.pane_id == pane.pane_id
    assert found.pane_name == "proj"


async def test_list_panes_returns_created_panes(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    p1 = await mux.create_pane("a", tmp_path)
    p2 = await mux.create_pane("b", tmp_path)
    listed = await mux.list_panes()
    ids = {p.pane_id for p in listed}
    assert p1.pane_id in ids
    assert p2.pane_id in ids


async def test_kill_pane_removes_it(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    pane = await mux.create_pane("doomed", tmp_path)
    assert await mux.kill_pane(pane.pane_id) is True
    # libtmux's kill is async on tmux's side — give it a moment.
    await asyncio.sleep(0.1)
    assert await mux.find_pane(pane.pane_id) is None


async def test_kill_missing_pane_returns_false(mux: TmuxMultiplexer) -> None:
    assert await mux.kill_pane("@99999") is False


async def test_rename_pane_updates_name(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    pane = await mux.create_pane("oldname", tmp_path)
    assert await mux.rename_pane(pane.pane_id, "newname") is True
    refreshed = await mux.find_pane(pane.pane_id)
    assert refreshed is not None and refreshed.pane_name == "newname"


async def test_send_keys_then_capture_sees_text(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    """Send a literal echo command + Enter; capture should show
    both the typed line and the echoed output."""
    pane = await mux.create_pane("echo-test", tmp_path)
    # Wait for the shell to render its prompt.
    await asyncio.sleep(0.2)
    ok = await mux.send_keys(pane.pane_id, "echo paige-marker-xyz", enter=True)
    assert ok
    # send_keys with enter=True splits and sleeps internally;
    # give the shell time to print the result.
    await asyncio.sleep(0.6)
    text = await mux.capture(pane.pane_id)
    assert text is not None
    assert "paige-marker-xyz" in text


async def test_capture_missing_pane_is_none(mux: TmuxMultiplexer) -> None:
    assert await mux.capture("@99999") is None


async def test_send_keys_special_keys_pass_through(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    """literal=False routes special key names like Up/Down/Escape
    straight through (no split-and-delay)."""
    pane = await mux.create_pane("keys", tmp_path)
    await asyncio.sleep(0.1)
    ok = await mux.send_keys(pane.pane_id, "Escape", enter=False, literal=False)
    assert ok


async def test_get_foreground_pid_is_int(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    pane = await mux.create_pane("pid", tmp_path)
    pid = await mux.get_foreground_pid(pane.pane_id)
    assert pid is not None
    assert pid > 0


async def test_send_keys_exits_copy_mode_first(mux: TmuxMultiplexer, tmp_path: Path) -> None:
    """If the pane is in copy-mode (which happens silently when the
    user scrolls with `mouse on`), tmux interprets each character we
    send as a copy-mode binding — `g` opens a `(goto line)`
    command-prompt, `f`/`F`/`t`/`T` open jump prompts, etc. — and
    the text never reaches the running program. send_keys must
    detect this and send `q` first to cancel copy-mode.

    Test shape: put the pane into copy-mode via `tmux copy-mode`,
    then send a marker that starts with `g` (the worst case — it
    would otherwise trigger the goto-line prompt). After send_keys
    the pane must be back to normal AND the marker must reach the
    shell so `echo` prints it."""
    pane = await mux.create_pane("copy-mode", tmp_path)
    await asyncio.sleep(0.2)

    # Drop the pane into copy-mode the same way a mouse scroll would.
    server = mux._get_server()  # type: ignore[reportPrivateUsage]
    server.cmd("copy-mode", "-t", pane.pane_id)
    await asyncio.sleep(0.1)
    window_before = mux._find_window_sync(pane.pane_id)  # type: ignore[reportPrivateUsage]
    assert window_before is not None
    pane_before = window_before.active_pane
    assert pane_before is not None
    assert TmuxMultiplexer._pane_in_mode(pane_before) is True

    # The marker starts with `g` — without the copy-mode exit this
    # would trigger tmux's `(goto line)` prompt and the rest of the
    # input would land in that prompt.
    ok = await mux.send_keys(pane.pane_id, "echo goto-marker-xyz", enter=True)
    assert ok
    await asyncio.sleep(0.6)

    # Re-read pane_in_mode — should be back to 0 (the `q` cancelled).
    window_after = mux._find_window_sync(pane.pane_id)  # type: ignore[reportPrivateUsage]
    assert window_after is not None
    pane_after = window_after.active_pane
    assert pane_after is not None
    assert TmuxMultiplexer._pane_in_mode(pane_after) is False

    captured = await mux.capture(pane.pane_id)
    assert captured is not None
    assert "goto-marker-xyz" in captured
