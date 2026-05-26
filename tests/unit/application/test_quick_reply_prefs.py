"""QuickReplyPrefs — per-(person, conversation) slot defaults."""

from __future__ import annotations

import pytest

from paige.application.quick_reply_prefs import DEFAULT_SLOTS, QuickReplyPrefs
from paige.domain.conversation import Conversation
from paige.domain.person import Person

ALICE = Person(user_id="alice")
BOB = Person(user_id="bob")
CONV_A = Conversation(chat_id="chat", thread_id="thread-a")
CONV_B = Conversation(chat_id="chat", thread_id="thread-b")


def test_unknown_pair_returns_hardcoded_defaults() -> None:
    """First time we see (person, conversation), the three slots are
    the seeded defaults — no DB / disk lookup."""
    prefs = QuickReplyPrefs()
    assert prefs.get(ALICE, CONV_A) == DEFAULT_SLOTS


def test_update_persists_one_slot_others_keep_default() -> None:
    prefs = QuickReplyPrefs()
    prefs.update(ALICE, CONV_A, 0, "what broke?")
    slots = prefs.get(ALICE, CONV_A)
    assert slots[0] == "what broke?"
    assert slots[1] == DEFAULT_SLOTS[1]
    assert slots[2] == DEFAULT_SLOTS[2]


def test_update_is_isolated_per_person() -> None:
    prefs = QuickReplyPrefs()
    prefs.update(ALICE, CONV_A, 0, "alice-text")
    assert prefs.get(BOB, CONV_A) == DEFAULT_SLOTS


def test_update_is_isolated_per_conversation() -> None:
    prefs = QuickReplyPrefs()
    prefs.update(ALICE, CONV_A, 1, "thread-a-text")
    assert prefs.get(ALICE, CONV_B)[1] == DEFAULT_SLOTS[1]


def test_out_of_range_slot_raises() -> None:
    prefs = QuickReplyPrefs()
    with pytest.raises(IndexError):
        prefs.update(ALICE, CONV_A, 3, "x")
    with pytest.raises(IndexError):
        prefs.update(ALICE, CONV_A, -1, "x")
