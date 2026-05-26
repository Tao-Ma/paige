"""VerbosityService — per-(person, conversation, ContentKind) BRIEF/FULL."""

from __future__ import annotations

from paige.application.verbosity import (
    ContentKind,
    Verbosity,
    VerbosityService,
)
from paige.domain.conversation import Conversation
from paige.domain.person import Person

ALICE = Person(user_id="u-alice", display_name="Alice")
BOB = Person(user_id="u-bob")
CONV_A = Conversation(chat_id="-100", thread_id="42")
CONV_B = Conversation(chat_id="-100", thread_id="43")
CONV_DM = Conversation(chat_id="oc-1")


def test_default_is_full() -> None:
    s = VerbosityService()
    assert s.get(ALICE, CONV_A, ContentKind.TEXT) is Verbosity.FULL
    assert s.get(ALICE, CONV_A, ContentKind.TOOL_USE) is Verbosity.FULL
    assert s.get(ALICE, CONV_A, ContentKind.TOOL_RESULT) is Verbosity.FULL


def test_default_can_be_overridden() -> None:
    s = VerbosityService(default=Verbosity.BRIEF)
    assert s.get(ALICE, CONV_A, ContentKind.TEXT) is Verbosity.BRIEF


def test_set_then_get() -> None:
    s = VerbosityService(default=Verbosity.BRIEF)
    s.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.FULL)
    assert s.get(ALICE, CONV_A, ContentKind.TEXT) is Verbosity.FULL
    # Other kinds untouched.
    assert s.get(ALICE, CONV_A, ContentKind.TOOL_USE) is Verbosity.BRIEF


def test_toggle_returns_new_value() -> None:
    s = VerbosityService(default=Verbosity.BRIEF)
    new = s.toggle(ALICE, CONV_A, ContentKind.TEXT)
    assert new is Verbosity.FULL
    assert s.get(ALICE, CONV_A, ContentKind.TEXT) is Verbosity.FULL
    new2 = s.toggle(ALICE, CONV_A, ContentKind.TEXT)
    assert new2 is Verbosity.BRIEF


def test_per_person_isolation() -> None:
    s = VerbosityService(default=Verbosity.BRIEF)
    s.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.FULL)
    assert s.get(BOB, CONV_A, ContentKind.TEXT) is Verbosity.BRIEF


def test_per_conversation_isolation() -> None:
    """Same person, different conversations → different state.
    Picture an Alice with mobile session FULL + CI session BRIEF."""
    s = VerbosityService(default=Verbosity.BRIEF)
    s.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.FULL)
    assert s.get(ALICE, CONV_B, ContentKind.TEXT) is Verbosity.BRIEF


def test_dm_conversation_no_thread() -> None:
    """thread_id=None is a distinct binding from any non-None."""
    s = VerbosityService(default=Verbosity.BRIEF)
    s.set(ALICE, CONV_DM, ContentKind.TEXT, Verbosity.FULL)
    assert s.get(ALICE, CONV_A, ContentKind.TEXT) is Verbosity.BRIEF
    assert s.get(ALICE, CONV_DM, ContentKind.TEXT) is Verbosity.FULL


# ── truncation ──────────────────────────────────────────────────


def test_maybe_truncate_passes_short_text_through() -> None:
    s = VerbosityService(default=Verbosity.BRIEF, brief_chars=200)
    text = "short message"
    out = s.maybe_truncate(ALICE, CONV_A, ContentKind.TEXT, text)
    assert out == text


def test_maybe_truncate_brief_clips_long_text() -> None:
    s = VerbosityService(default=Verbosity.BRIEF, brief_chars=20)
    text = "a" * 100
    out = s.maybe_truncate(ALICE, CONV_A, ContentKind.TEXT, text)
    assert out.startswith("a" * 20)
    assert "truncated" in out
    assert "80" in out  # 100 - 20


def test_maybe_truncate_full_passes_through() -> None:
    s = VerbosityService(default=Verbosity.BRIEF, brief_chars=20)
    s.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.FULL)
    text = "a" * 100
    out = s.maybe_truncate(ALICE, CONV_A, ContentKind.TEXT, text)
    assert out == text


def test_maybe_truncate_kind_specific() -> None:
    """Only the configured kind is affected."""
    s = VerbosityService(default=Verbosity.BRIEF, brief_chars=20)
    s.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.FULL)
    long = "x" * 100
    # TEXT is FULL
    assert s.maybe_truncate(ALICE, CONV_A, ContentKind.TEXT, long) == long
    # TOOL_USE still BRIEF (default)
    out = s.maybe_truncate(ALICE, CONV_A, ContentKind.TOOL_USE, long)
    assert "truncated" in out


def test_default_full_does_not_truncate() -> None:
    """Default-FULL: even a tiny brief_chars cap is moot."""
    s = VerbosityService(brief_chars=5)
    text = "a" * 100
    out = s.maybe_truncate(ALICE, CONV_A, ContentKind.TEXT, text)
    assert out == text
