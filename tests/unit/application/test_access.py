"""AllowList — gate semantics + handler-wrapping helpers."""

from __future__ import annotations

import pytest

from paige.application.access import AdminList, AllowList
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.person import Person

ALICE = Person(user_id="u-alice")
BOB = Person(user_id="u-bob")
CONV = Conversation(chat_id="-100", thread_id="42")


def _inbound(sender: Person) -> Inbound:
    return Inbound(sender=sender, conversation=CONV, text="hi", message_id="m")


def _action(sender: Person) -> ActionEvent:
    return ActionEvent(
        sender=sender,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="9"),
        action_id="x",
        value={},
        ack_token="t",
    )


# ── basic semantics ──────────────────────────────────────────────


def test_empty_is_open() -> None:
    a = AllowList()
    assert a.is_open() is True
    assert a.allows_user("anybody") is True


def test_non_empty_is_closed() -> None:
    a = AllowList(["u-alice"])
    assert a.is_open() is False
    assert a.allows_user("u-alice") is True
    assert a.allows_user("u-bob") is False


def test_blank_user_ids_dropped() -> None:
    """Blank entries from a CSV split shouldn't accidentally widen
    the gate."""
    a = AllowList(["u-alice", "", "  "])
    assert a.allows_user("u-alice") is True
    assert a.allows_user("") is False


def test_multiple_users() -> None:
    a = AllowList(["u-alice", "u-carol"])
    assert a.allows_user("u-alice") is True
    assert a.allows_user("u-carol") is True
    assert a.allows_user("u-bob") is False


# ── guard_inbound ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_inbound_passes_allowed() -> None:
    a = AllowList(["u-alice"])
    seen: list[Inbound] = []

    async def handler(inb: Inbound) -> None:
        seen.append(inb)

    guarded = a.guard_inbound(handler)
    await guarded(_inbound(ALICE))
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_guard_inbound_drops_disallowed_silently() -> None:
    a = AllowList(["u-alice"])
    seen: list[Inbound] = []

    async def handler(inb: Inbound) -> None:
        seen.append(inb)

    guarded = a.guard_inbound(handler)
    await guarded(_inbound(BOB))
    assert seen == []


@pytest.mark.asyncio
async def test_guard_inbound_open_passes_everyone() -> None:
    a = AllowList()
    seen: list[Inbound] = []

    async def handler(inb: Inbound) -> None:
        seen.append(inb)

    guarded = a.guard_inbound(handler)
    await guarded(_inbound(BOB))
    assert len(seen) == 1


# ── guard_command ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_command_passes_arg_along() -> None:
    a = AllowList(["u-alice"])
    seen: list[tuple[Inbound, str]] = []

    async def handler(inb: Inbound, arg: str) -> None:
        seen.append((inb, arg))

    guarded = a.guard_command(handler)
    await guarded(_inbound(ALICE), "haiku")
    assert seen == [(_inbound(ALICE), "haiku")]


@pytest.mark.asyncio
async def test_guard_command_drops_disallowed() -> None:
    a = AllowList(["u-alice"])
    called = False

    async def handler(_inb: Inbound, _arg: str) -> None:
        nonlocal called
        called = True

    guarded = a.guard_command(handler)
    await guarded(_inbound(BOB), "")
    assert called is False


# ── guard_action ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_action_passes_allowed() -> None:
    a = AllowList(["u-alice"])
    seen: list[ActionEvent] = []

    async def handler(ev: ActionEvent) -> None:
        seen.append(ev)

    guarded = a.guard_action(handler)
    await guarded(_action(ALICE))
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_guard_action_drops_disallowed_without_ack() -> None:
    """No ack on drop — Bob shouldn't even know the bot saw his tap."""
    a = AllowList(["u-alice"])
    called = False

    async def handler(_ev: ActionEvent) -> None:
        nonlocal called
        called = True

    guarded = a.guard_action(handler)
    await guarded(_action(BOB))
    assert called is False


# ── AdminList ────────────────────────────────────────────────────


def test_admin_list_with_explicit_admins_only_listed_pass() -> None:
    a = AdminList(admins=["u-alice"], allowed=["u-alice", "u-bob"])
    assert a.is_admin("u-alice") is True
    assert a.is_admin("u-bob") is False


def test_admin_list_empty_admins_falls_back_to_allowed() -> None:
    """Solo-deploy default — empty PAIGE_ADMIN_USERS means every
    allowed user is admin."""
    a = AdminList(admins=(), allowed=["u-alice", "u-bob"])
    assert a.is_admin("u-alice") is True
    assert a.is_admin("u-bob") is True
    assert a.is_admin("u-carol") is False


def test_admin_list_empty_admins_and_open_allowed_passes_everyone() -> None:
    """Allow-list open + admin list empty = anyone is admin (only
    sane behavior for a config that explicitly opted into 'open')."""
    a = AdminList()
    assert a.is_admin("anybody") is True


def test_admin_list_blank_user_ids_dropped() -> None:
    a = AdminList(admins=["u-alice", "", " "])
    assert a.is_admin("u-alice") is True
    assert a.is_admin("") is False
