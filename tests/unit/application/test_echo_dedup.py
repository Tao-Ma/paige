"""EchoDedup — recent send-keys ring buffer for IM echo suppression."""

from __future__ import annotations

from time import monotonic

import pytest

from paige.application.echo_dedup import EchoDedup


def test_record_then_match_consumes_entry() -> None:
    d = EchoDedup()
    d.record("@1", "hello")
    assert d.is_echo("@1", "hello") is True
    assert d.is_echo("@1", "hello") is False  # consumed


def test_no_match_for_different_pane() -> None:
    d = EchoDedup()
    d.record("@1", "hi")
    assert d.is_echo("@2", "hi") is False


def test_no_match_for_different_text() -> None:
    d = EchoDedup()
    d.record("@1", "hi")
    assert d.is_echo("@1", "bye") is False


def test_normalization_strips_and_collapses_whitespace() -> None:
    d = EchoDedup()
    d.record("@1", "  hello   world  \n")
    assert d.is_echo("@1", "hello world") is True


def test_match_pops_oldest_first_for_repeated_text() -> None:
    """When the same (pane, text) was recorded twice, two matches
    are needed to consume both — protects against false negatives
    when the user actually types the same line twice."""
    d = EchoDedup()
    d.record("@1", "ls")
    d.record("@1", "ls")
    assert d.is_echo("@1", "ls") is True
    assert d.is_echo("@1", "ls") is True
    assert d.is_echo("@1", "ls") is False


def test_ttl_expires_old_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = monotonic()

    def now() -> float:
        return fake_time

    import paige.application.echo_dedup as mod

    monkeypatch.setattr(mod, "monotonic", now)

    d = EchoDedup(ttl_seconds=2.0)
    d.record("@1", "x")
    fake_time += 5.0  # advance past TTL
    assert d.is_echo("@1", "x") is False


def test_max_entries_drops_oldest() -> None:
    d = EchoDedup(max_entries=3)
    for i in range(5):
        d.record("@1", f"msg-{i}")
    # First two entries should be evicted.
    assert d.is_echo("@1", "msg-0") is False
    assert d.is_echo("@1", "msg-1") is False
    # Last three remain.
    assert d.is_echo("@1", "msg-4") is True
    assert d.is_echo("@1", "msg-3") is True
    assert d.is_echo("@1", "msg-2") is True


def test_record_extends_buffer_and_prunes_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_time = monotonic()

    def now() -> float:
        return fake_time

    import paige.application.echo_dedup as mod

    monkeypatch.setattr(mod, "monotonic", now)

    d = EchoDedup(ttl_seconds=1.0)
    d.record("@1", "old")
    fake_time += 5.0
    d.record("@1", "new")  # prune happens here
    # Old entry is gone; new entry matches.
    assert d.is_echo("@1", "old") is False
    assert d.is_echo("@1", "new") is True


def test_stable_under_unicode() -> None:
    d = EchoDedup()
    d.record("@1", "héllo 🌍")
    assert d.is_echo("@1", "héllo 🌍") is True
