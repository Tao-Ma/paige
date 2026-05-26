"""CommandService — slash command handlers + forwarding."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.commands import (
    FORWARDED_COMMANDS,
    NATIVE_COMMANDS,
    UNBOUND_COMMAND_HINT,
    CommandService,
)
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.domain.conversation import Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent, TextContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    """Wire CommandService onto FakeChannel + FakeMultiplexer + RunRegistry."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = CommandService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        allow_list=AllowList(),  # open
    )
    service.install(channel)

    class Harness:
        pass

    h = Harness()
    h.channel = channel  # type: ignore[attr-defined]
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    h.service = service  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _inbound(text: str) -> Inbound:
    return Inbound(sender=ALICE, conversation=CONV, text=text, message_id="m1")


# ── registration ─────────────────────────────────────────────────


async def test_install_registers_all_native_and_forwarded(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    registered = set(h.channel._command_handlers.keys())  # noqa: SLF001
    assert set(NATIVE_COMMANDS) <= registered
    assert set(FORWARDED_COMMANDS) <= registered


# ── /help ────────────────────────────────────────────────────────


async def test_help_sends_a_card(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("help", _inbound("/help"))
    await h.outbox.stop()
    assert len(h.channel.sent) == 1
    sent = h.channel.sent[0]
    assert isinstance(sent.content, CardContent)
    body = sent.content.card.text
    # Some shape we can rely on:
    assert "/help" in body
    assert "/esc" in body
    assert "/clear" in body  # forwarded


# ── /esc ─────────────────────────────────────────────────────────


async def test_esc_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("esc", _inbound("/esc"))
    await h.outbox.stop()
    assert len(h.channel.sent) == 1
    sent = h.channel.sent[0]
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == UNBOUND_COMMAND_HINT
    assert h.mux.send_keys_calls == []


async def test_esc_bound_sends_escape_named_key(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_command("esc", _inbound("/esc"))
    await h.outbox.stop()

    assert len(h.mux.send_keys_calls) == 1
    call = h.mux.send_keys_calls[0]
    assert call.pane_id == "@1"
    assert call.text == "Escape"
    assert call.enter is False
    assert call.literal is False
    # No chat ack — Claude renders the interrupt in the pane.
    assert h.channel.sent == []


# ── /unbind ──────────────────────────────────────────────────────


async def test_unbind_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("unbind", _inbound("/unbind"))
    await h.outbox.stop()
    assert h.channel.sent[0].content == TextContent(UNBOUND_COMMAND_HINT)
    # Registry stays empty.
    assert h.registry.get_pane(ALICE, CONV) is None


async def test_unbind_drops_binding_and_acks(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@5", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@5")

    await h.channel.deliver_command("unbind", _inbound("/unbind"))
    await h.outbox.stop()

    assert h.registry.get_pane(ALICE, CONV) is None
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert "myproj" in sent.content.text
    assert "Unbound" in sent.content.text


async def test_unbind_keeps_other_bindings(harness) -> None:  # type: ignore[no-untyped-def]
    """/unbind from one chat doesn't touch bindings in other chats.

    Within the sender's own chat the unbind is chat-scoped (drops
    every non-topic binding), so the "other binding" we keep has
    to live in a different chat to be a meaningful contrast.
    """
    h = harness
    h.mux.add_pane("@5", "p", Path("/p"))
    other_chat = Conversation(chat_id="-200", thread_id="99")
    await h.registry.bind(ALICE, CONV, "@5")
    await h.registry.bind(ALICE, other_chat, "@5")

    await h.channel.deliver_command("unbind", _inbound("/unbind"))
    await h.outbox.stop()

    assert h.registry.get_pane(ALICE, CONV) is None
    assert h.registry.get_pane(ALICE, other_chat) == "@5"


# ── forwarded commands ──────────────────────────────────────────


@pytest.mark.parametrize("cmd", FORWARDED_COMMANDS)
async def test_forwarded_command_sends_keys_when_bound(  # type: ignore[no-untyped-def]
    harness, cmd
) -> None:
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_command(cmd, _inbound(f"/{cmd}"))
    await h.outbox.stop()

    assert len(h.mux.send_keys_calls) == 1
    call = h.mux.send_keys_calls[0]
    assert call.pane_id == "@1"
    assert call.text == f"/{cmd}"
    assert call.enter is True
    assert call.literal is True
    # No chat ack — Claude renders the slash command in the pane.
    assert h.channel.sent == []


async def test_forwarded_command_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("clear", _inbound("/clear"))
    await h.outbox.stop()
    assert h.channel.sent[0].content == TextContent(UNBOUND_COMMAND_HINT)
    assert h.mux.send_keys_calls == []


async def test_forwarded_command_with_arg_carries_arg(harness) -> None:  # type: ignore[no-untyped-def]
    """`inbound.text` includes the original `/cmd arg`; the handler
    forwards verbatim."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_command("model", _inbound("/model haiku"), "haiku")
    await h.outbox.stop()
    [call] = h.mux.send_keys_calls
    assert call.text == "/model haiku"


# ── allow-list gate ─────────────────────────────────────────────


async def test_disallowed_sender_command_is_dropped() -> None:
    """A closed AllowList blocks a non-listed sender from running
    any command — no chat reply, no send_keys."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = CommandService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        allow_list=AllowList(["u-only-alice"]),
    )
    service.install(channel)

    mux.add_pane("@1", "p", Path("/p"))
    bob = Person(user_id="u-bob")
    await registry.bind(bob, CONV, "@1")
    bob_inbound = Inbound(sender=bob, conversation=CONV, text="/esc", message_id="m")

    await channel.deliver_command("esc", bob_inbound)
    await channel.deliver_command("help", bob_inbound)
    await channel.deliver_command("clear", bob_inbound)
    await outbox.stop()

    assert channel.sent == []
    assert mux.send_keys_calls == []


async def test_start_no_longer_registered_by_command_service(harness) -> None:  # type: ignore[no-untyped-def]
    """`/start` moved to DirectoryService; CommandService doesn't
    handle it."""
    h = harness
    assert "start" not in h.channel._command_handlers  # noqa: SLF001
