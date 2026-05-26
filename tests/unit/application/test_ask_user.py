"""AskUserService — parse, render, and click-driven picker handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.ask_user import (
    ACTION_PICK,
    TOOL_NAME,
    AskUserService,
    build_card,
    parse_questions,
)
from paige.application.message_seq import MessageSeqService
from paige.application.run_registry import RunRegistry
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


def _input(num_options: int = 3, header: str = "Project") -> str:
    return json.dumps(
        {
            "questions": [
                {
                    "question": "Confirm the project details?",
                    "header": header,
                    "multiSelect": False,
                    "options": [
                        {"label": f"Option {i + 1}", "description": f"desc {i + 1}"}
                        for i in range(num_options)
                    ],
                }
            ]
        }
    )


# ── parse_questions ──────────────────────────────────────────────


def test_parse_well_formed_input() -> None:
    qs = parse_questions(_input(num_options=2))
    assert qs is not None
    assert len(qs) == 1
    q = qs[0]
    assert q.question == "Confirm the project details?"
    assert q.header == "Project"
    assert q.multi_select is False
    assert len(q.options) == 2
    assert q.options[0].label == "Option 1"
    assert q.options[0].description == "desc 1"


def test_parse_returns_none_for_malformed_json() -> None:
    assert parse_questions("{not json") is None


def test_parse_returns_none_for_non_dict() -> None:
    assert parse_questions(json.dumps([1, 2, 3])) is None


def test_parse_returns_none_when_questions_missing() -> None:
    assert parse_questions(json.dumps({"foo": "bar"})) is None


def test_parse_returns_none_when_options_missing() -> None:
    bad = json.dumps({"questions": [{"question": "?", "header": "h"}]})
    assert parse_questions(bad) is None


def test_parse_returns_none_when_options_not_list() -> None:
    bad = json.dumps({"questions": [{"question": "?", "options": "no"}]})
    assert parse_questions(bad) is None


def test_parse_handles_multiple_questions() -> None:
    payload = json.dumps(
        {
            "questions": [
                {"question": "Q1", "options": [{"label": "A"}]},
                {"question": "Q2", "options": [{"label": "B"}, {"label": "C"}]},
            ]
        }
    )
    qs = parse_questions(payload)
    assert qs is not None
    assert len(qs) == 2
    assert qs[1].options[1].label == "C"


# ── build_card ───────────────────────────────────────────────────


def test_build_card_renders_first_question_with_buttons() -> None:
    qs = parse_questions(_input(num_options=3))
    assert qs is not None
    card = build_card("toolu_X", qs)
    assert "Confirm the project details?" in card.text
    # Each option also appears as a numbered paragraph so users see
    # the descriptions without peeking at the TUI.
    assert "**1. Option 1**" in card.text
    assert "**2. Option 2**" in card.text
    assert "**3. Option 3**" in card.text
    assert len(card.rows) == 3
    [b1, b2, b3] = [row[0] for row in card.rows]
    assert b1.label == "Option 1"
    assert b1.action_id == ACTION_PICK
    assert b1.value["tool_id"] == "toolu_X"
    assert b1.value["idx"] == "0"
    # Each button packs enough state to rebuild the post-click card
    # — just the question + label + header, no full body, so click
    # values stay small.
    assert b1.value["label"] == "Option 1"
    assert b1.value["question"] == "Confirm the project details?"
    # Header carries the ❓ prefix so it can never collide with
    # interactive_ui's humanized overlay names.
    assert b1.value["header"] == "❓ Project"
    assert card.header_title == "❓ Project"
    assert "body" not in b1.value  # don't bloat the click payload
    assert b2.value["idx"] == "1"
    assert b3.value["idx"] == "2"


def test_build_card_renders_option_descriptions_in_body() -> None:
    """When Claude provides option descriptions, they ride along in
    the body so paige users see picker context without flipping to
    the TUI. Each option becomes its own paragraph (split by
    blank lines) so the cards layer renders each as a separate
    markdown element — Lark's per-element truncation can't eat the
    tail of a long enumeration."""
    qs = parse_questions(_input(num_options=2))
    assert qs is not None
    card = build_card("toolu_X", qs)
    # `_input` seeds description="desc N" — those should appear.
    assert "**1. Option 1** — desc 1" in card.text
    assert "**2. Option 2** — desc 2" in card.text
    # Body uses blank-line separators (\n\n) between question +
    # options so cards.py can split into multi-md-element form.
    paragraphs = card.text.split("\n\n")
    assert "Confirm the project details?" in paragraphs[0]
    assert any("**1. Option 1**" in p for p in paragraphs)


def test_build_card_skips_dash_when_description_empty() -> None:
    """Don't dangle an em-dash with no description after it."""
    payload = json.dumps(
        {
            "questions": [
                {
                    "question": "Pick one",
                    "options": [{"label": "Just a label", "description": ""}],
                }
            ]
        }
    )
    qs = parse_questions(payload)
    assert qs is not None
    card = build_card("toolu_X", qs)
    assert "**1. Just a label**" in card.text
    assert "—" not in card.text


def test_build_card_uses_header_as_title() -> None:
    qs = parse_questions(_input(header="Restore plan"))
    assert qs is not None
    card = build_card("toolu_X", qs)
    assert card.header_title == "❓ Restore plan"


@pytest.mark.parametrize(
    "raw, expected_norm",
    [
        ("[CONFIRM_PROJECT_DETAILS]", "Confirm project details"),
        ("PICK_NEXT_SLICE", "Pick next slice"),
        ("RESTORE-PLAN", "Restore plan"),
        ("(ENABLED_TOOLS)", "Enabled tools"),
        ("Next slice", "Next slice"),  # already human — untouched
        ("Click test", "Click test"),
        ("", "Question"),  # empty falls back
        ("[]", "Question"),  # empty after stripping brackets
        ("Q1", "Q1"),  # short identifier preserved
    ],
)
def test_build_card_normalizes_tag_style_header(raw: str, expected_norm: str) -> None:
    """Claude's spec uses code-constant headers like `[CONFIRM_X]`;
    the colored card strip needs sentence case to not look like
    placeholder demo text. The `❓ ` prefix sits in front for
    cross-card-type uniqueness."""
    qs = parse_questions(_input(header=raw))
    assert qs is not None
    card = build_card("toolu_X", qs)
    assert card.header_title == f"❓ {expected_norm}"


def test_build_card_falls_back_for_blank_option_label() -> None:
    payload = json.dumps(
        {
            "questions": [
                {
                    "question": "Pick one",
                    "options": [{"label": ""}, {"label": "Nice"}],
                }
            ]
        }
    )
    qs = parse_questions(payload)
    assert qs is not None
    card = build_card("toolu_X", qs)
    assert card.rows[0][0].label == "Option 1"
    assert card.rows[1][0].label == "Nice"


def test_build_card_notes_extra_questions() -> None:
    payload = json.dumps(
        {
            "questions": [
                {"question": "Q1", "options": [{"label": "A"}]},
                {"question": "Q2", "options": [{"label": "B"}]},
                {"question": "Q3", "options": [{"label": "C"}]},
            ]
        }
    )
    qs = parse_questions(payload)
    assert qs is not None
    card = build_card("toolu_X", qs)
    assert "+2 more" in card.text


def test_build_card_handles_empty_questions() -> None:
    card = build_card("toolu_X", ())
    assert TOOL_NAME in card.text
    assert card.rows == ()


# ── AskUserService.click ─────────────────────────────────────────


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    service = AskUserService(
        registry=registry,
        multiplexer=mux,
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
    h.service = service  # type: ignore[attr-defined]
    return h


def _pick_event(idx: int, *, ack_token: str = "ack-1") -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-card"),
        action_id=ACTION_PICK,
        value={
            "tool_id": "toolu_X",
            "idx": str(idx),
            "label": f"Option {idx + 1}",
            "question": "Confirm the project details?",
            "header": "❓ Project",
        },
        ack_token=ack_token,
    )


async def test_pick_first_option_sends_only_enter(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_pick_event(0))

    keys = [(c.pane_id, c.text, c.literal) for c in h.mux.send_keys_calls]
    assert keys == [("@1", "Enter", False)]


async def test_pick_third_option_sends_two_downs_then_enter(
    harness,
) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_pick_event(2))

    keys = [(c.pane_id, c.text, c.literal) for c in h.mux.send_keys_calls]
    assert keys == [
        ("@1", "Down", False),
        ("@1", "Down", False),
        ("@1", "Enter", False),
    ]


async def test_pick_acks_with_choice(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_pick_event(1))

    [(event, ack_text)] = h.channel.acks
    assert event.action_id == ACTION_PICK
    assert ack_text is not None
    assert "Picked" in ack_text


async def test_pick_patches_card_to_drop_buttons_and_show_choice(
    harness,
) -> None:  # type: ignore[no-untyped-def]
    """After a successful pick, the card is edited to remove the
    option buttons and show what was picked. The dispatcher will
    edit again with the real tool_result; this is the interim
    feedback so the user doesn't keep tapping."""
    from paige.domain.outbound import CardContent

    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_action(_pick_event(1))

    [(anchor, outbound)] = h.channel.edits
    assert anchor.message_id == "m-card"
    assert isinstance(outbound.content, CardContent)
    new_card = outbound.content.card
    assert new_card.rows == ()
    # Question text + the chosen label both visible.
    assert "Confirm the project details?" in new_card.text
    assert "Picked: Option 2" in new_card.text
    # Header preserves the ❓ prefix from the original buttoned card.
    assert new_card.header_title == "❓ Project"


async def test_pick_does_not_patch_when_send_keys_fails(
    harness,
) -> None:  # type: ignore[no-untyped-def]
    """If the picker keystrokes can't be delivered we leave the card
    intact so the user can retry, instead of leaving them stranded
    with a "Picked" footer for an action that never happened."""
    h = harness
    # Bind to a phantom pane id — registry returns it, but the
    # multiplexer's send_keys returns False because the pane isn't
    # there. Same shape as a pane that's been closed since the card
    # was sent.
    await h.registry.bind(ALICE, CONV, "@phantom")

    await h.channel.deliver_action(_pick_event(0))

    assert h.channel.edits == []
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "send_keys failed"


async def test_pick_without_binding_acks_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    # No bind. No pane mapping for ALICE/CONV.

    await h.channel.deliver_action(_pick_event(0))

    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "No bound pane" in ack_text
    assert h.mux.send_keys_calls == []


async def test_pick_with_invalid_idx_acks_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    bad = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m"),
        action_id=ACTION_PICK,
        value={"tool_id": "toolu_X", "idx": "not-a-number"},
        ack_token="ack-1",
    )
    await h.channel.deliver_action(bad)

    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "Invalid" in ack_text
    assert h.mux.send_keys_calls == []


async def test_other_action_ids_are_ignored(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    other = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m"),
        action_id="ses:bind",
        value={"pane_id": "@1"},
        ack_token="ack-2",
    )
    await h.channel.deliver_action(other)

    # AskUserService doesn't handle ses:bind, so no send_keys, no ack
    # from us. (Any sessions handler would record its own; here there
    # isn't one, so acks list stays empty.)
    assert h.mux.send_keys_calls == []
    assert h.channel.acks == []
