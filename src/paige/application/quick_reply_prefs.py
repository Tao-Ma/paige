"""QuickReplyPrefs — per-(person, conversation) 3 quick-reply slots.

In-memory only; resets on every paige restart. The hardcoded
defaults below seed any (person, conversation) that hasn't been
seen yet. `update` mutates a single slot — used by
`EndTurnPanelService` after the user submits the 4-input panel, so
that an edited prompt becomes the next default for the same slot.

When persistence is needed later, swap the in-memory dict for a
`FileStorage`-backed read/write — the API stays the same.
"""

from __future__ import annotations

from ..domain.conversation import Conversation
from ..domain.person import Person

SLOT_COUNT = 3

# Initial defaults. The user can edit any of these via the
# `end_turn` panel; the edit is remembered for that (person,
# conversation) until paige restarts.
DEFAULT_SLOTS: tuple[str, str, str] = (
    "what's next",
    "commit & push",
    "should we clear for next session",
)


_Key = tuple[str, str, str]


class QuickReplyPrefs:
    """3 editable slot defaults per (person, conversation).

    Slots are addressed by integer index 0..2. Out-of-range indices
    raise IndexError — callers (the panel render + submit handler)
    always know the valid range from `SLOT_COUNT`.
    """

    def __init__(self) -> None:
        self._store: dict[_Key, tuple[str, str, str]] = {}

    def get(self, person: Person, conversation: Conversation) -> tuple[str, str, str]:
        return self._store.get(_key(person, conversation), DEFAULT_SLOTS)

    def update(
        self,
        person: Person,
        conversation: Conversation,
        slot: int,
        text: str,
    ) -> None:
        if not 0 <= slot < SLOT_COUNT:
            raise IndexError(f"slot {slot} out of range [0, {SLOT_COUNT})")
        current = list(self.get(person, conversation))
        current[slot] = text
        self._store[_key(person, conversation)] = (current[0], current[1], current[2])


def _key(person: Person, conversation: Conversation) -> _Key:
    return (person.user_id, conversation.chat_id, conversation.thread_id or "")


__all__ = ["DEFAULT_SLOTS", "SLOT_COUNT", "QuickReplyPrefs"]
