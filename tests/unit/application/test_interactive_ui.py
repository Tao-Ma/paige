"""InteractiveUIService — pane-scrape detection + click → keystrokes."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.interactive_ui import (
    ACTION_KEY_DOWN,
    ACTION_KEY_ENTER,
    ACTION_KEY_ESC,
    ACTION_KEY_UP,
    ACTION_OPTION,
    ACTION_REFRESH,
    InteractiveUIService,
)
from paige.application.message_seq import MessageSeqService
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.outbound import CardContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


PERMISSION_PANE = "\n".join(
    [
        "Do you want to proceed?",
        "❯ 1. Yes",
        "  2. Yes, and remember",
        "  3. No",
        "Esc to cancel",
    ]
)


ASKUSER_PANE = "\n".join(
    [
        "What's the deployment target?",
        "☐ Staging",
        "☐ Production",
        "Enter to select",
    ]
)


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = InteractiveUIService(
        multiplexer=mux,
        registry=registry,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        message_seq=MessageSeqService(),
        poll_interval=0.05,
        idle_debounce=2,
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


# ── tick: detect → send ────────────────────────────────────────


async def _setup_bound_pane(h, capture: str) -> None:  # type: ignore[no-untyped-def]
    """Wire @1 into mux+registry+binding and prime its capture.

    `list_panes()` only returns panes with registered runs, so tests
    that drive `tick()` must `register_run` in addition to `bind`.
    """
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.mux.set_capture("@1", capture)


async def test_tick_sends_card_when_permission_prompt_visible(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await _setup_bound_pane(h, PERMISSION_PANE)

    await h.service.tick()
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert "1. Yes" in sent.content.card.text
    # Numbered options → tap-to-pick rows + trailer.
    rows = sent.content.card.rows
    assert any(b.action_id == ACTION_OPTION for row in rows for b in row)
    # Header is humanized — "PermissionPrompt" → "Permission prompt"
    # so the colored card strip doesn't read like a code identifier.
    assert sent.content.card.header_title == "Permission prompt"


async def test_tick_no_card_when_no_ui_visible(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await _setup_bound_pane(h, "Just normal output\nno overlay\n")

    await h.service.tick()
    await h.outbox.stop()

    assert h.channel.sent == []


async def test_tick_no_iui_card_for_askuser(harness) -> None:  # type: ignore[no-untyped-def]
    """AskUserQuestion is detected by the pane scraper but the
    iui card path doesn't render its own card for it — when a
    `LivePaneService` is wired (production), the detection delegates
    to `livepane.start_for_binding`. In tests with no live_pane
    wired, the iui side just sends nothing (no fallback card),
    leaving the JSONL renderer (`paige.application.ask_user`) as
    the late-arriving safety net."""
    h = harness
    await _setup_bound_pane(h, ASKUSER_PANE)

    await h.service.tick()
    await h.outbox.stop()

    assert h.channel.sent == []


async def test_tick_dedups_unchanged_content(harness) -> None:  # type: ignore[no-untyped-def]
    """Same pane content on consecutive ticks → no edit churn."""
    h = harness
    await _setup_bound_pane(h, PERMISSION_PANE)

    await h.service.tick()
    await h.service.tick()
    await h.outbox.stop()

    assert len(h.channel.sent) == 1  # only one card sent
    assert h.channel.edits == []  # no edit churn


async def test_card_deleted_after_idle_debounce(harness) -> None:  # type: ignore[no-untyped-def]
    """When the UI clears, the card sticks for `idle_debounce` ticks
    then gets deleted. Avoids flicker on transient pane redraws."""
    h = harness
    await _setup_bound_pane(h, PERMISSION_PANE)
    await h.service.tick()  # send card

    h.mux.set_capture("@1", "no overlay\n")
    await h.service.tick()  # 1st miss — debounce
    assert h.channel.deleted == []
    await h.service.tick()  # 2nd miss — fires
    await h.outbox.stop()
    assert len(h.channel.deleted) == 1


# ── click handlers ─────────────────────────────────────────────


def _event(action_id: str, *, value: dict[str, str] | None = None) -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-card"),
        action_id=action_id,
        value=value or {},
        ack_token="cbq",
    )


async def test_click_arrow_sends_named_key(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_event(ACTION_KEY_DOWN))

    keys = [(c.pane_id, c.text, c.literal) for c in h.mux.send_keys_calls]
    assert keys[0] == ("@1", "Down", False)


async def test_click_enter_sends_named_key(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_event(ACTION_KEY_ENTER))

    keys = [(c.pane_id, c.text, c.literal) for c in h.mux.send_keys_calls]
    assert keys[0] == ("@1", "Enter", False)


async def test_click_option_sends_literal_digit(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_event(ACTION_OPTION, value={"num": "2"}))

    keys = [(c.pane_id, c.text, c.literal, c.enter) for c in h.mux.send_keys_calls]
    # Literal digit, no Enter — Claude Code 2.x's numbered menu
    # accepts the digit alone to commit the choice.
    assert keys[0] == ("@1", "2", True, False)


async def test_click_option_patches_card_to_drop_buttons(harness) -> None:  # type: ignore[no-untyped-def]
    """Numbered option clicks finalize a choice. The card is patched
    to drop the buttons and append a "✓ Sent: #N" footer so the user
    gets immediate feedback, riding the inline-refresh slot for
    atomic Feishu repaint. Otherwise the user stares at stale buttons
    while the outbox PATCH path tries (and often fails on the
    clicker)."""
    h = harness
    await _setup_bound_pane(h, PERMISSION_PANE)
    # Tick once to send the initial card and seed state.anchor.
    await h.service.tick()
    [original] = h.channel.sent

    # User taps option 2.
    await h.channel.deliver_action(_event(ACTION_OPTION, value={"num": "2"}))

    # Keystroke went to the pane.
    assert any(c.text == "2" for c in h.mux.send_keys_calls)
    # Card was edited via the channel directly (inline-refresh slot).
    [(anchor, outbound)] = h.channel.edits
    assert anchor.message_id == original.message_id if hasattr(original, "message_id") else True
    assert isinstance(outbound.content, CardContent)
    new_card = outbound.content.card
    assert new_card.rows == ()  # buttons gone
    assert "✓ Sent: #2" in new_card.text
    # Original TUI body preserved for context.
    assert "1. Yes" in new_card.text
    # Header still humanized (not the literal "Interactive UI").
    assert new_card.header_title == "Permission prompt"


async def test_picked_card_is_not_deleted_when_overlay_clears(harness) -> None:  # type: ignore[no-untyped-def]
    """After a click finalizes the choice the TUI overlay usually
    disappears (the bash command starts running, etc.). The polling
    loop's idle-debounce must NOT delete the patched "✓ Sent" card
    — that would leave a Feishu "撤回" tombstone in place of the
    user's record of what they picked."""
    h = harness
    await _setup_bound_pane(h, PERMISSION_PANE)
    await h.service.tick()  # initial card

    await h.channel.deliver_action(_event(ACTION_OPTION, value={"num": "2"}))
    assert len(h.channel.edits) == 1  # patched

    # Overlay disappears; debounce idle_misses past threshold.
    h.mux.set_capture("@1", "no overlay\n")
    await h.service.tick()
    await h.service.tick()
    await h.outbox.stop()

    # No delete on the patched card — the user's "✓ Sent" record
    # stays in the thread.
    assert h.channel.deleted == []


async def test_click_refresh_does_not_send_keys(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.mux.set_capture("@1", PERMISSION_PANE)

    await h.channel.deliver_action(_event(ACTION_REFRESH))
    await h.outbox.stop()

    # Refresh re-captures and re-sends if a UI is present, but doesn't
    # send any keystrokes.
    assert h.mux.send_keys_calls == []


async def test_click_without_binding_acks_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    # No bind — sender/conv resolve to no pane.

    await h.channel.deliver_action(_event(ACTION_KEY_UP))

    [(_event_, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "No bound pane" in ack_text
    assert h.mux.send_keys_calls == []


async def test_unknown_action_id_is_ignored(harness) -> None:  # type: ignore[no-untyped-def]
    """Other services' action ids (e.g. ses:bind) must not get
    swallowed by the InteractiveUIService handler."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_event("ses:bind", value={"pane_id": "@1"}))

    assert h.mux.send_keys_calls == []
    assert h.channel.acks == []


async def test_option_click_with_invalid_num_acks_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_event(ACTION_OPTION, value={"num": "abc"}))

    [(_event_, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "Invalid" in ack_text
    assert h.mux.send_keys_calls == []


async def test_arrow_click_acks_with_key_label(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_event(ACTION_KEY_ESC))

    [(_event_, ack_text)] = h.channel.acks
    assert ack_text == "Escape"
