"""StatusCarrierService — badge migration scoped to agent cards.

Verifies the `is_status_carrier` gate on `_on_send_complete`:
- A flagged card adopts the carrier role and receives the badge.
- An unflagged card lands without disturbing the current carrier
  (sidebar commands / server menus don't steal the badge).
- An edit on the current carrier anchor stays carrier regardless
  of the new card's flag value (post-pick edits, panel morphs).
"""

from __future__ import annotations

from paige.application.outbox import Outbox
from paige.application.status_carrier import StatusCarrierService
from paige.domain.card import Card
from paige.domain.conversation import Conversation
from paige.domain.outbound import CardContent, Outbound
from paige.domain.pane import Binding
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


def _binding() -> Binding:
    mux = FakeMultiplexer()
    pane = mux.add_pane("@1", "p", cwd=__import__("pathlib").Path("/tmp"))
    return Binding(person=ALICE, conversation=CONV, pane_id=pane.pane_id)


def _carrier_card(text: str = "agent") -> Card:
    return Card(text=text, is_status_carrier=True)


def _plain_card(text: str = "menu") -> Card:
    return Card(text=text)


async def _setup() -> tuple[StatusCarrierService, Outbox, FakeChannel]:
    ch = FakeChannel()
    outbox = Outbox(ch)
    svc = StatusCarrierService(outbox=outbox)
    svc.install()
    return svc, outbox, ch


def _card_outbound(card: Card) -> Outbound:
    return Outbound(conversation=CONV, content=CardContent(card))


async def test_carrier_card_adopts_and_receives_badge() -> None:
    svc, outbox, ch = await _setup()
    await svc.on_status_change(_binding(), "Worked 5s")
    fut = outbox.enqueue_send(ALICE, _card_outbound(_carrier_card()))
    await fut
    # The send_complete handler runs after the future resolves;
    # drain the outbox to ensure the follow-up PATCH lands too.
    await outbox.stop()
    # First send + one badge PATCH.
    assert len(ch.sent) == 1
    assert len(ch.edits) == 1
    patched_card = ch.edits[0][1].content.card  # type: ignore[union-attr]
    assert patched_card.status_badge == "Worked 5s"


async def test_non_carrier_card_does_not_steal_badge() -> None:
    svc, outbox, ch = await _setup()
    await svc.on_status_change(_binding(), "Worked 5s")
    # First send: a real agent card becomes carrier.
    await outbox.enqueue_send(ALICE, _card_outbound(_carrier_card("a")))
    # Second send: a non-carrier sidebar card lands.
    await outbox.enqueue_send(ALICE, _card_outbound(_plain_card("menu")))
    await outbox.stop()
    # Edits we issued: one badge stamp on the agent card. The plain
    # card should not have caused a strip-badge PATCH on the prior
    # carrier, nor a stamp on itself.
    badge_targets = [a.message_id for a, _ in ch.edits]
    agent_anchor_id = "1001"  # FakeChannel starts at 1000 and increments
    assert badge_targets == [agent_anchor_id]


async def test_edit_on_carrier_anchor_preserves_carrier_regardless_of_flag() -> None:
    svc, outbox, ch = await _setup()
    await svc.on_status_change(_binding(), "Worked 9s")
    fut = outbox.enqueue_send(ALICE, _card_outbound(_carrier_card()))
    anchor = await fut
    assert anchor is not None
    # External edit on the same anchor with a card that LACKS the
    # flag (mimics an ask_user post-pick rebuild). Should still be
    # treated as the carrier — badge re-applied via PATCH.
    ch.edits.clear()
    await outbox.enqueue_edit(ALICE, anchor, _card_outbound(_plain_card("picked")))
    await outbox.stop()
    # First entry is the caller's edit, the trailing edit(s) are the
    # carrier service's badge re-stamp. We should see at least one
    # PATCH carrying the badge text.
    badge_texts = [
        e[1].content.card.status_badge  # type: ignore[union-attr]
        for e in ch.edits
        if isinstance(e[1].content, CardContent)
    ]
    assert "Worked 9s" in badge_texts
