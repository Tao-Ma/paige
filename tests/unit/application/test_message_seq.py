"""MessageSeqService — toggle, stamp_send, stamp_edit, chain bookkeeping."""

from __future__ import annotations

from paige.application.message_seq import MessageSeqService, _format_footer
from paige.domain.card import Card
from paige.domain.conversation import Anchor, Conversation
from paige.domain.outbound import (
    CardContent,
    DocumentContent,
    Outbound,
    TextContent,
    TypingContent,
)
from paige.domain.person import Person

ALICE = Person(user_id="u-alice", display_name="Alice")
BOB = Person(user_id="u-bob", display_name="Bob")
CONV_A = Conversation(chat_id="-100", thread_id="42")
CONV_B = Conversation(chat_id="-101", thread_id="9")


def _text(s: str = "hi") -> Outbound:
    return Outbound(conversation=CONV_A, content=TextContent(s))


def _card(s: str = "body") -> Outbound:
    return Outbound(conversation=CONV_A, content=CardContent(card=Card(text=s)))


def _anchor(message_id: str = "om_1") -> Anchor:
    return Anchor(conversation=CONV_A, message_id=message_id)


# ── toggle ──────────────────────────────────────────────────────


def test_toggle_starts_off() -> None:
    svc = MessageSeqService()
    assert svc.is_enabled(ALICE, CONV_A) is False


def test_toggle_flips() -> None:
    svc = MessageSeqService()
    assert svc.toggle(ALICE, CONV_A) is True
    assert svc.is_enabled(ALICE, CONV_A) is True
    assert svc.toggle(ALICE, CONV_A) is False


def test_toggle_per_person_conversation() -> None:
    """Two users in the same conversation can independently toggle."""
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)
    assert svc.is_enabled(ALICE, CONV_A) is True
    assert svc.is_enabled(BOB, CONV_A) is False


# ── stamp_send: disabled = no-op ─────────────────────────────────


def test_stamp_send_disabled_returns_unchanged() -> None:
    svc = MessageSeqService()
    out = _text("hello")
    stamped, seq = svc.stamp_send(ALICE, CONV_A, out)
    assert stamped is out
    assert seq is None


# ── stamp_send: enabled stamps + allocates seq ───────────────────


def test_stamp_send_text_appends_footer_and_returns_seq() -> None:
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)
    stamped, seq = svc.stamp_send(ALICE, CONV_A, _text("hello"))
    assert seq == 1
    assert isinstance(stamped.content, TextContent)
    assert stamped.content.text.endswith("_seq #1_")
    assert "hello" in stamped.content.text


def test_stamp_send_card_appends_footer_to_card_text() -> None:
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)
    stamped, seq = svc.stamp_send(ALICE, CONV_A, _card("body"))
    assert seq == 1
    assert isinstance(stamped.content, CardContent)
    assert stamped.content.card.text.endswith("_seq #1_")
    assert "body" in stamped.content.card.text


def test_stamp_send_document_returns_unchanged() -> None:
    """DocumentContent has no body field — image bytes only — so no
    footer can be rendered. The chain bookkeeping still records the
    seq via record_send_anchor for any text-content edit later."""
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)
    out = Outbound(
        conversation=CONV_A,
        content=DocumentContent(data=b"x", filename="x.png", as_image=True),
    )
    stamped, seq = svc.stamp_send(ALICE, CONV_A, out)
    assert stamped is out  # unchanged
    assert seq == 1  # seq still consumed for chain consistency


def test_stamp_send_typing_returns_unchanged() -> None:
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)
    out = Outbound(conversation=CONV_A, content=TypingContent())
    stamped, seq = svc.stamp_send(ALICE, CONV_A, out)
    assert stamped is out
    assert seq == 1


# ── per-conversation counter ─────────────────────────────────────


def test_counter_is_per_conversation() -> None:
    """Different conversations have independent counters."""
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)
    svc.toggle(BOB, CONV_B)

    _, a1 = svc.stamp_send(ALICE, CONV_A, _text("a"))
    _, b1 = svc.stamp_send(BOB, CONV_B, _text("b"))
    _, a2 = svc.stamp_send(ALICE, CONV_A, _text("a2"))

    assert a1 == 1
    assert b1 == 1  # separate counter
    assert a2 == 2


# ── stamp_edit: chain extends ────────────────────────────────────


def test_stamp_edit_chains_after_recorded_send() -> None:
    """A send followed by an edit on the same anchor extends the
    chain — the edit footer shows the full history."""
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)

    _, send_seq = svc.stamp_send(ALICE, CONV_A, _text("v1"))
    assert send_seq is not None
    anchor = _anchor("om_1")
    svc.record_send_anchor(anchor, send_seq)

    edit_out = _text("v2")
    stamped, edit_seq = svc.stamp_edit(ALICE, CONV_A, anchor, edit_out)

    assert edit_seq == 2
    assert isinstance(stamped.content, TextContent)
    # Consecutive chain [1,2] collapses to a single range, no bracket.
    assert stamped.content.text.endswith("_seq #1–#2_")


def test_stamp_edit_without_recorded_send_starts_chain_at_edit() -> None:
    """Edit on an unknown anchor (e.g. send happened with stamping
    off) becomes the chain root."""
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)

    stamped, seq = svc.stamp_edit(ALICE, CONV_A, _anchor("om_x"), _text("v1"))
    assert seq == 1
    assert isinstance(stamped.content, TextContent)
    assert stamped.content.text.endswith("_seq #1_")  # singleton chain


def test_stamp_edit_chain_grows_across_multiple_edits() -> None:
    svc = MessageSeqService()
    svc.toggle(ALICE, CONV_A)

    _, send_seq = svc.stamp_send(ALICE, CONV_A, _text("v1"))
    anchor = _anchor("om_1")
    svc.record_send_anchor(anchor, send_seq or 0)

    svc.stamp_edit(ALICE, CONV_A, anchor, _text("v2"))
    stamped, _ = svc.stamp_edit(ALICE, CONV_A, anchor, _text("v3"))

    assert isinstance(stamped.content, TextContent)
    # Consecutive chain [1,2,3] collapses to one range.
    assert stamped.content.text.endswith("_seq #1–#3_")


# ── footer compaction ────────────────────────────────────────────


def test_footer_singleton() -> None:
    assert _format_footer([3]) == "_seq #3_"


def test_footer_consecutive_run_drops_bracket() -> None:
    assert _format_footer([3, 4, 5, 6]) == "_seq #3–#6_"


def test_footer_consecutive_pair() -> None:
    assert _format_footer([3, 4]) == "_seq #3–#4_"


def test_footer_all_gaps_keeps_arrows() -> None:
    assert _format_footer([3, 5, 7]) == "_seq #7 [#3 → #5 → #7]_"


def test_footer_mixed_runs_and_gaps() -> None:
    assert _format_footer([3, 4, 6, 7, 8]) == "_seq #8 [#3–#4 → #6–#8]_"


def test_footer_run_then_isolated() -> None:
    assert _format_footer([3, 4, 5, 9]) == "_seq #9 [#3–#5 → #9]_"


# ── stamp_edit: disabled = no-op, no chain growth ────────────────


def test_stamp_edit_disabled_does_not_grow_chain() -> None:
    svc = MessageSeqService()
    # Never toggle on.
    stamped, seq = svc.stamp_edit(ALICE, CONV_A, _anchor("om_1"), _text("v1"))
    assert stamped.content is _text("v1").content or isinstance(stamped.content, TextContent)
    assert seq is None
