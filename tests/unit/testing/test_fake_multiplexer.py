"""FakeMultiplexer — observable in-memory multiplexer for tests."""

from __future__ import annotations

from pathlib import Path

from paige.ports.multiplexer import Multiplexer
from paige.testing.fakes import FakeMultiplexer, SendKeysCall


def test_satisfies_multiplexer_protocol() -> None:
    assert isinstance(FakeMultiplexer(), Multiplexer)


async def test_create_then_find_and_list() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("proj", Path("/proj"))
    assert pane.pane_name == "proj"
    found = await m.find_pane(pane.pane_id)
    assert found == pane
    listed = await m.list_panes()
    assert listed == [pane]


async def test_create_records_call() -> None:
    m = FakeMultiplexer()
    p1 = await m.create_pane("a", Path("/x"))
    p2 = await m.create_pane("b", Path("/y"))
    assert m.created == [p1, p2]


async def test_seed_pane_for_pre_existing_state() -> None:
    """`add_pane` lets tests simulate a tmux pane that existed
    before paige started watching."""
    m = FakeMultiplexer()
    seeded = m.add_pane("@99", "external", Path("/work"), "paige")
    found = await m.find_pane("@99")
    assert found == seeded
    assert found.multiplexer_session == "paige"


async def test_kill_pane_returns_true_when_existed() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("p", Path("/"))
    ok = await m.kill_pane(pane.pane_id)
    assert ok
    assert m.killed == [pane.pane_id]
    assert await m.find_pane(pane.pane_id) is None


async def test_kill_pane_returns_false_when_missing() -> None:
    m = FakeMultiplexer()
    assert await m.kill_pane("@nope") is False
    assert m.killed == []


async def test_rename_replaces_name_in_place() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("old", Path("/"))
    ok = await m.rename_pane(pane.pane_id, "new")
    assert ok
    refreshed = await m.find_pane(pane.pane_id)
    assert refreshed is not None and refreshed.pane_name == "new"


async def test_send_keys_records_with_flags() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("p", Path("/"))
    await m.send_keys(pane.pane_id, "hi", enter=True, literal=True)
    await m.send_keys(pane.pane_id, "Up", enter=False, literal=False)
    assert m.send_keys_calls == [
        SendKeysCall(pane_id=pane.pane_id, text="hi", enter=True, literal=True),
        SendKeysCall(pane_id=pane.pane_id, text="Up", enter=False, literal=False),
    ]


async def test_send_keys_to_missing_pane_returns_false() -> None:
    m = FakeMultiplexer()
    assert await m.send_keys("@nope", "hi") is False
    assert m.send_keys_calls == []


async def test_capture_returns_seeded_content() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("p", Path("/"))
    m.set_capture(pane.pane_id, "hello\nworld")
    assert await m.capture(pane.pane_id) == "hello\nworld"


async def test_capture_unseeded_returns_empty_string_for_existing_pane() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("p", Path("/"))
    assert await m.capture(pane.pane_id) == ""


async def test_capture_missing_pane_returns_none() -> None:
    m = FakeMultiplexer()
    assert await m.capture("@nope") is None


async def test_foreground_pid_seeded_value() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("p", Path("/"))
    m.set_foreground_pid(pane.pane_id, 42)
    assert await m.get_foreground_pid(pane.pane_id) == 42


async def test_foreground_pid_unseeded_is_none() -> None:
    m = FakeMultiplexer()
    pane = await m.create_pane("p", Path("/"))
    assert await m.get_foreground_pid(pane.pane_id) is None
