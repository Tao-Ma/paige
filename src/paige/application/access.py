"""AllowList — per-user_id gate for inbound events.

Empty allow-list = open: every sender passes. Non-empty = closed:
only listed user_ids pass. There's no per-conversation or
per-command granularity here; if your needs grow that complex
this becomes a `Policy` service.

Where the check lives: each application service that registers
handlers wraps them via `AllowList.guard(handler)` at install()
time, so the gate runs once per dispatch — not at every handler's
top. Unauthorized inbound is silently dropped (no chat reply, no
state change). Telling Bob "you're not allowed" would itself be
acknowledging the bot's existence to him, which is the opposite
of what gating wants.

Design note: this is application-layer policy, not adapter
behavior. The Channel port stays neutral; backends don't need to
know about allow-listing. A future PolicyService can absorb this
plus ACLs, rate-limit hints, audit trails — all without touching
adapters.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

from ..domain.card import ActionEvent
from ..domain.inbound import Inbound

T = TypeVar("T")


class AllowList:
    """Frozen set of allowed user_ids. Empty means everyone passes."""

    __slots__ = ("_users",)

    def __init__(self, users: Iterable[str] = ()) -> None:
        self._users = frozenset(u for u in users if u)

    def is_open(self) -> bool:
        """True when no users are listed — anyone passes."""
        return not self._users

    def allows_user(self, user_id: str) -> bool:
        return self.is_open() or user_id in self._users

    # ── handler-wrapping helpers ─────────────────────────────────

    def guard_inbound(
        self,
        handler: Callable[[Inbound], Awaitable[None]],
    ) -> Callable[[Inbound], Awaitable[None]]:
        """Wrap an `on_inbound` handler so unauthorized senders are
        dropped. Returned closure has the same signature."""

        async def guarded(inbound: Inbound) -> None:
            if not self.allows_user(inbound.sender.user_id):
                return
            await handler(inbound)

        return guarded

    def guard_command(
        self,
        handler: Callable[[Inbound, str], Awaitable[None]],
    ) -> Callable[[Inbound, str], Awaitable[None]]:
        """Wrap an `on_command` handler. Same drop-silent semantics."""

        async def guarded(inbound: Inbound, arg: str) -> None:
            if not self.allows_user(inbound.sender.user_id):
                return
            await handler(inbound, arg)

        return guarded

    def guard_action(
        self,
        handler: Callable[[ActionEvent], Awaitable[None]],
    ) -> Callable[[ActionEvent], Awaitable[None]]:
        """Wrap an `on_action` handler. Tap-from-unauthorized is
        dropped without ack — the keyboard stays in its current
        state on the tapper's side, which is fine: they shouldn't
        have a card to tap on in the first place."""

        async def guarded(event: ActionEvent) -> None:
            if not self.allows_user(event.sender.user_id):
                return
            await handler(event)

        return guarded


class AdminList:
    """Frozen set of admin user_ids — gates `/server` and similar.

    Constructed with both the bot's allow-list AND a (possibly empty)
    admin set. Empty admin set means "every allowed user is admin"
    so solo deploys don't have to set both env vars.

    Unlike `AllowList`, this isn't an early-drop gate — admin commands
    want to *tell* the unauthorized user "admin only", not silently
    eat the tap. So this class only exposes a predicate (`is_admin`);
    callers handle the negative case explicitly.
    """

    __slots__ = ("_admins", "_allowed")

    def __init__(
        self,
        *,
        admins: Iterable[str] = (),
        allowed: Iterable[str] = (),
    ) -> None:
        self._admins = frozenset(u for u in admins if u)
        self._allowed = frozenset(u for u in allowed if u)

    def is_admin(self, user_id: str) -> bool:
        if self._admins:
            return user_id in self._admins
        # Empty admin list → every allowed user is admin (and if the
        # allow-list itself is open, everyone is). Solo-deploy default.
        if not self._allowed:
            return True
        return user_id in self._allowed


__all__ = ["AdminList", "AllowList"]
