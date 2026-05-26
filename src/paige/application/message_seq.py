"""MessageSeqService — debug seq stamping on outgoing messages.

When enabled, every outgoing send/edit gets a `_seq #N_` footer on
the rendered body. Edits show the chain so the user can say "msg #42
broke" and the operator greps `paige.log` for that exact seq to
reach the original Outbound + payload. Consecutive runs collapse to
a range to save space: an all-consecutive chain renders `_seq #3–#6_`
and a gapped one `_seq #9 [#3–#5 → #9]_`.

Counter scope is per-conversation (per topic) because the user
reads messages thread-by-thread; one linear seq per thread reads
naturally. The on/off toggle is per-(person, conversation) so two
users in the same group chat can opt in independently — same shape
as `VerbosityService`.

Off by default. The single use case is debugging — every footer is
visible noise in normal conversation.

Two-step stamping for sends: `stamp_send` allocates the seq and
returns it alongside the new Outbound, then the caller invokes
`record_send_anchor(anchor, seq)` once the channel returns the
message_id. Without that bind, an edit on the same message_id
later wouldn't find the chain root.

Known limitations (acceptable for a debug aid, worth noting):

- **State is in-memory only.** Counters and chains reset on every
  `prod.sh restart` / `upgrade`. A user mid-debug should re-toggle
  after a redeploy.
- **`DocumentContent` consumes a seq it can't render.** Image-only
  outbounds have no text slot for the footer; the counter still
  advances so `/screenshot` between two text messages leaves a
  visible "gap" in seq numbers. Fine — gaps signal "image card
  here, not a missing message" — but worth knowing.
- **`_chains` entries leak on `delete`.** No `clear_anchor` hook
  yet — deleted-card chain entries stay until the process exits.
  Bounded growth (ints in lists, small per entry) but unbounded
  over time. Easy follow-up if the dev session lasts long enough
  to matter.
"""

from __future__ import annotations

from dataclasses import replace

from ..domain.conversation import Anchor, Conversation
from ..domain.outbound import CardContent, Outbound, TextContent
from ..domain.person import Person


class MessageSeqService:
    """Per-conversation seq counter + per-(person, conversation)
    on/off toggle + per-anchor edit chain."""

    def __init__(self) -> None:
        # (user_id, chat_id, thread_id_or_empty) → bool
        self._enabled: dict[tuple[str, str, str], bool] = {}
        # (chat_id, thread_id_or_empty) → next-seq counter
        self._counters: dict[tuple[str, str], int] = {}
        # message_id → ordered chain of seqs (origin … current)
        self._chains: dict[str, list[int]] = {}

    # ── prefs toggle ─────────────────────────────────────────────

    def is_enabled(self, person: Person, conversation: Conversation) -> bool:
        return self._enabled.get(_pkey(person, conversation), False)

    def toggle(self, person: Person, conversation: Conversation) -> bool:
        new = not self.is_enabled(person, conversation)
        self._enabled[_pkey(person, conversation)] = new
        return new

    # ── stamping ─────────────────────────────────────────────────

    def stamp_send(
        self,
        person: Person,
        conversation: Conversation,
        outbound: Outbound,
    ) -> tuple[Outbound, int | None]:
        """Allocate the next seq for this conversation and append a
        `_seq #N_` footer to the outbound's body. Returns the new
        outbound and the allocated seq, or `(outbound, None)` if
        the toggle is off. Caller must call `record_send_anchor`
        once the channel returns the resulting anchor."""
        if not self.is_enabled(person, conversation):
            return outbound, None
        seq = self._next_seq(conversation)
        return _with_footer(outbound, [seq]), seq

    def stamp_edit(
        self,
        person: Person,
        conversation: Conversation,
        anchor: Anchor,
        outbound: Outbound,
    ) -> tuple[Outbound, int | None]:
        """Allocate the next seq, extend the chain stored against
        `anchor.message_id`, and append the compact-range chain footer
        to the body. Returns `(outbound, None)` if the toggle is off.
        If the anchor has no recorded chain (e.g. the original send
        happened with stamping off), this edit becomes the chain
        root with a single-entry footer."""
        if not self.is_enabled(person, conversation):
            return outbound, None
        chain = list(self._chains.get(anchor.message_id, []))
        seq = self._next_seq(conversation)
        chain.append(seq)
        self._chains[anchor.message_id] = chain
        return _with_footer(outbound, chain), seq

    def record_send_anchor(self, anchor: Anchor, seq: int) -> None:
        """Bind a freshly-allocated seq to the message_id the
        channel returned, so subsequent edits on the same message
        can extend the chain."""
        self._chains[anchor.message_id] = [seq]

    # ── internals ────────────────────────────────────────────────

    def _next_seq(self, conversation: Conversation) -> int:
        key = _ckey(conversation)
        nxt = self._counters.get(key, 0) + 1
        self._counters[key] = nxt
        return nxt


def _pkey(person: Person, conversation: Conversation) -> tuple[str, str, str]:
    return (person.user_id, conversation.chat_id, conversation.thread_id or "")


def _ckey(conversation: Conversation) -> tuple[str, str]:
    return (conversation.chat_id, conversation.thread_id or "")


def _with_footer(outbound: Outbound, chain: list[int]) -> Outbound:
    """Append a seq footer to the outbound's body. TextContent and
    CardContent get the footer in their text field; DocumentContent
    and TypingContent are returned unchanged (no body to stamp on
    an image / typing indicator)."""
    footer = _format_footer(chain)
    content = outbound.content
    if isinstance(content, TextContent):
        new_text = f"{content.text}\n\n{footer}" if content.text.strip() else footer
        return replace(outbound, content=TextContent(text=new_text))
    if isinstance(content, CardContent):
        old_card = content.card
        body = old_card.text or ""
        new_text = f"{body}\n\n{footer}" if body.strip() else footer
        return replace(
            outbound,
            content=CardContent(card=replace(old_card, text=new_text)),
        )
    return outbound


def _format_footer(chain: list[int]) -> str:
    if len(chain) <= 1:
        return f"_seq #{chain[0]}_"
    segments = _chain_segments(chain)
    if len(segments) == 1:
        # The whole chain is one consecutive run (#3 → #4 → #5 → #6):
        # render it as a single range with no bracket, since the range
        # end already is the current seq. `_seq #3–#6_`.
        return f"_seq {segments[0]}_"
    return f"_seq #{chain[-1]} [{' → '.join(segments)}]_"


def _chain_segments(chain: list[int]) -> list[str]:
    """Collapse maximal consecutive runs into ranges. The chain is
    strictly increasing; a run of ≥2 seqs differing by 1 becomes
    `#start–#end`, an isolated seq stays `#n`. Gaps (from a
    `DocumentContent` that consumed a seq it couldn't render) break a
    run. En-dash matches the range style used elsewhere (e.g. Read's
    `lines 5–12`)."""
    segments: list[str] = []
    i, n = 0, len(chain)
    while i < n:
        j = i
        while j + 1 < n and chain[j + 1] == chain[j] + 1:
            j += 1
        segments.append(f"#{chain[i]}" if j == i else f"#{chain[i]}–#{chain[j]}")
        i = j + 1
    return segments


__all__ = ["MessageSeqService"]
