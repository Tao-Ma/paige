"""HistoryService — /history paginated transcript card."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.history import (
    ACTION_HIST_DISMISS,
    ACTION_PAGE,
    EMPTY_HINT,
    NO_RUN_HINT,
    READ_FAILED_HINT,
    UNBOUND_HINT,
    HistoryService,
    _paginate,
    _split_long_event,
)
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent, TextContent
from paige.domain.person import Person
from paige.infrastructure.transcript_path import transcript_path
from paige.testing.fakes import FakeChannel, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


@pytest.fixture
async def harness(tmp_path: Path):  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = HistoryService(
        registry=registry,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        projects_root=tmp_path,
    )
    service.install(channel)

    class Harness:
        pass

    h = Harness()
    h.channel = channel  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    h.service = service  # type: ignore[attr-defined]
    h.projects_root = tmp_path  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _inbound(text: str = "/history") -> Inbound:
    return Inbound(sender=ALICE, conversation=CONV, text=text, message_id="m1")


def _action_page(page_index: int, anchor_id: str = "card-1") -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id=anchor_id),
        action_id=ACTION_PAGE,
        value={"i": str(page_index)},
        ack_token="tok",
    )


def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines))


def _user_event(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _assistant_tool(name: str, tool_id: str, input_obj: object) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}],
        },
    }


def _tool_result(tool_id: str, text: str) -> dict[str, object]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}],
        },
    }


# ── /history error paths ─────────────────────────────────────────


async def test_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == UNBOUND_HINT


async def test_bound_no_run_pointer_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.registry.bind(ALICE, CONV, "@1")
    # No register_run, so no run pointer.
    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == NO_RUN_HINT


async def test_jsonl_missing_sends_empty_hint(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    # JSONL never created — empty result.

    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == EMPTY_HINT


# ── happy path ───────────────────────────────────────────────────


async def test_renders_card_with_last_page(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    _write_jsonl(
        jsonl,
        [
            _user_event("hello world"),
            _assistant_text("hi back!"),
        ],
    )

    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    body = sent.content.card.text
    assert "👤 hello world" in body
    assert "hi back!" in body


async def test_formats_tool_use_and_result(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    _write_jsonl(
        jsonl,
        [
            _assistant_tool("Bash", "tu_1", {"command": "ls -la"}),
            _tool_result("tu_1", "total 0\ndrwx ..."),
        ],
    )

    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    body = sent.content.card.text
    # Tool name on its own line; args rendered as literal (inline code
    # for the short single-line JSON), so raw markdown chars can't leak.
    assert "🔧 Bash" in body
    assert "`{" in body and "ls -la" in body
    # Multi-line tool output rendered in a fenced block.
    assert "↳" in body
    assert "```\ntotal 0\ndrwx ...\n```" in body
    # No unbalanced fences anywhere in the body.
    assert body.count("```") % 2 == 0


async def test_paginates_long_transcript(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    # 20 events of ~500 chars each → multiple pages at the 3500-char limit.
    big = "x" * 500
    _write_jsonl(jsonl, [_assistant_text(big) for _ in range(20)])

    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    rows = sent.content.card.rows
    # At least one nav row with a page indicator.
    assert rows
    labels = [b.label for b in rows[0]]
    # Should be on the LAST page → only "Older" + page indicator.
    assert any("◀" in label for label in labels)
    assert not any("Newer" in label for label in labels)


# ── length-driven pagination (unit) ──────────────────────────────


async def test_page_tap_stamps_seq_footer_when_enabled(tmp_path: Path) -> None:
    """The page-tap repaint bypasses the Outbox (direct channel.edit),
    so HistoryService must stamp the seq footer itself when seq debug
    is on — matching the ask_user / screenshot click-edit convention."""
    from paige.application.message_seq import MessageSeqService

    channel = FakeChannel()
    registry = RunRegistry(FakeStorage())
    await registry.load()
    outbox = Outbox(channel)
    seq = MessageSeqService()
    seq.toggle(ALICE, CONV)  # enable
    service = HistoryService(
        registry=registry,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        projects_root=tmp_path,
        message_seq=seq,
    )
    service.install(channel)
    await registry.bind(ALICE, CONV, "@1")
    await registry.register_run("@1", "rid", tmp_path / "proj")
    jsonl = transcript_path("rid", tmp_path / "proj", projects_root=tmp_path)
    _write_jsonl(jsonl, [_assistant_text("x" * 500) for _ in range(20)])  # multi-page

    await channel.deliver_command("history", _inbound())
    await service._handle_action(_action_page(0))  # noqa: SLF001
    await outbox.stop()

    assert channel.edits, "page tap should edit in place"
    _anchor, edited = channel.edits[-1]
    assert isinstance(edited.content, CardContent)
    assert "_seq #" in edited.content.card.text


def test_paginate_splits_a_single_oversized_event() -> None:
    """One event larger than the limit must span multiple pages —
    never a single over-limit page."""
    big = "\n".join(f"line {i} " + "z" * 40 for i in range(200))
    pages = list(_paginate([big], limit=500))
    assert len(pages) > 1
    assert all(len(p) <= 500 for p in pages)


def test_paginate_packs_small_events_by_length() -> None:
    events = ["a" * 100 for _ in range(10)]
    pages = list(_paginate(events, limit=350))
    # Each page packs ~3 events (3*100 + separators) under 350.
    assert len(pages) >= 3
    assert all(len(p) <= 350 for p in pages)


def test_split_long_event_keeps_fences_balanced_across_pages() -> None:
    """A code fence that spans a page boundary is closed on the page
    it overflows and reopened on the next, so no page leaks an open
    fence into the card."""
    code = "\n".join(f"code line {i}" for i in range(100))
    event = f"intro\n```python\n{code}\n```\noutro"
    pages = list(_split_long_event(event, limit=300))
    assert len(pages) > 1
    for p in pages:
        assert p.count("```") % 2 == 0, p
    # Continuation pages reopen a fence so their content renders as code.
    assert any(p.startswith("```") for p in pages[1:])


def test_split_long_event_hard_splits_a_monster_line() -> None:
    event = "y" * 2000  # one line, no newlines
    pages = list(_split_long_event(event, limit=300))
    assert len(pages) > 1
    assert all(len(p) <= 300 for p in pages)
    assert "".join(pages) == event


# ── pagination tap ───────────────────────────────────────────────


async def test_page_tap_edits_card(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    big = "y" * 500
    _write_jsonl(jsonl, [_assistant_text(big) for _ in range(20)])

    await h.channel.deliver_command("history", _inbound())
    # Tap "Older" → page 0. Pages are cached synchronously before
    # the send, so the action handler finds them without needing
    # the outbox to drain first.
    await h.channel.deliver_action(_action_page(0))
    await h.outbox.stop()

    assert h.channel.edits, "expected an edit on page tap"
    [(_anchor, edit_outbound)] = h.channel.edits
    assert isinstance(edit_outbound.content, CardContent)
    # Page-0 card has a Newer button (we're at the start now).
    rows = edit_outbound.content.card.rows
    assert rows
    labels = [b.label for b in rows[0]]
    assert any("Newer" in label for label in labels)


async def test_page_tap_without_history_fails_gracefully(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    # Action without preceding /history → should ack with "expired".
    await h.channel.deliver_action(_action_page(0))
    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "expired" in ack_text.lower()
    assert h.channel.edits == []


async def test_page_tap_invalid_index(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    _write_jsonl(jsonl, [_assistant_text("only one page worth")])

    await h.channel.deliver_command("history", _inbound())
    await h.channel.deliver_action(_action_page(99))

    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "Invalid page"
    assert h.channel.edits == []


async def test_card_carries_back_and_dismiss_row(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Every History card ends with ◀ Back / ✕ Dismiss so the user
    isn't stuck reading transcript with no escape route."""
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    _write_jsonl(jsonl, [_assistant_text("hi")])

    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    rows = sent.content.card.rows  # type: ignore[union-attr]
    # Last row is always the nav row.
    nav_labels = [b.label for b in rows[-1]]
    assert "◀ Back" in nav_labels
    assert "✕ Dismiss" in nav_labels


async def test_dismiss_deletes_card(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    _write_jsonl(jsonl, [_assistant_text("a")])

    await h.channel.deliver_command("history", _inbound())
    dismiss = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-hist"),
        action_id=ACTION_HIST_DISMISS,
        value={},
        ack_token="tok",
    )
    await h.channel.deliver_action(dismiss)
    await h.outbox.stop()

    [deleted] = h.channel.deleted
    assert deleted.message_id == "m-hist"


async def test_other_action_ignored(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    _write_jsonl(jsonl, [_assistant_text("a")])

    await h.channel.deliver_command("history", _inbound())

    other = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="x"),
        action_id="dir:pick",  # someone else's action_id
        value={"i": "0"},
        ack_token="tok",
    )
    await h.channel.deliver_action(other)
    # Our service should silently bail; no ack, no edit.
    assert h.channel.edits == []
    assert h.channel.acks == []


# ── read errors ──────────────────────────────────────────────────


async def test_read_error_sends_hint(harness, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = harness
    cwd = tmp_path / "proj"
    cwd.mkdir()
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", cwd)
    jsonl = transcript_path("rid", cwd, projects_root=h.projects_root)
    _write_jsonl(jsonl, [_assistant_text("a")])

    real_read_text = Path.read_text

    def fail_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == jsonl:
            raise OSError("denied")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fail_read)

    await h.channel.deliver_command("history", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == READ_FAILED_HINT
