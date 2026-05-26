"""Card / Action / ActionEvent — interactive surfaces."""

from paige.domain.card import Action, ActionEvent, Card
from paige.domain.conversation import Anchor, Conversation
from paige.domain.person import Person


def test_action_required_fields() -> None:
    a = Action(label="Yes", action_id="confirm")
    assert a.label == "Yes"
    assert a.action_id == "confirm"
    assert a.value == {}


def test_action_with_value() -> None:
    a = Action(label="Pick", action_id="pick", value={"idx": "3"})
    assert a.value == {"idx": "3"}


def test_card_text_only() -> None:
    c = Card(text="hello")
    assert c.text == "hello"
    assert c.rows == ()
    assert c.header_title is None
    assert c.header_color is None


def test_card_with_buttons_grid() -> None:
    rows = (
        (Action("a", "cb:a"), Action("b", "cb:b")),
        (Action("c", "cb:c"),),
    )
    c = Card(text="pick one", rows=rows)
    assert len(c.rows) == 2
    assert len(c.rows[0]) == 2
    assert c.rows[0][0].label == "a"


def test_card_header() -> None:
    c = Card(text="x", header_title="⚙ Prefs", header_color="violet")
    assert c.header_title == "⚙ Prefs"
    assert c.header_color == "violet"


def test_action_event_carries_anchor_and_token() -> None:
    conv = Conversation(chat_id="oc", thread_id="om_root")
    anchor = Anchor(conversation=conv, message_id="om_card")
    ev = ActionEvent(
        sender=Person(user_id="u1"),
        conversation=conv,
        card_anchor=anchor,
        action_id="confirm",
        value={"k": "v"},
        ack_token="opaque-token-xxx",
    )
    assert ev.card_anchor is anchor
    assert ev.action_id == "confirm"
    assert ev.value == {"k": "v"}
    assert ev.ack_token == "opaque-token-xxx"
