"""EndTurnPanelService — slash-command interception in card inputs.

The panel's quick-reply slots and free-form input default to forwarding
the typed text into the bound tmux pane. When the user types a string
that parses as `/<name>` AND `name` matches a registered channel
command, the panel routes through `Channel.dispatch_command` instead —
behaviorally the same as typing the command into the conversation.

Verified here:
- Free input dispatches a known paige command (no pane send_keys).
- Free input falls back to tmux when the name isn't registered.
- Slot input dispatches a known command AND skips quick-reply save.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from paige.application import end_turn_panel as etp
from paige.application.access import AllowList
from paige.application.echo_dedup import EchoDedup
from paige.application.end_turn_panel import (
    ACTION_ACCEPT,
    ACTION_FREE,
    ACTION_SLOT,
    EndTurnPanelService,
)
from paige.application.outbox import Outbox
from paige.application.quick_reply_prefs import QuickReplyPrefs
from paige.application.readiness import ReadinessService
from paige.application.run_registry import RunRegistry
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent
from paige.domain.pane import Binding
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


async def _build() -> tuple[
    EndTurnPanelService,
    FakeChannel,
    FakeMultiplexer,
    QuickReplyPrefs,
    list[tuple[Inbound, str]],
]:
    """Build a panel wired against fakes. Returns the service, fake
    channel, fake multiplexer, the QuickReplyPrefs instance, and a
    recorder list that captures `(inbound, arg)` of every dispatched
    `/sessions` call."""
    ch = FakeChannel()
    outbox = Outbox(ch)
    mux = FakeMultiplexer()
    mux.add_pane("@1", "p", cwd=Path("/tmp"))
    registry = RunRegistry(storage=FakeStorage())
    await registry.bind(ALICE, CONV, pane_id="@1")
    dispatched: list[tuple[Inbound, str]] = []

    async def sessions_handler(inbound: Inbound, arg: str) -> None:
        dispatched.append((inbound, arg))

    ch.on_command("sessions", sessions_handler)
    panel = EndTurnPanelService(
        channel=ch,
        registry=registry,
        outbox=outbox,
        multiplexer=mux,
        echo_dedup=EchoDedup(),
        readiness=ReadinessService(),
        quick_reply=QuickReplyPrefs(),
        allow_list=AllowList(users=(ALICE.user_id,)),
    )
    return panel, ch, mux, panel._quick_reply, dispatched  # type: ignore[attr-defined]


def _event(action_id: str, text: str, slot: str | None = None) -> ActionEvent:
    value: dict[str, str] = {"_input": text}
    if slot is not None:
        value["slot"] = slot
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="card-1"),
        action_id=action_id,
        value=value,
        ack_token="tok",
    )


async def test_free_submit_dispatches_known_command_skipping_pane() -> None:
    panel, ch, mux, _qr, dispatched = await _build()
    await panel._handle_action(_event(ACTION_FREE, "/sessions"))
    # Command handler fired exactly once, with the typed arg.
    assert len(dispatched) == 1
    inbound, arg = dispatched[0]
    assert inbound.sender == ALICE
    assert inbound.conversation == CONV
    assert arg == ""
    # No tmux send.
    assert mux.send_keys_calls == []
    # Ack reflects the dispatch path.
    assert ch.acks and ch.acks[-1][1] == "Sent /sessions"


async def test_free_submit_unknown_command_falls_back_to_pane() -> None:
    panel, ch, mux, _qr, dispatched = await _build()
    await panel._handle_action(_event(ACTION_FREE, "/notathing"))
    # Nothing dispatched; tmux got the literal text.
    assert dispatched == []
    assert len(mux.send_keys_calls) == 1
    call = mux.send_keys_calls[0]
    assert call.text == "/notathing"
    assert call.enter is True


async def test_slot_submit_command_skips_quick_reply_save() -> None:
    panel, ch, mux, qr, dispatched = await _build()
    before = qr.get(ALICE, CONV)
    await panel._handle_action(_event(ACTION_SLOT, "/sessions", slot="0"))
    after = qr.get(ALICE, CONV)
    # Quick-reply slot 0 unchanged — we didn't save the command text.
    assert before == after
    # Command dispatched; no tmux send.
    assert len(dispatched) == 1
    assert mux.send_keys_calls == []


async def test_slot_submit_non_command_saves_and_forwards() -> None:
    panel, _ch, mux, qr, dispatched = await _build()
    await panel._handle_action(_event(ACTION_SLOT, "ship it", slot="1"))
    after = qr.get(ALICE, CONV)
    assert after[1] == "ship it"
    assert dispatched == []
    assert len(mux.send_keys_calls) == 1
    assert mux.send_keys_calls[0].text == "ship it"


# ── ghost-suggestion Accept button ──────────────────────────────────
#
# A live capture (ESC = \x1b) of the `❯ ` prompt showing Claude's grey
# ghost: cursor (`\x1b[7m`) on the first char, faint (`\x1b[2m`) for
# the rest. extract_prompt_suggestion turns it into "yes, ship it".

_GHOST_PANE = "\n".join(
    [
        "  …prior assistant output",
        "\x1b[38;5;246m" + "─" * 40,
        "\x1b[39m❯ \x1b[7my\x1b[0;2mes, ship it\x1b[0m",
        "\x1b[38;5;246m" + "─" * 40,
    ]
)


def _accept_event(text: str) -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="card-1"),
        action_id=ACTION_ACCEPT,
        value={"_input": text},
        ack_token="tok",
    )


async def test_panel_offers_accept_when_ghost_present() -> None:
    panel, ch, mux, _qr, _d = await _build()
    mux.set_capture("@1", _GHOST_PANE)
    await panel._send_panel(Binding(person=ALICE, conversation=CONV, pane_id="@1"), "run-1")
    out = ch.sent[-1]
    assert isinstance(out.content, CardContent)
    card = out.content.card
    # Suggestion is the FIRST input box, pre-filled + editable; quick
    # slots and the free box follow.
    first = card.inputs[0]
    assert first.action_id == ACTION_ACCEPT
    assert first.default_value == "yes, ship it"
    assert card.inputs[1].action_id == ACTION_SLOT  # 1/2/3 follow
    assert card.inputs[-1].action_id == ACTION_FREE  # type box last


async def test_panel_no_accept_when_no_ghost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(etp, "_GHOST_POLL_INTERVAL_S", 0.001)
    panel, ch, mux, _qr, _d = await _build()
    # FakeMultiplexer's default capture is "" → no ghost, no suggestion
    # box. Readiness defaults to NOT ready, so the deferred poll bails.
    await panel._send_panel(Binding(person=ALICE, conversation=CONV, pane_id="@1"), "run-1")
    out = ch.sent[-1]
    assert isinstance(out.content, CardContent)
    actions = [s.action_id for s in out.content.card.inputs]
    assert ACTION_ACCEPT not in actions
    assert actions[0] == ACTION_SLOT  # straight to 1/2/3


async def test_poll_patches_in_accept_when_ghost_appears_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ghost absent at send time, then rendered a beat later → the
    deferred poll edits the live Ready card to add the Accept button."""
    monkeypatch.setattr(etp, "_GHOST_POLL_INTERVAL_S", 0.005)
    panel, ch, mux, _qr, _d = await _build()
    run_id = "run-1"
    panel._readiness._ready[run_id] = True  # READY, so the poll runs
    binding = Binding(person=ALICE, conversation=CONV, pane_id="@1")
    await panel._send_panel(binding, run_id)
    # Initial card went out with no suggestion box (capture was empty).
    assert ch.sent[-1].content.card.inputs[0].action_id == ACTION_SLOT  # type: ignore[union-attr]
    # Ghost shows up; let the poll catch it and patch the card.
    mux.set_capture("@1", _GHOST_PANE)
    await asyncio.sleep(0.05)
    assert ch.edits, "expected a patch adding the suggestion box"
    edited = ch.edits[-1][1].content
    assert isinstance(edited, CardContent)
    assert edited.card.inputs[0].action_id == ACTION_ACCEPT
    assert edited.card.inputs[0].default_value == "yes, ship it"


async def test_poll_bails_when_run_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """If claude resumes (NOT_READY) before the ghost appears, the
    poll must not patch the card — the suggestion would be stale."""
    monkeypatch.setattr(etp, "_GHOST_POLL_INTERVAL_S", 0.005)
    panel, ch, mux, _qr, _d = await _build()
    run_id = "run-1"
    panel._readiness._ready[run_id] = False  # not ready → poll bails first tick
    binding = Binding(person=ALICE, conversation=CONV, pane_id="@1")
    await panel._send_panel(binding, run_id)
    mux.set_capture("@1", _GHOST_PANE)  # ghost present, but run isn't ready
    await asyncio.sleep(0.05)
    assert ch.edits == []


async def test_accept_submit_forwards_ghost_text_to_pane() -> None:
    panel, _ch, mux, _qr, dispatched = await _build()
    await panel._handle_action(_accept_event("yes, ship it"))
    assert dispatched == []  # not a command
    assert len(mux.send_keys_calls) == 1
    assert mux.send_keys_calls[0].text == "yes, ship it"
    assert mux.send_keys_calls[0].enter is True


async def test_accept_submit_empty_text_is_rejected() -> None:
    panel, ch, mux, _qr, _d = await _build()
    await panel._handle_action(_accept_event("   "))
    assert mux.send_keys_calls == []
    assert ch.acks and ch.acks[-1][1] == "Empty suggestion"
