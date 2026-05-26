"""Pane + Binding."""

from pathlib import Path

from paige.domain.conversation import Conversation
from paige.domain.pane import Binding, Pane
from paige.domain.person import Person


def test_pane_required_fields() -> None:
    p = Pane(pane_id="@0", pane_name="proj", cwd=Path("/proj"))
    assert p.pane_id == "@0"
    assert p.pane_name == "proj"
    assert p.cwd == Path("/proj")
    assert p.multiplexer_session == ""


def test_pane_with_multiplexer_session() -> None:
    p = Pane(
        pane_id="@5",
        pane_name="claude",
        cwd=Path("/work"),
        multiplexer_session="paige",
    )
    assert p.multiplexer_session == "paige"


def test_binding_links_topic_to_pane() -> None:
    b = Binding(
        person=Person(user_id="u1"),
        conversation=Conversation(chat_id="oc", thread_id="om_root"),
        pane_id="@7",
    )
    assert b.pane_id == "@7"
    assert b.person.user_id == "u1"
    assert b.conversation.thread_id == "om_root"
