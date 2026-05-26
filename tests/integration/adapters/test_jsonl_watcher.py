"""JsonlWatcher — integration tests against real JSONL files.

Verifies the end-to-end loop: track a file, append JSONL, flush
(or wait one poll cycle), see events. State persistence covered
via FileStorage round-trips.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from paige.adapters.jsonl_watcher import JsonlWatcher
from paige.adapters.storage import FileStorage
from paige.domain.transcript import BlockKind, Role, TranscriptEvent
from paige.ports.watcher import Watcher

pytestmark = pytest.mark.integration


def _user_line(text: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "uuid": f"u-{text}",
                "timestamp": "2026-04-25T10:00:00Z",
                "message": {"role": "user", "content": text},
            }
        )
        + "\n"
    )


def _assistant_text_line(text: str) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


def _assistant_tool_use_line(tool_id: str, name: str = "AskUserQuestion") -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": name,
                            "input": {"q": "?"},
                        }
                    ],
                },
            }
        )
        + "\n"
    )


def _user_tool_result_line(tool_id: str, text: str = "ok") -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": text,
                        }
                    ],
                },
            }
        )
        + "\n"
    )


@pytest.fixture
async def watcher(tmp_path: Path) -> AsyncIterator[JsonlWatcher]:
    storage = FileStorage(tmp_path / "state")
    w = JsonlWatcher(storage, poll_interval=0.05)
    yield w
    await w.stop()


def test_satisfies_watcher_protocol(watcher: JsonlWatcher) -> None:
    assert isinstance(watcher, Watcher)


async def test_flush_emits_events_for_appended_lines(watcher: JsonlWatcher, tmp_path: Path) -> None:
    transcript = tmp_path / "run.jsonl"

    received: list[tuple[str, TranscriptEvent]] = []

    async def handler(run_id: str, ev: TranscriptEvent) -> None:
        received.append((run_id, ev))

    watcher.on_event(handler)
    # Track before writing — `track()` skips past anything on disk
    # at first sight, so writes need to happen after to be seen.
    watcher.track("run-1", transcript)
    transcript.write_text(_user_line("first") + _assistant_text_line("hi"))

    n = await watcher.flush()
    assert n == 2
    assert [r[0] for r in received] == ["run-1", "run-1"]
    assert received[0][1].role is Role.USER
    assert received[1][1].role is Role.ASSISTANT
    assert received[1][1].blocks[0].kind is BlockKind.TEXT


async def test_flush_only_returns_new_lines_after_first_read(
    watcher: JsonlWatcher, tmp_path: Path
) -> None:
    transcript = tmp_path / "run.jsonl"

    seen: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        seen.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)
    transcript.write_text(_user_line("first"))

    assert await watcher.flush() == 1
    assert await watcher.flush() == 0  # nothing new

    with transcript.open("a") as f:
        f.write(_assistant_text_line("answer"))

    assert await watcher.flush() == 1
    assert len(seen) == 2
    assert seen[1].blocks[0].text == "answer"


async def test_truncation_resets_offset(watcher: JsonlWatcher, tmp_path: Path) -> None:
    transcript = tmp_path / "run.jsonl"

    seen: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        seen.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)
    transcript.write_text(_user_line("first") + _assistant_text_line("a"))
    await watcher.flush()
    assert len(seen) == 2

    # Simulate /clear: file is rewritten with shorter content.
    transcript.write_text(_user_line("after-clear"))
    n = await watcher.flush()
    assert n == 1
    assert seen[2].blocks[0].text == "after-clear"


async def test_track_skips_existing_content(watcher: JsonlWatcher, tmp_path: Path) -> None:
    """Tracking a JSONL that already has bytes on disk must NOT
    re-emit them. Production attaches paige to long-running claude
    sessions whose transcripts can be 10s of MB; replaying that
    history floods the chat. The first track sees those bytes as
    "history we missed" and skips past them.
    """
    transcript = tmp_path / "run.jsonl"
    transcript.write_text(
        _user_line("ancient-history-1")
        + _assistant_text_line("ancient-history-2")
        + _user_line("ancient-history-3")
    )

    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)

    # Initial flush sees nothing — all existing bytes are below the
    # offset. No flood.
    assert await watcher.flush() == 0
    assert received == []

    # Subsequent appends emit normally.
    with transcript.open("a") as f:
        f.write(_assistant_text_line("fresh-content"))
    assert await watcher.flush() == 1
    assert len(received) == 1
    assert received[0].blocks[0].text == "fresh-content"


async def test_track_seeds_unanswered_tool_use(watcher: JsonlWatcher, tmp_path: Path) -> None:
    """Pre-existing JSONL whose last tool_use has no matching
    tool_result represents a session blocked on AskUserQuestion when
    paige attached. The watcher must seed that tool_use so it
    surfaces in IM, even though everything else on disk is treated
    as history.
    """
    transcript = tmp_path / "run.jsonl"
    transcript.write_text(
        _user_line("history-1")
        + _assistant_text_line("history-2")
        + _assistant_tool_use_line("toolu_pending")
    )

    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)

    assert await watcher.flush() == 1
    assert len(received) == 1
    assert received[0].blocks[0].kind is BlockKind.TOOL_USE
    assert received[0].blocks[0].tool_id == "toolu_pending"
    assert received[0].blocks[0].tool_name == "AskUserQuestion"


async def test_track_skips_answered_tool_use(watcher: JsonlWatcher, tmp_path: Path) -> None:
    """tool_use whose tool_result already arrived before track() is
    history — must not re-emit on attach."""
    transcript = tmp_path / "run.jsonl"
    transcript.write_text(
        _assistant_tool_use_line("toolu_done") + _user_tool_result_line("toolu_done")
    )

    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)

    assert await watcher.flush() == 0
    assert received == []


async def test_track_seeds_only_unanswered_in_mixed_history(
    watcher: JsonlWatcher, tmp_path: Path
) -> None:
    """One answered + one unanswered tool_use in pre-existing file:
    only the unanswered one is seeded."""
    transcript = tmp_path / "run.jsonl"
    transcript.write_text(
        _assistant_tool_use_line("toolu_done")
        + _user_tool_result_line("toolu_done")
        + _assistant_text_line("some-prose")
        + _assistant_tool_use_line("toolu_waiting")
    )

    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)

    assert await watcher.flush() == 1
    assert len(received) == 1
    assert received[0].blocks[0].tool_id == "toolu_waiting"


async def test_track_seeds_then_streams_new_appends(watcher: JsonlWatcher, tmp_path: Path) -> None:
    """After seeding the unanswered tool_use from history, bytes
    appended after track() are read normally — no gap, no replay."""
    transcript = tmp_path / "run.jsonl"
    transcript.write_text(_assistant_tool_use_line("toolu_blocked"))

    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)

    # First flush seeds the unanswered tool_use.
    assert await watcher.flush() == 1
    assert received[-1].blocks[0].tool_id == "toolu_blocked"

    # User answers in IM → tool_result lands in JSONL → emitted as
    # a normal append, not a re-seed.
    with transcript.open("a") as f:
        f.write(_user_tool_result_line("toolu_blocked", "yes"))
        f.write(_assistant_text_line("thanks"))

    assert await watcher.flush() == 2
    assert received[-2].blocks[0].kind is BlockKind.TOOL_RESULT
    assert received[-2].blocks[0].tool_id == "toolu_blocked"
    assert received[-1].blocks[0].text == "thanks"


async def test_missing_file_yields_no_events(watcher: JsonlWatcher, tmp_path: Path) -> None:
    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", tmp_path / "nope.jsonl")

    assert await watcher.flush() == 0
    assert received == []


async def test_untrack_stops_emissions(watcher: JsonlWatcher, tmp_path: Path) -> None:
    transcript = tmp_path / "run.jsonl"

    received: list[TranscriptEvent] = []

    async def handler(_run_id: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    watcher.on_event(handler)
    watcher.track("run-1", transcript)
    transcript.write_text(_user_line("first"))
    await watcher.flush()
    watcher.untrack("run-1")

    with transcript.open("a") as f:
        f.write(_assistant_text_line("ignored"))

    assert await watcher.flush() == 0
    assert len(received) == 1


async def test_offset_persists_across_watcher_restarts(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    transcript = tmp_path / "run.jsonl"

    storage1 = FileStorage(state_dir)
    w1 = JsonlWatcher(storage1, poll_interval=0.05)
    seen1: list[TranscriptEvent] = []

    async def h1(_rid: str, ev: TranscriptEvent) -> None:
        seen1.append(ev)

    w1.on_event(h1)
    w1.track("run-1", transcript)
    transcript.write_text(_user_line("first"))
    await w1.flush()
    assert len(seen1) == 1

    # New watcher instance, same storage.
    storage2 = FileStorage(state_dir)
    w2 = JsonlWatcher(storage2, poll_interval=0.05)
    seen2: list[TranscriptEvent] = []

    async def h2(_rid: str, ev: TranscriptEvent) -> None:
        seen2.append(ev)

    w2.on_event(h2)
    w2.track("run-1", transcript)

    # Loaded offset should mean nothing re-delivered.
    await w2.start()
    await w2.flush()
    await w2.stop()
    assert seen2 == []

    # Append + new watcher should pick up only the new bytes.
    with transcript.open("a") as f:
        f.write(_assistant_text_line("new-content"))

    storage3 = FileStorage(state_dir)
    w3 = JsonlWatcher(storage3, poll_interval=0.05)
    seen3: list[TranscriptEvent] = []

    async def h3(_rid: str, ev: TranscriptEvent) -> None:
        seen3.append(ev)

    w3.on_event(h3)
    w3.track("run-1", transcript)
    await w3.start()
    await w3.flush()
    await w3.stop()
    assert len(seen3) == 1
    assert seen3[0].blocks[0].text == "new-content"


async def test_polling_loop_emits_without_explicit_flush(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path / "state")
    transcript = tmp_path / "run.jsonl"

    w = JsonlWatcher(storage, poll_interval=0.05)
    received: list[TranscriptEvent] = []

    async def handler(_rid: str, ev: TranscriptEvent) -> None:
        received.append(ev)

    w.on_event(handler)
    w.track("run-1", transcript)
    transcript.write_text(_user_line("seed"))

    await w.start()
    try:
        # Wait for at least one poll tick.
        for _ in range(40):
            if received:
                break
            await asyncio.sleep(0.05)
    finally:
        await w.stop()

    assert len(received) >= 1
    assert received[0].blocks[0].text == "seed"
