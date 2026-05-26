"""ScreenshotService — `/screenshot` capture + control-key taps."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.message_seq import MessageSeqService
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.application.screenshot import (
    ACTION_KEY,
    ACTION_REFRESH,
    CAPTURE_FAILED_HINT,
    PANE_GONE_HINT,
    REFRESH_LABEL,
    UNBOUND_HINT,
    ScreenshotService,
)
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import DocumentContent, TextContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = ScreenshotService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        message_seq=MessageSeqService(),
    )
    service.install(channel)

    class Harness:
        pass

    h = Harness()
    h.channel = channel  # type: ignore[attr-defined]
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    h.service = service  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _inbound(text: str = "/screenshot") -> Inbound:
    return Inbound(sender=ALICE, conversation=CONV, text=text, message_id="m1")


def _action(action_id: str, value: dict[str, str]) -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m9"),
        action_id=action_id,
        value=value,
        ack_token="tok",
    )


# ── /screenshot — happy path ─────────────────────────────────────


async def test_screenshot_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("screenshot", _inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == UNBOUND_HINT


async def test_screenshot_pane_gone(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    # Bind to a pane that doesn't exist on the multiplexer side.
    await h.registry.bind(ALICE, CONV, "@404")

    await h.channel.deliver_command("screenshot", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == PANE_GONE_HINT


async def test_screenshot_empty_capture(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    # Default capture is "" — counts as failure (consistent with v1).

    await h.channel.deliver_command("screenshot", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == CAPTURE_FAILED_HINT


async def test_screenshot_renders_png_and_attaches_keys(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.mux.set_capture("@1", "$ echo hello\nhello\n")

    await h.channel.deliver_command("screenshot", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, DocumentContent)
    assert sent.content.as_image is True
    assert sent.content.filename == "screenshot.png"
    # PNG magic.
    assert sent.content.data.startswith(b"\x89PNG\r\n\x1a\n")
    # 3 rows × 3 keys + a final 1-button Refresh row.
    assert len(sent.content.rows) == 4
    for row in sent.content.rows[:3]:
        assert len(row) == 3
    [refresh] = sent.content.rows[3]
    assert refresh.action_id == ACTION_REFRESH
    assert refresh.value["p"] == "@1"
    # Every key button carries the pane id + a key id.
    keys = [b for row in sent.content.rows[:3] for b in row]
    assert {b.action_id for b in keys} == {ACTION_KEY}
    assert all(b.value["p"] == "@1" for b in keys)
    key_ids = {b.value["k"] for b in keys}
    assert key_ids == {"up", "dn", "lt", "rt", "esc", "ent", "spc", "tab", "cc"}


# ── tap on a control key ─────────────────────────────────────────


async def test_key_tap_sends_named_key(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))

    await h.channel.deliver_action(_action(ACTION_KEY, {"k": "up", "p": "@1"}))

    [call] = h.mux.send_keys_calls
    assert call.pane_id == "@1"
    assert call.text == "Up"
    assert call.enter is False
    assert call.literal is False
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "↑"


async def test_key_tap_unknown_key(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))

    await h.channel.deliver_action(_action(ACTION_KEY, {"k": "f99", "p": "@1"}))

    assert h.mux.send_keys_calls == []
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "Invalid key"


async def test_key_tap_pane_gone(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness

    await h.channel.deliver_action(_action(ACTION_KEY, {"k": "up", "p": "@deleted"}))

    assert h.mux.send_keys_calls == []
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "Pane is gone"


async def test_key_tap_other_action_ignored(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))

    # Some other service's action_id — ours should bail silently.
    await h.channel.deliver_action(_action("ses:bind", {"pane_id": "@1"}))

    assert h.mux.send_keys_calls == []
    assert h.channel.acks == []


async def test_key_tap_carbon_copy_sends_C_dash_c(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))

    await h.channel.deliver_action(_action(ACTION_KEY, {"k": "cc", "p": "@1"}))

    [call] = h.mux.send_keys_calls
    assert call.text == "C-c"
    assert call.literal is False  # tmux interprets as Ctrl-C


# ── tap on Refresh ───────────────────────────────────────────────


async def test_refresh_recaptures_and_edits(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    h.mux.set_capture("@1", "$ pwd\n/p\n")

    await h.channel.deliver_action(_action(ACTION_REFRESH, {"p": "@1"}))

    # No keystroke goes to the pane — Refresh just re-renders.
    assert h.mux.send_keys_calls == []
    [(anchor, outbound)] = h.channel.edits
    assert anchor.message_id == "m9"
    assert isinstance(outbound.content, DocumentContent)
    assert outbound.content.as_image is True
    assert outbound.content.data.startswith(b"\x89PNG\r\n\x1a\n")
    # Refresh row carries forward — clicking again should still work.
    [refresh] = outbound.content.rows[3]
    assert refresh.action_id == ACTION_REFRESH
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == REFRESH_LABEL


async def test_refresh_pane_gone_acks_without_edit(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness

    await h.channel.deliver_action(_action(ACTION_REFRESH, {"p": "@deleted"}))

    assert h.channel.edits == []
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == PANE_GONE_HINT


async def test_refresh_capture_empty_acks_without_edit(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    # No capture seeded → empty.

    await h.channel.deliver_action(_action(ACTION_REFRESH, {"p": "@1"}))

    assert h.channel.edits == []
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == CAPTURE_FAILED_HINT


async def test_refresh_missing_pane_id_acks(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness

    await h.channel.deliver_action(_action(ACTION_REFRESH, {}))

    assert h.channel.edits == []
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "Invalid refresh"
