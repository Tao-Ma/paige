"""UsageService — drive Claude Code's /usage modal + parse + reply."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.application.usage import (
    CAPTURE_FAILED_HINT,
    PANE_GONE_HINT,
    UNBOUND_HINT,
    UsageService,
)
from paige.domain.conversation import Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import TextContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = UsageService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        allow_list=AllowList(),
        modal_render_delay=0.0,  # don't slow tests
    )
    service.install(channel)

    class Harness:
        pass

    h = Harness()
    h.channel = channel  # type: ignore[attr-defined]
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _inbound() -> Inbound:
    return Inbound(sender=ALICE, conversation=CONV, text="/usage", message_id="m1")


# ── error paths ──────────────────────────────────────────────────


async def test_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("usage", _inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == UNBOUND_HINT
    # Bound-pane-only side effects must not happen.
    assert h.mux.send_keys_calls == []


async def test_pane_gone_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.registry.bind(ALICE, CONV, "@404")  # registry says bound, mux says no
    await h.channel.deliver_command("usage", _inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == PANE_GONE_HINT
    assert h.mux.send_keys_calls == []


async def test_empty_capture_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    # capture default is "" — empty.

    await h.channel.deliver_command("usage", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == CAPTURE_FAILED_HINT


# ── happy path ───────────────────────────────────────────────────


async def test_parsed_modal_replied_as_code_block(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.mux.set_capture(
        "@1",
        "\n".join(
            [
                "Settings: Usage",
                "Daily quota",
                "█████▋   38% used",
                "Resets in 6h 12m",
                "Esc to cancel",
            ]
        ),
    )

    await h.channel.deliver_command("usage", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    body = sent.content.text
    assert body.startswith("```\n") and body.endswith("\n```")
    assert "38% used" in body
    assert "Resets in 6h 12m" in body


async def test_unparsed_pane_falls_back_to_raw_truncated(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    # Some random pane content that doesn't include the modal.
    raw = "$ git status\nOn branch main\nnothing to commit"
    h.mux.set_capture("@1", raw)

    await h.channel.deliver_command("usage", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    body = sent.content.text
    assert body.startswith("```\n")
    # Raw content is preserved verbatim when parser doesn't bite.
    assert "On branch main" in body


async def test_drives_tui_with_correct_keys(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.mux.set_capture("@1", "Settings: Usage\nDaily quota\nEsc to cancel")

    await h.channel.deliver_command("usage", _inbound())
    await h.outbox.stop()

    # Two send_keys calls in order: type "/usage" with Enter, then Escape.
    [first, second] = h.mux.send_keys_calls
    assert first.text == "/usage"
    assert first.enter is True
    assert first.literal is True
    assert second.text == "Escape"
    assert second.enter is False
    assert second.literal is False


async def test_long_raw_fallback_truncates(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.mux.set_capture("@1", "x" * 5000)  # no modal markers, well over 3000

    await h.channel.deliver_command("usage", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert "(truncated)" in sent.content.text
