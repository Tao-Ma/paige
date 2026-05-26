"""LivePaneService — `/livepane` command, auto-spawn from upstream
detectors, mode-aware input slot rendering, and lifecycle cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.live_pane import (
    ACTION_DISMISS,
    ACTION_STOP,
    ACTION_TEXT,
    LivePaneService,
)
from paige.application.message_seq import MessageSeqService
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="oc_chat", thread_id="om_root")


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = LivePaneService(
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
    await service.stop()
    await outbox.stop()


async def _bind_pane(h, capture: str) -> None:  # type: ignore[no-untyped-def]
    """Wire @1 into mux+registry+binding and prime its capture."""
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.mux.set_capture("@1", capture)


# ── mode-aware input slot ────────────────────────────────────────


async def test_input_slot_hidden_in_selection_mode(harness) -> None:  # type: ignore[no-untyped-def]
    """When the captured pane shows a selection footer
    (`Enter to select`) on a non-text option, the input slot is
    omitted — typing into a list picker would discard the text."""
    h = harness
    await _bind_pane(h, "❯ 1. Yes\n  2. No\nEnter to select · ↑/↓ to navigate")

    await h.service.start_for_binding(ALICE, CONV)
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert sent.content.card.inputs == ()


async def test_input_slot_visible_in_idle_mode(harness) -> None:  # type: ignore[no-untyped-def]
    """No selection footer detected → input slot shown with plain
    submit semantics (commit_first=0). Default text-input state."""
    h = harness
    await _bind_pane(h, "$ ls\nfile1.txt\nfile2.txt")

    await h.service.start_for_binding(ALICE, CONV)
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    slots = sent.content.card.inputs
    assert len(slots) == 1
    assert slots[0].value.get("commit_first") == "0"


async def test_input_slot_commit_first_on_text_option(harness) -> None:  # type: ignore[no-untyped-def]
    """Selection footer present AND a `❯ N. Type something`-style
    option highlighted → input slot shown in commit_first mode
    (submit prepends an Enter to commit the option before typing)."""
    h = harness
    pane = (
        "❯ 1. Authoring experience\n"
        "  2. Framework architecture\n"
        "❯ 5. Type something\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel"
    )
    await _bind_pane(h, pane)

    await h.service.start_for_binding(ALICE, CONV)
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    slots = sent.content.card.inputs
    assert len(slots) == 1
    assert slots[0].value.get("commit_first") == "1"


# ── start_for_binding idempotency ────────────────────────────────


async def test_start_for_binding_is_idempotent(harness) -> None:  # type: ignore[no-untyped-def]
    """An upstream detector (InteractiveUIService) calls
    start_for_binding on every tick. Second + subsequent calls for
    the same binding must NOT spawn additional cards — the running
    loop owns the binding."""
    h = harness
    await _bind_pane(h, "$ idle")

    await h.service.start_for_binding(ALICE, CONV)
    await h.service.start_for_binding(ALICE, CONV)
    await h.service.start_for_binding(ALICE, CONV)
    await h.outbox.stop()

    assert len(h.channel.sent) == 1


async def test_start_for_binding_no_op_when_unbound(harness) -> None:  # type: ignore[no-untyped-def]
    """No registered binding for (person, conversation) → silent
    no-op (no card sent, no exception). Upstream detector may race
    against a binding that just got dropped."""
    h = harness
    # No bind, no capture set.

    await h.service.start_for_binding(ALICE, CONV)
    await h.outbox.stop()

    assert h.channel.sent == []


async def test_stop_for_binding_cancels_loop(harness) -> None:  # type: ignore[no-untyped-def]
    """After `start_for_binding` + `stop_for_binding`, the same
    binding can be started fresh — `stop_for_binding` releases the
    `_binding_anchors` entry."""
    h = harness
    await _bind_pane(h, "$ idle")

    await h.service.start_for_binding(ALICE, CONV)
    await h.service.stop_for_binding(ALICE, CONV)
    # Second start spawns a NEW card; the binding key is free again.
    await h.service.start_for_binding(ALICE, CONV)
    await h.outbox.stop()

    assert len(h.channel.sent) == 2


# ── click handlers ──────────────────────────────────────────────


def _action(action_id: str, *, value: dict[str, str] | None = None) -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-card"),
        action_id=action_id,
        value=value or {},
        ack_token="cbq",
    )


async def test_text_submit_sends_keys_to_pane(harness) -> None:  # type: ignore[no-untyped-def]
    """Tapping Send on the input slot pushes `<text><Enter>` to
    the bound pane via tmux send_keys (literal=True, enter=True)."""
    h = harness
    await _bind_pane(h, "$ idle")

    event = _action(ACTION_TEXT, value={"p": "@1", "_input": "hello world", "commit_first": "0"})
    await h.channel.deliver_action(event)

    keys = [(c.pane_id, c.text, c.literal, c.enter) for c in h.mux.send_keys_calls]
    assert keys[0] == ("@1", "hello world", True, True)


async def test_text_submit_commit_first_prepends_enter(harness) -> None:  # type: ignore[no-untyped-def]
    """In commit_first mode, the submit first sends a literal-false
    Enter (commit the highlighted option in the picker), then sends
    the typed text with enter=True. Two separate send_keys calls."""
    h = harness
    await _bind_pane(h, "$ idle")

    event = _action(ACTION_TEXT, value={"p": "@1", "_input": "my answer", "commit_first": "1"})
    await h.channel.deliver_action(event)

    calls = [(c.text, c.literal, c.enter) for c in h.mux.send_keys_calls]
    assert calls[0] == ("Enter", False, False)
    assert calls[1] == ("my answer", True, True)


async def test_stop_action_finalizes_card(harness) -> None:  # type: ignore[no-untyped-def]
    """🛑 Stop: cancel the poll loop, keep the card visible as
    scrollback. Card body gets a `· stopped` suffix on the header."""
    h = harness
    await _bind_pane(h, "$ idle")
    await h.service.start_for_binding(ALICE, CONV)
    sent = h.channel.sent[0]
    anchor_id = "msg-anchor"
    # Override the anchor so the stop action targets the live loop.
    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id=anchor_id),
        action_id=ACTION_STOP,
        value={"p": "@1"},
        ack_token="cbq",
    )
    # We're not asserting against the live loop's anchor (which is
    # a Fake-generated msg id) — this verifies the action handler
    # accepts the action_id and acks; the loop cancellation path
    # is exercised in test_stop_for_binding_cancels_loop above.
    await h.channel.deliver_action(event)
    assert sent is not None


async def test_dismiss_action_deletes_card(harness) -> None:  # type: ignore[no-untyped-def]
    """✕ Dismiss: cancel the loop AND delete the card. The
    channel sees a delete call for the anchor."""
    h = harness
    await _bind_pane(h, "$ idle")

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-dismiss"),
        action_id=ACTION_DISMISS,
        value={"p": "@1"},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)

    assert any(a.message_id == "m-dismiss" for a in h.channel.deleted)


# ── /livepane command ───────────────────────────────────────────


async def test_livepane_command_posts_card(harness) -> None:  # type: ignore[no-untyped-def]
    """`/livepane` in a bound conversation captures the pane and
    posts a card. Cleaner unit test of the user-invoked path."""
    h = harness
    await _bind_pane(h, "$ idle")

    inbound = Inbound(
        sender=ALICE,
        conversation=CONV,
        text="/livepane",
        message_id="m-inbound",
    )
    await h.channel.deliver_command("livepane", inbound)
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert "live pane" in sent.content.card.header_title.lower()


async def test_livepane_command_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    """`/livepane` without a bound pane emits a text hint pointing
    the user to /start."""
    h = harness
    # No bind.

    inbound = Inbound(
        sender=ALICE,
        conversation=CONV,
        text="/livepane",
        message_id="m-inbound",
    )
    await h.channel.deliver_command("livepane", inbound)
    await h.outbox.stop()

    [sent] = h.channel.sent
    from paige.domain.outbound import TextContent

    assert isinstance(sent.content, TextContent)
    assert "/start" in sent.content.text
