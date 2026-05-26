"""HistoryService — `/history` paginated transcript card.

Reads the bound conversation's transcript JSONL, formats each
TranscriptEvent into a one-block summary, splits the result into
~3500-char pages, and shows the last page with Older / Newer
buttons. Tap to repaint the same card to a different page —
identical UX to v1's /history but without the unread-since-last-view
mode (deferred polish).

Path resolution: RunRegistry has the `(run_id, cwd)` pointer for
the bound pane; `infrastructure.transcript_path` does the path math.
The file may not exist yet (fresh session, just `/clear`'d) — in
that case we send an empty-history hint and skip the card.

Pagination state is per-conversation in memory: the action handler
fetches by index, so we don't have to re-read + re-format on each
tap. A fresh `/history` overwrites the cached pages.

Pages don't survive a process restart — but neither does anything
else paginated (DirectoryService listings, /sessions). Acceptable
for v1; gives someone a reason to write a stateful pager later.

Out of scope (deferred): unread-since-last-view, dormant-session
peek (no live pane), session-keyed pagination keys. The latter
two need the Manage card UI which we haven't built yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from ..domain.card import Action, ActionEvent, Card
from ..domain.conversation import Conversation
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound, TextContent
from ..domain.person import Person
from ..domain.transcript import Block, BlockKind, Role, TranscriptEvent
from ..infrastructure.jsonl_parser import JsonlParser
from ..infrastructure.markdown_safe import demote_headings, literal_md, safe_clip
from ..infrastructure.transcript_path import transcript_path
from ..ports.channel import Channel
from .access import AllowList
from .message_seq import MessageSeqService
from .outbox import Outbox
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

ACTION_PAGE = "hist:p"
# Back returns to the /session Manage card when a binding exists,
# else to the /sessions chooser. Routed by SessionsService since
# it owns the Manage card builder; defining the id here avoids a
# circular import (sessions.py already imports HistoryService).
ACTION_HIST_BACK = "hist:bk"
# Dismiss deletes the card; handled by HistoryService locally.
ACTION_HIST_DISMISS = "hist:di"

UNBOUND_HINT = "No session bound to this conversation. Use /start to pick a directory."
NO_RUN_HINT = "Pane is bound but has no live run yet — try again in a moment."
READ_FAILED_HINT = "Failed to read transcript."
EMPTY_HINT = "📋 No messages yet in this session."

_PAGE_LIMIT = 3500
_THINKING_LIMIT = 500
_TOOL_USE_LIMIT = 200
_TOOL_RESULT_LIMIT = 400
_TEXT_USER_LIMIT = 1500
_TEXT_ASSISTANT_LIMIT = 2000

_ConvKey = tuple[str, str, str]  # (user_id, chat_id, thread_id_or_empty)


class HistoryService:
    """`/history` command + page-tap action handler."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        outbox: Outbox,
        channel: Channel,
        allow_list: AllowList,
        projects_root: Path | None = None,
        message_seq: MessageSeqService | None = None,
    ) -> None:
        self._registry = registry
        self._outbox = outbox
        self._channel = channel
        self._allow_list = allow_list
        self._projects_root = projects_root
        # Page-tap repaints go direct to the channel (inline-refresh),
        # bypassing the Outbox — so they need to stamp the seq footer
        # themselves to stay consistent with the initial send, matching
        # the ask_user / screenshot / live_pane click-edit convention.
        self._message_seq = message_seq
        # Pages cached per conversation so taps don't re-read the JSONL.
        self._pages: dict[_ConvKey, list[str]] = {}

    def install(self, channel: Channel) -> None:
        channel.on_command("history", self._allow_list.guard_command(self._history))
        channel.on_action(self._allow_list.guard_action(self._handle_action))

    async def build_card_for(self, sender: Person, conversation: Conversation) -> Card | None:
        """Build the history card for (sender, conversation) and
        populate the page cache so subsequent page-tap actions resolve.

        Returns the Card when a transcript was found, or None when an
        unbound / no-run / empty-history / read-failed hint was sent
        as a text message in its place. The caller (the Manage card's
        📋 History handler) edits this Card into the Manage anchor so
        the History view replaces the Manage card in place — same UX
        shape as Prefs / Commands.
        """
        inbound = Inbound(sender=sender, conversation=conversation, text="", message_id="")
        jsonl = self._resolve_jsonl(inbound)
        if jsonl is None:
            return None
        pages = self._read_and_paginate(jsonl, inbound)
        if pages is None:
            return None
        if not pages:
            self._send_text(inbound, EMPTY_HINT)
            return None
        self._pages[_key(sender.user_id, conversation)] = pages
        last = len(pages) - 1
        return _build_card(pages, last)

    async def build_card_for_path(
        self, sender: Person, conversation: Conversation, jsonl_path: Path
    ) -> Card | None:
        """Path-driven variant of `build_card_for` — paginates an
        arbitrary JSONL transcript and returns the last-page card.
        Used by `/sessions → 📦 Archive → 📖 View` so an archived
        transcript can be browsed without first restoring it.

        Same hint semantics as the bound path: empty / read-failed
        produce a text hint and return None. The pages are cached
        under the same `(user_id, chat_id, thread_id)` key as
        `build_card_for`, so opening /history and an archive view in
        the same conversation will overwrite each other's pagination
        — the older card shows `History expired — /history again`
        on next page-tap. Acceptable for v1; the alternative is
        keying on `card_anchor.message_id`, deferred.
        """
        inbound = Inbound(sender=sender, conversation=conversation, text="", message_id="")
        pages = self._read_and_paginate(jsonl_path, inbound)
        if pages is None:
            return None
        if not pages:
            self._send_text(inbound, EMPTY_HINT)
            return None
        self._pages[_key(sender.user_id, conversation)] = pages
        last = len(pages) - 1
        return _build_card(pages, last)

    # ── /history ─────────────────────────────────────────────────

    async def _history(self, inbound: Inbound, _arg: str) -> None:
        jsonl = self._resolve_jsonl(inbound)
        if jsonl is None:
            return  # hint already sent
        pages = self._read_and_paginate(jsonl, inbound)
        if pages is None:
            return  # hint already sent
        if not pages:
            self._send_text(inbound, EMPTY_HINT)
            return
        self._pages[_key(inbound.sender.user_id, inbound.conversation)] = pages
        last = len(pages) - 1
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=CardContent(card=_build_card(pages, last)),
            ),
        )

    # ── page action ──────────────────────────────────────────────

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id == ACTION_HIST_DISMISS:
            await self._channel.ack(event, "✕")
            self._outbox.enqueue_delete(event.sender, event.card_anchor)
            return
        if event.action_id != ACTION_PAGE:
            return  # ACTION_HIST_BACK is routed by SessionsService
        key = _key(event.sender.user_id, event.conversation)
        pages = self._pages.get(key)
        if pages is None:
            await self._channel.ack(event, "History expired — /history again")
            return
        try:
            page_index = int(event.value.get("i", "-1"))
        except ValueError:
            page_index = -1
        if not (0 <= page_index < len(pages)):
            await self._channel.ack(event, "Invalid page")
            return
        await self._channel.ack(event, f"Page {page_index + 1}/{len(pages)}")
        edit = Outbound(
            conversation=event.conversation,
            content=CardContent(card=_build_card(pages, page_index)),
        )
        # Direct channel.edit (not the Outbox) so the page swap rides the
        # click-response inline-refresh slot. Going through the Outbox
        # runs the edit in a later task — past the click window — and the
        # out-of-band PATCH repaints unreliably on the tapper, surfacing
        # as a brand-new card on every ◀ Older / Newer ▶ tap. Stamp the
        # seq footer here since the Outbox (which would normally stamp)
        # is bypassed.
        if self._message_seq is not None:
            edit, _ = self._message_seq.stamp_edit(
                event.sender, event.conversation, event.card_anchor, edit
            )
        await self._channel.edit(event.card_anchor, edit)

    # ── helpers ──────────────────────────────────────────────────

    def _resolve_jsonl(self, inbound: Inbound) -> Path | None:
        """Find the JSONL for the conversation's bound pane, or None.
        Side effect: sends the appropriate hint when None is returned."""
        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            self._send_text(inbound, UNBOUND_HINT)
            return None
        pointer = self._registry.get_run_pointer(pane_id)
        if pointer is None:
            self._send_text(inbound, NO_RUN_HINT)
            return None
        return transcript_path(pointer.run_id, pointer.cwd, projects_root=self._projects_root)

    def _read_and_paginate(self, jsonl: Path, inbound: Inbound) -> list[str] | None:
        """Read+parse+format+paginate. None on read error (after hint
        sent); empty list on parse-empty or missing file."""
        if not jsonl.is_file():
            return []
        try:
            text = jsonl.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("history: read %s failed: %s", jsonl, e)
            self._send_text(inbound, READ_FAILED_HINT)
            return None
        events = JsonlParser.parse(text)
        if not events:
            return []
        return list(_paginate(_format_events(events), _PAGE_LIMIT))

    def _send_text(self, inbound: Inbound, text: str) -> None:
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=TextContent(text),
            ),
        )


def _key(user_id: str, conversation: Conversation) -> _ConvKey:
    return (user_id, conversation.chat_id, conversation.thread_id or "")


def _build_card(pages: list[str], page_index: int) -> Card:
    body = pages[page_index]
    rows: list[tuple[Action, ...]] = []
    if len(pages) > 1:
        buttons: list[Action] = []
        if page_index > 0:
            buttons.append(
                Action(
                    label="◀ Older",
                    action_id=ACTION_PAGE,
                    value={"i": str(page_index - 1)},
                )
            )
        buttons.append(
            Action(
                label=f"{page_index + 1}/{len(pages)}",
                action_id=ACTION_PAGE,
                value={"i": str(page_index)},
            )
        )
        if page_index < len(pages) - 1:
            buttons.append(
                Action(
                    label="Newer ▶",
                    action_id=ACTION_PAGE,
                    value={"i": str(page_index + 1)},
                )
            )
        rows.append(tuple(buttons))
    # Nav row mirrors the Manage / Prefs cards: Back returns to
    # whichever card most likely brought the user here, Dismiss
    # deletes the card outright.
    rows.append(
        (
            Action(label="◀ Back", action_id=ACTION_HIST_BACK),
            Action(label="✕ Dismiss", action_id=ACTION_HIST_DISMISS),
        )
    )
    return Card(
        text=body,
        rows=tuple(rows),
        header_title="📜 History",
        header_color="wathet",
        # History is read-content and now length-bounded per page, so it
        # should render flat — never behind a tap-to-expand. This also
        # keeps the initial send (Outbox, which would otherwise apply the
        # per-conversation collapse pref) consistent with paged repaints
        # (direct channel.edit, which doesn't consult that pref).
        force_no_collapse=True,
    )


def _format_events(events: list[TranscriptEvent]) -> list[str]:
    """One TranscriptEvent → one rendered chunk. Empty turns dropped."""
    out: list[str] = []
    for ev in events:
        rendered = _format_event(ev)
        if rendered:
            out.append(rendered)
    return out


def _format_event(ev: TranscriptEvent) -> str:
    parts: list[str] = []
    for block in ev.blocks:
        rendered = _format_block(ev.role, block)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def _format_block(role: Role, block: Block) -> str:
    text = block.text or ""
    if block.kind is BlockKind.TEXT:
        # Assistant/user prose IS markdown — preserve it, but demote ATX
        # headings (a skill body injected by a slash command opens with
        # a `# Heading` that would dominate the card) and repair any
        # fence the clip severed.
        if role is Role.USER:
            return f"👤 {safe_clip(demote_headings(text), _TEXT_USER_LIMIT)}"
        return safe_clip(demote_headings(text), _TEXT_ASSISTANT_LIMIT)
    if block.kind is BlockKind.THINKING:
        return f"∴ Thinking…\n{safe_clip(demote_headings(text), _THINKING_LIMIT)}"
    if block.kind is BlockKind.TOOL_USE:
        # Tool arguments are raw JSON, not markdown — render verbatim so
        # stray `*`/`_`/backticks/`[N]` don't format and a long arg
        # can't sever the card.
        name = block.tool_name or "?"
        rendered_args = literal_md(text, limit=_TOOL_USE_LIMIT)
        return f"🔧 {name}\n{rendered_args}" if rendered_args else f"🔧 {name}"
    if block.kind is BlockKind.TOOL_RESULT:
        rendered = literal_md(text, limit=_TOOL_RESULT_LIMIT)
        return f"↳\n{rendered}" if rendered else "↳ _(no output)_"
    return ""


def _paginate(events: list[str], limit: int) -> Iterator[str]:
    """Group rendered events into pages of <= `limit` chars.

    Pagination is driven by length, not message count: events pack
    into a page until the next would overflow, and a single event
    that is itself larger than `limit` is split across multiple pages
    (fence-aware — see `_split_long_event`) rather than emitted as one
    oversized page that Lark would truncate.
    """
    page = ""
    for ev in events:
        if len(ev) > limit:
            # Flush whatever's buffered, then split the oversized event
            # into its own run of length-bounded pages.
            if page:
                yield page
                page = ""
            yield from _split_long_event(ev, limit)
            continue
        if not page:
            page = ev
            continue
        candidate = f"{page}\n\n{ev}"
        if len(candidate) > limit:
            yield page
            page = ev
        else:
            page = candidate
    if page:
        yield page


def _split_long_event(text: str, limit: int) -> Iterator[str]:
    """Split one over-limit rendered event into <= `limit`-char pages,
    breaking on line boundaries and keeping code fences balanced across
    the cut: a page that ends mid-fence gets a closing ```` ``` ````,
    and the continuation page reopens one so its content still renders
    as code. A single line longer than `limit` is hard-split by chars.
    """
    fence_open = False  # whether we're currently inside a ``` block
    buf: list[str] = []
    size = 0

    def flush() -> str:
        chunk = "\n".join(buf)
        if fence_open:
            chunk += "\n```"  # close the fence this page left open
        return chunk

    for raw_line in text.split("\n"):
        # A single monstrous line (e.g. a minified blob): hard-split it
        # into limit-sized pieces so no page exceeds the budget.
        pieces = _hard_split_line(raw_line, limit) if len(raw_line) + 1 > limit else [raw_line]
        for line in pieces:
            cost = len(line) + 1
            if buf and size + cost > limit:
                yield flush()
                reopen = fence_open
                buf = ["```"] if reopen else []
                size = 4 if reopen else 0
            buf.append(line)
            size += cost
            if line.strip().startswith("```"):
                fence_open = not fence_open
    if buf:
        yield flush()


def _hard_split_line(line: str, limit: int) -> list[str]:
    """Break a single over-long line into <= `limit`-char fragments.
    Fences aren't a concern here — a fence marker is a whole short
    line, never a fragment of a long one."""
    return [line[i : i + limit] for i in range(0, len(line), limit)] or [""]


__all__ = [
    "ACTION_HIST_BACK",
    "ACTION_HIST_DISMISS",
    "ACTION_PAGE",
    "EMPTY_HINT",
    "NO_RUN_HINT",
    "READ_FAILED_HINT",
    "UNBOUND_HINT",
    "HistoryService",
]
