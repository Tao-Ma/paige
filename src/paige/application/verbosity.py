"""VerbosityService — per-(person, conversation, content_kind) BRIEF/FULL state.

Default is FULL for all three settable kinds — text, tool_use,
tool_result. Hiding content behind a 200-char clip is worse than the
mobile-noise trade-off it tries to mitigate: AskUserQuestion's
prompt, an Edit's diff, a tool's argument list — all the things the
user actually needs to act on get clipped to JSON-shaped slop.
Users explicitly opt into BRIEF per kind for the noisy ones (large
Bash stdout, megabyte file Reads).

Why per-conversation, not per-user: a user observing a mobile app
session and a CI session simultaneously may want different verbosity
in each.

Why in-memory only: v1's `session_filters` was deliberately not
persisted. The user re-toggles after restart — that's the only thing
the env-var version did differently, and it wasn't worth keeping.
v2 follows the same call.

THINKING blocks aren't on this card. v1 trimmed them at a fixed cap
in the renderer; v2 will do the same in `render_block` if needed.
Status messages aren't here either — `StatusService` owns its own
truncation rules.
"""

from __future__ import annotations

from enum import StrEnum

from ..domain.conversation import Conversation
from ..domain.person import Person
from ..infrastructure.markdown_safe import close_unbalanced_fence


class ContentKind(StrEnum):
    """The block kinds the user can dial verbosity on."""

    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


class Verbosity(StrEnum):
    BRIEF = "brief"
    FULL = "full"


_TRUNCATED_MARKER = "… ({n} chars truncated)"


class VerbosityService:
    """Per-(person, conversation, content_kind) BRIEF/FULL with
    truncation helper.

    `default` lets callers (typically tests) construct the service
    with `default=Verbosity.BRIEF` to assert truncation behavior.
    Production uses the FULL default.
    """

    def __init__(
        self,
        *,
        default: Verbosity = Verbosity.FULL,
        brief_chars: int = 200,
    ) -> None:
        self._default = default
        self._brief_chars = brief_chars
        # (user_id, chat_id, thread_id_or_empty, ContentKind) → Verbosity
        self._state: dict[tuple[str, str, str, ContentKind], Verbosity] = {}

    def get(
        self,
        person: Person,
        conversation: Conversation,
        kind: ContentKind,
    ) -> Verbosity:
        return self._state.get(
            self._key(person, conversation, kind),
            self._default,
        )

    def set(
        self,
        person: Person,
        conversation: Conversation,
        kind: ContentKind,
        verbosity: Verbosity,
    ) -> None:
        self._state[self._key(person, conversation, kind)] = verbosity

    def toggle(
        self,
        person: Person,
        conversation: Conversation,
        kind: ContentKind,
    ) -> Verbosity:
        """Flip the verbosity for this triple. Returns the new value."""
        current = self.get(person, conversation, kind)
        new = Verbosity.FULL if current is Verbosity.BRIEF else Verbosity.BRIEF
        self.set(person, conversation, kind, new)
        return new

    def maybe_truncate(
        self,
        person: Person,
        conversation: Conversation,
        kind: ContentKind,
        text: str,
    ) -> str:
        """Truncate to `brief_chars` if the binding is set to BRIEF;
        return text unchanged on FULL."""
        if self.get(person, conversation, kind) is Verbosity.FULL:
            return text
        return _truncate(text, self._brief_chars)

    @staticmethod
    def _key(
        person: Person,
        conversation: Conversation,
        kind: ContentKind,
    ) -> tuple[str, str, str, ContentKind]:
        return (
            person.user_id,
            conversation.chat_id,
            conversation.thread_id or "",
            kind,
        )


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = len(text) - n
    # Close any code fence the cut severed before appending the marker —
    # an open fence would otherwise swallow the rest of the card body
    # (the live stream renders this into a `tag: markdown` element).
    return close_unbalanced_fence(text[:n]) + _TRUNCATED_MARKER.format(n=cut)
