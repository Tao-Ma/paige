"""EchoDedup — drop IM-typed prompts bouncing back through Claude's JSONL.

When the bot forwards a user's chat message to a tmux pane, the same
text reappears as a `role="user"` event in the transcript a moment
later. Without dedup we'd send it back to the user's chat — they'd
see their own message twice.

The dedup window is small (5 s by default). Beyond that, a USER-role
event in the transcript is assumed to be tmux-typed (i.e. the user is
sitting at their terminal). v1 surfaces those with a `⌨` marker so
the chat user can see what was typed at the laptop.

Whitespace is collapsed for matching so a user who hits enter twice
or copies a multi-line snippet still matches.
"""

from __future__ import annotations

from time import monotonic


class EchoDedup:
    """Tiny ring buffer of (pane_id, normalized_text, mono_time)."""

    def __init__(self, *, ttl_seconds: float = 5.0, max_entries: int = 256) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: list[tuple[str, str, float]] = []

    def record(self, pane_id: str, text: str) -> None:
        """Note a send_keys call so the next matching JSONL event is
        recognized as an echo."""
        self._prune()
        self._entries.append((pane_id, _normalize(text), monotonic()))
        # Bound the buffer; oldest entries fall off first.
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max :]

    def is_echo(self, pane_id: str, text: str) -> bool:
        """Return True if (pane_id, text) matches a recorded send.
        On match, the entry is consumed — a duplicate later won't
        match (in case the user actually typed the same thing again
        at the laptop)."""
        self._prune()
        norm = _normalize(text)
        for i, (rec_pane, rec_text, _ts) in enumerate(self._entries):
            if rec_pane == pane_id and rec_text == norm:
                self._entries.pop(i)
                return True
        return False

    def _prune(self) -> None:
        cutoff = monotonic() - self._ttl
        self._entries = [entry for entry in self._entries if entry[2] >= cutoff]


def _normalize(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip ends.

    `"hello  world\\n"` and `" hello world "` both normalize to
    `"hello world"` — a user pressing Enter twice or stripping a
    trailing newline shouldn't break the match.
    """
    return " ".join(text.split())
