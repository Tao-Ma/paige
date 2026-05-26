"""CollapsePrefService — per-(person, conversation) collapse threshold.

When a card body has more newlines than this, the Feishu adapter
wraps the body in a `collapsible_panel` so the user sees a
tap-to-expand header instead of a wall of text. Per-(person,
conversation) so two users in the same group chat can dial it
independently — same shape as `MessageSeqService` /
`VerbosityService`.

Cycle order: `25 → 50 → 100 → 0 (off) → 25`. The Prefs sub-pane
on the Manage card has a single button that advances through
these values.

Default for users who haven't visited Prefs: `25` lines —
short enough that tool output and large diffs collapse, long
enough that 5-line replies stay flat.

Limitations (acceptable for a UX knob, worth noting):

- **State is in-memory only.** Resets on every `prod.sh restart`
  / `upgrade`. Users who want a non-default threshold will need
  to set it again after a redeploy.
- **The threshold is applied at enqueue time** by the Outbox, not
  at render time. Editing a previously-sent card via PATCH uses
  the threshold *at the moment of the edit*, not the original
  send — generally fine; the user's current pref is what they
  want now.
- **Lark v7.9+ only** for the resulting `collapsible_panel`.
  Older clients render an "upgrade" placeholder. paige doesn't
  gate on client version — older clients just see the placeholder.
"""

from __future__ import annotations

from dataclasses import replace

from ..domain.conversation import Conversation
from ..domain.outbound import Outbound
from ..domain.person import Person

# Cycle order. `0` is the "disable for this topic" stop —
# everything renders flat regardless of body length.
_CYCLE: tuple[int, ...] = (25, 50, 100, 0)
_DEFAULT: int = _CYCLE[0]


class CollapsePrefService:
    """Per-(person, conversation) collapse-threshold registry."""

    def __init__(self) -> None:
        # (user_id, chat_id, thread_id_or_empty) → threshold lines.
        # Absent = `_DEFAULT`.
        self._thresholds: dict[tuple[str, str, str], int] = {}

    # ── prefs ────────────────────────────────────────────────────

    def threshold(self, person: Person, conversation: Conversation) -> int:
        """Current threshold for this topic. Returns `_DEFAULT` for
        topics the user hasn't visited Prefs in. `0` means "off"."""
        return self._thresholds.get(_key(person, conversation), _DEFAULT)

    def cycle(self, person: Person, conversation: Conversation) -> int:
        """Advance to the next value in the cycle and return the new
        threshold. Wraps around from off (0) back to the first value."""
        current = self.threshold(person, conversation)
        try:
            idx = _CYCLE.index(current)
        except ValueError:
            # Current value isn't on the cycle (e.g. an env override
            # from a previous version). Snap back to the first cycle
            # entry.
            idx = -1
        nxt = _CYCLE[(idx + 1) % len(_CYCLE)]
        self._thresholds[_key(person, conversation)] = nxt
        return nxt

    # ── stamping ─────────────────────────────────────────────────

    def stamp(
        self,
        person: Person,
        conversation: Conversation,
        outbound: Outbound,
    ) -> Outbound:
        """Apply the user's threshold to the outbound. Returns a
        replaced Outbound carrying `collapse_threshold_lines`. The
        adapter reads this field at render time."""
        n = self.threshold(person, conversation)
        if n == outbound.collapse_threshold_lines:
            # Avoid an unnecessary `replace` allocation when the
            # field is already correct (e.g. defaulted to 0 and the
            # user has the topic set to 0 / off).
            return outbound
        return replace(outbound, collapse_threshold_lines=n)


def _key(person: Person, conversation: Conversation) -> tuple[str, str, str]:
    return (person.user_id, conversation.chat_id, conversation.thread_id or "")


__all__ = ["CollapsePrefService"]
