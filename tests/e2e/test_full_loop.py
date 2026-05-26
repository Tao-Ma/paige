"""End-to-end: real tmux + mock_claude + paige's full service graph.

Architecture under test (real except where noted):

    User → FakeChannel.deliver_inbound          [FAKE]
        → Dispatcher → RunRegistry.get_pane     [REAL]
        → TmuxMultiplexer.send_keys             [REAL tmux]
        → mock_claude (in tmux pane)            [REAL process]
        → opens ~/.claude/tasks/<sid>/.lock     [paige's discovery signal]
        → writes to ~/.claude/projects/.../sid.jsonl
        → JsonlWatcher detects bytes            [REAL]
        → Dispatcher routes events
        → Outbox → FakeChannel.send             [FAKE → recorder]

RunDiscovery walks `/proc/<pid>/fd/` for symlinks pointing into
`~/.claude/tasks/<uuid>/...` — that uuid is the live sessionId. The
fixture sandboxes $HOME so mock_claude's tasks-dir lock fd lands in
tmp_path, not the dev machine's real home.

Linux-only (real /proc dependency). Marked `e2e` so it stays out of
default `pytest` runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import libtmux  # type: ignore[import-untyped]
import pytest

from paige.adapters.jsonl_watcher import JsonlWatcher
from paige.adapters.storage import FileStorage
from paige.adapters.tmux import TmuxMultiplexer
from paige.application.run_registry import RunRegistry
from paige.domain.conversation import Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent
from paige.domain.person import Person
from paige.entrypoint.app import App, assemble
from paige.infrastructure.transcript_path import transcript_path
from paige.testing.fakes import FakeChannel

pytestmark = pytest.mark.e2e

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")

_MOCK_CLAUDE = Path(__file__).parent.parent / "_fixtures" / "mock_claude"


class Harness:
    """Bag of fixtures the test reads from."""

    app: App
    channel: FakeChannel
    multiplexer: TmuxMultiplexer
    watcher: JsonlWatcher
    registry: RunRegistry
    pane_id: str
    jsonl_path: Path
    server: libtmux.Server
    tmp_root: Path


@pytest.fixture
async def e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    if sys.platform != "linux":
        pytest.skip("e2e relies on real /proc")

    tmp_root = tmp_path
    proj_cwd = tmp_root / "proj"
    proj_cwd.mkdir()

    # Sandbox $HOME so mock_claude's `~/.claude/tasks/<sid>/.lock` fd
    # lands under tmp_path, and the JSONL `transcript_path` call
    # resolves there too. Set BEFORE the libtmux.Server starts so the
    # tmux server (and any panes it spawns) inherit the patched env.
    fake_home = tmp_root / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    socket_name = f"paige-e2e-{uuid.uuid4().hex[:8]}"
    server = libtmux.Server(socket_name=socket_name)

    # Real Claude Code uses the dashed RFC-4122 form
    # (`8-4-4-4-12`); paige's discovery validates that shape, so the
    # mock has to match.
    sid = str(uuid.uuid4())
    # Compute the JSONL path the way discovery will: paige reads
    # mock_claude's `~/.claude/tasks/<sid>/.lock` fd, then calls
    # `transcript_path(sid, proj_cwd)` →
    # `~/.claude/projects/<encoded_cwd>/<sid>.jsonl`. With $HOME
    # patched above this resolves under tmp_root.
    jsonl = transcript_path(sid, proj_cwd)
    jsonl.parent.mkdir(parents=True)
    # File doesn't have to pre-exist; mock_claude opens with append.

    state_dir = tmp_root / "state"
    state_dir.mkdir()

    # Build paige's service graph against this tmux server.
    storage = FileStorage(state_dir)
    multiplexer = TmuxMultiplexer(default_session="paige-e2e", socket_name=socket_name)
    watcher = JsonlWatcher(storage, poll_interval=0.05)
    channel = FakeChannel()
    registry = RunRegistry(storage)
    await registry.load()

    app = assemble(
        channel=channel,
        multiplexer=multiplexer,
        watcher=watcher,
        storage=storage,
        registry=registry,
        status_interval=0.05,
        discovery_interval=0.1,
    )

    # Spawn mock_claude in a tmux window.
    pane = await multiplexer.create_pane("proj", proj_cwd)

    # Start mock_claude in the pane, with JSONL_PATH pointing at our
    # marker dir.
    cmd = f"JSONL_PATH={jsonl} {shutil.which('python3') or sys.executable} {_MOCK_CLAUDE}"
    sent = await multiplexer.send_keys(pane.pane_id, cmd, enter=True, literal=True)
    assert sent

    # Wait for mock_claude to print its readiness banner. This is also
    # what makes the JSONL fd appear in /proc.
    await _wait_for_pane_text(multiplexer, pane.pane_id, "mock_claude ready", 5.0)

    await app.start()

    h = Harness()
    h.app = app
    h.channel = channel
    h.multiplexer = multiplexer
    h.watcher = watcher
    h.registry = registry
    h.pane_id = pane.pane_id
    h.jsonl_path = jsonl
    h.server = server
    h.tmp_root = tmp_root

    try:
        yield h
    finally:
        await app.stop()
        with contextlib.suppress(Exception):
            server.kill_server()


async def _wait_for_pane_text(
    multiplexer: TmuxMultiplexer,
    pane_id: str,
    needle: str,
    timeout: float,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        text = await multiplexer.capture(pane_id)
        if text and needle in text:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"pane {pane_id} never showed {needle!r}")


async def _wait_for(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"predicate {predicate} never became true within {timeout}s")


# ── tests ────────────────────────────────────────────────────────


async def test_run_discovery_finds_mock_claude(e2e: Harness) -> None:
    """RunDiscovery should find mock_claude via the open
    `~/.claude/tasks/<sid>/.lock` fd within a couple of ticks."""
    h = e2e
    await _wait_for(
        lambda: h.pane_id in h.registry.list_panes(),
        timeout=3.0,
    )
    ptr = h.registry.get_run_pointer(h.pane_id)
    assert ptr is not None
    assert ptr.run_id == h.jsonl_path.stem


async def test_user_message_round_trips(e2e: Harness) -> None:
    """Inbound text → tmux → mock_claude → JSONL → watcher →
    Dispatcher → Outbox → FakeChannel.sent."""
    h = e2e

    # Wait for discovery to register the run.
    await _wait_for(
        lambda: h.pane_id in h.registry.list_panes(),
        timeout=3.0,
    )

    # Bind directly (UI flow tested in unit tests).
    await h.registry.bind(ALICE, CONV, h.pane_id)

    # Send the user's chat message → reaches mock_claude via tmux.
    inbound = Inbound(
        sender=ALICE,
        conversation=CONV,
        text="hello world",
        message_id="m1",
    )
    await h.channel.deliver_inbound(inbound)

    # Wait for the assistant echo to flow back through the watcher.
    def echo_arrived() -> bool:
        return any(
            isinstance(o.content, CardContent) and "echo: hello world" in o.content.card.text
            for o in h.channel.sent
        )

    await _wait_for(echo_arrived, timeout=10.0)


async def test_run_discovery_survives_clear(e2e: Harness) -> None:
    """Regression: `/clear` rotates Claude's sessionId, and the
    only place paige can pick up the new value is the live process's
    open `~/.claude/tasks/<sid>/.lock` fd. The per-pid session file
    Claude Code writes (`~/.claude/sessions/<pid>.json`) freezes its
    `sessionId` field at process startup — never rewritten on
    `/clear` — so any file-based discovery returns the stale uuid,
    the watcher tails the dead JSONL, and every JSONL event silently
    drops at `find_bindings_for_run`. Symptom: spinner + permission
    cards keep working (pane scrape) but content / tool_use /
    tool_result vanish from IM.

    mock_claude's `__CLEAR__` sentinel reproduces the rotation:
    closes old fd, opens new task-dir lock, writes to new JSONL,
    does NOT rewrite the session file. This test would have caught
    both the April `14d5a8b` regression and the May
    `7f3505c+6ae537f` regression.
    """
    h = e2e

    await _wait_for(lambda: h.pane_id in h.registry.list_panes(), timeout=3.0)
    await h.registry.bind(ALICE, CONV, h.pane_id)

    # First round-trip on the original sid.
    initial_sid = h.jsonl_path.stem
    ptr_before = h.registry.get_run_pointer(h.pane_id)
    assert ptr_before is not None and ptr_before.run_id == initial_sid

    inbound1 = Inbound(sender=ALICE, conversation=CONV, text="before-clear", message_id="m1")
    await h.channel.deliver_inbound(inbound1)

    def echo_arrived(needle: str) -> bool:
        return any(
            isinstance(o.content, CardContent) and needle in o.content.card.text
            for o in h.channel.sent
        )

    await _wait_for(lambda: echo_arrived("echo: before-clear"), timeout=10.0)

    # Trigger the rotation. mock_claude sees `__CLEAR__` on stdin →
    # opens a new tasks-dir fd + JSONL, leaves the session file's
    # sessionId stale. paige's RunDiscovery has to find the new sid
    # via the fd walk on the next tick.
    await h.multiplexer.send_keys(h.pane_id, "__CLEAR__", enter=True, literal=True)

    # Wait for the registry to flip to a new run_id (anything but the
    # original). With the fd walk this happens within one tick;
    # without it the registry would stay on `initial_sid` forever
    # (any file-based discovery would trust the frozen session-file
    # `sessionId`).
    def run_id_rotated() -> bool:
        ptr = h.registry.get_run_pointer(h.pane_id)
        return ptr is not None and ptr.run_id != initial_sid

    await _wait_for(run_id_rotated, timeout=5.0)

    # Second round-trip on the post-rotation sid. If discovery is
    # still pinned to the stale sid, the watcher won't pick up the
    # new JSONL's writes and this echo never arrives.
    inbound2 = Inbound(sender=ALICE, conversation=CONV, text="after-clear", message_id="m2")
    await h.channel.deliver_inbound(inbound2)

    await _wait_for(lambda: echo_arrived("echo: after-clear"), timeout=10.0)


async def test_status_card_appears_during_thinking(e2e: Harness) -> None:
    """While mock_claude renders its spinner line, StatusService
    should send a status card. After the spinner clears, the
    debounced delete kicks in."""
    h = e2e
    await _wait_for(lambda: h.pane_id in h.registry.list_panes(), timeout=3.0)
    await h.registry.bind(ALICE, CONV, h.pane_id)

    inbound = Inbound(sender=ALICE, conversation=CONV, text="ping", message_id="m1")
    await h.channel.deliver_inbound(inbound)

    # The status card is sent as a card; it should appear at least
    # once within a few seconds.
    from paige.domain.outbound import CardContent

    def status_card_seen() -> bool:
        return any(isinstance(o.content, CardContent) for o in h.channel.sent)

    await _wait_for(status_card_seen, timeout=10.0)
