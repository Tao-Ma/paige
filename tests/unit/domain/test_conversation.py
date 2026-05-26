"""Conversation + Anchor — chat scoping, threaded replies."""

from paige.domain.conversation import Anchor, Conversation


def test_conversation_no_thread_means_chat_root() -> None:
    c = Conversation(chat_id="oc_xyz")
    assert c.chat_id == "oc_xyz"
    assert c.thread_id is None


def test_conversation_with_thread_id() -> None:
    c = Conversation(chat_id="oc_xyz", thread_id="om_root")
    assert c.thread_id == "om_root"


def test_conversation_value_equality() -> None:
    a = Conversation(chat_id="x", thread_id="y")
    b = Conversation(chat_id="x", thread_id="y")
    assert a == b
    assert hash(a) == hash(b)


def test_conversation_thread_id_distinguishes() -> None:
    """Two conversations with the same chat_id but different threads
    must NOT compare equal — they're distinct topics."""
    a = Conversation(chat_id="x", thread_id="t1")
    b = Conversation(chat_id="x", thread_id="t2")
    assert a != b


def test_anchor_holds_conversation_and_message_id() -> None:
    c = Conversation(chat_id="x", thread_id="t")
    a = Anchor(conversation=c, message_id="om_msg")
    assert a.conversation is c
    assert a.message_id == "om_msg"
