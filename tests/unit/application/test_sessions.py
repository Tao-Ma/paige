"""SessionsService — /sessions list + tap-to-bind action."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.history import HistoryService
from paige.application.message_seq import MessageSeqService
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.application.sessions import (
    ACTION_ACTIVE_PICK,
    ACTION_ARCHIVE_PICK,
    ACTION_ARCHIVE_RESTORE,
    ACTION_ARCHIVE_VIEW,
    ACTION_BIND,
    ACTION_DORMANT_ARCHIVE,
    ACTION_DORMANT_DELETE,
    ACTION_DORMANT_PICK,
    ACTION_MANAGE_BACK,
    ACTION_MANAGE_CMD,
    ACTION_MANAGE_COMMANDS,
    ACTION_MANAGE_DISMISS,
    ACTION_MANAGE_HISTORY,
    ACTION_MANAGE_PREFS,
    ACTION_MANAGE_UNBIND,
    ACTION_NEW_PICK,
    ACTION_NEW_START,
    ACTION_OPEN_ACTIVE,
    ACTION_OPEN_ARCHIVE,
    ACTION_OPEN_NEW,
    ACTION_OPEN_RESUME,
    ACTION_PREFS_BACK,
    ACTION_PREFS_MSG_SEQ,
    ACTION_PREFS_TOGGLE,
    ACTION_RESUME,
    ACTION_SESSIONS_REFRESH,
    SessionsService,
)
from paige.application.verbosity import ContentKind, Verbosity, VerbosityService
from paige.domain.card import Action, ActionCell, ActionEvent, Card
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent, TextContent
from paige.domain.person import Person
from paige.infrastructure.sessions_index import DormantSession
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


async def _empty_index(_root: Path, _excluded: frozenset[str]) -> list[DormantSession]:
    """Default test index — never finds anything on disk."""
    return []


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    history_service = HistoryService(
        registry=registry,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
    )
    history_service.install(channel)
    verbosity = VerbosityService()
    message_seq = MessageSeqService()
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),  # open
        history_service=history_service,
        verbosity=verbosity,
        message_seq=message_seq,
        dormant_index=_empty_index,
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
    h.history_service = history_service  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _inbound() -> Inbound:
    return Inbound(sender=ALICE, conversation=CONV, text="/sessions", message_id="m1")


# ── /sessions chooser (top-level category card) ─────────────────


async def test_chooser_renders_card_when_empty(harness) -> None:  # type: ignore[no-untyped-def]
    """Even with zero active and zero dormant, the chooser still
    renders a card (so the user can tap 🆕 New) — no TextContent
    empty-hint fallback like the pre-redesign version had."""
    h = harness
    await h.channel.deliver_command("sessions", _inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    flat = [b for row in sent.content.card.rows for b in row]
    assert any("🆕 New" in b.label for b in flat)
    # Body advertises the New path as the next step.
    assert "none yet" in sent.content.card.text.lower()


async def test_chooser_has_six_buttons_in_three_rows(harness) -> None:  # type: ignore[no-untyped-def]
    """Layout: ● Active(N) | ○ Resume(M) / 🆕 New | 📦 Archive(K) /
    🔄 Refresh | ✕ Dismiss."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.register_run("@1", "sid", Path("/p"))

    await h.channel.deliver_command("sessions", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    rows = sent.content.card.rows
    assert len(rows) == 3
    assert len(rows[0]) == 2
    assert len(rows[1]) == 2
    assert len(rows[2]) == 2
    action_ids = {b.action_id for row in rows for b in row}
    assert action_ids == {
        ACTION_OPEN_ACTIVE,
        ACTION_OPEN_RESUME,
        ACTION_OPEN_NEW,
        ACTION_OPEN_ARCHIVE,
        ACTION_SESSIONS_REFRESH,
        ACTION_MANAGE_DISMISS,
    }


async def test_chooser_body_shows_active_count(harness) -> None:  # type: ignore[no-untyped-def]
    """The body advertises counts so the user knows whether tapping
    Active / Resume will land on something."""
    h = harness
    h.mux.add_pane("@1", "p1", Path("/p"))
    h.mux.add_pane("@2", "p2", Path("/p"))
    await h.registry.register_run("@1", "sid-a", Path("/p"))
    await h.registry.register_run("@2", "sid-b", Path("/p"))

    await h.channel.deliver_command("sessions", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert "2 active" in sent.content.card.text


async def test_chooser_active_button_label_carries_count(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p1", Path("/p"))
    h.mux.add_pane("@2", "p2", Path("/p"))
    await h.registry.register_run("@1", "sid-a", Path("/p"))
    await h.registry.register_run("@2", "sid-b", Path("/p"))

    await h.channel.deliver_command("sessions", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    flat = [b for row in sent.content.card.rows for b in row]
    active_button = next(b for b in flat if b.action_id == ACTION_OPEN_ACTIVE)
    assert active_button.label == "● Active (2)"


async def test_chooser_skips_panes_without_run_pointer(harness) -> None:  # type: ignore[no-untyped-def]
    """A pane that hasn't been registered as a run yet doesn't count
    toward the Active(N) badge — RunDiscovery hadn't paired it with
    a live JSONL."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    # No register_run call.
    await h.channel.deliver_command("sessions", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    flat = [b for row in sent.content.card.rows for b in row]
    active_button = next(b for b in flat if b.action_id == ACTION_OPEN_ACTIVE)
    assert active_button.label == "● Active (0)"


# ── Active sub-pane (after tapping ● Active(N)) ─────────────────


def _open_event(action_id: str) -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-chooser"),
        action_id=action_id,
        value={},
        ack_token="cbq-open",
    )


async def test_open_active_renders_listing_sorted_by_cwd(harness) -> None:  # type: ignore[no-untyped-def]
    """ACTION_OPEN_ACTIVE → Active sub-pane listing of panes ordered
    by cwd path. Trailing nav: Refresh / Back / Dismiss."""
    h = harness
    h.mux.add_pane("@1", "z-proj", Path("/p/z-proj"))
    h.mux.add_pane("@2", "a-proj", Path("/p/a-proj"))
    await h.registry.register_run("@1", "sid-z", Path("/p/z-proj"))
    await h.registry.register_run("@2", "sid-a", Path("/p/a-proj"))

    await h.channel.deliver_action(_open_event(ACTION_OPEN_ACTIVE))

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-chooser"
    assert isinstance(edited.content, CardContent)
    card = edited.content.card
    pick_rows = [row for row in card.rows if row[0].action_id == ACTION_ACTIVE_PICK]
    assert len(pick_rows) == 2
    # Ordered by cwd: /p/a-proj before /p/z-proj.
    assert pick_rows[0][0].value == {"pane_id": "@2"}
    assert pick_rows[1][0].value == {"pane_id": "@1"}
    nav = card.rows[-1]
    assert {b.action_id for b in nav} == {
        ACTION_OPEN_ACTIVE,  # 🔄 Refresh re-renders self
        ACTION_SESSIONS_REFRESH,  # ◀ Back returns to chooser
        ACTION_MANAGE_DISMISS,
    }


async def test_open_active_with_no_panes_shows_empty_body(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_action(_open_event(ACTION_OPEN_ACTIVE))
    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    assert "No active sessions" in card.text


# ── Resume sub-pane (after tapping ○ Resume(M)) ─────────────────


async def test_open_resume_renders_listing_sorted_by_cwd(harness) -> None:  # type: ignore[no-untyped-def]
    """ACTION_OPEN_RESUME → Resume sub-pane listing of dormants."""
    h = harness

    async def fixed_index(_root: Path, _excluded: frozenset[str]) -> list[DormantSession]:
        return [
            DormantSession(
                session_id="sid-z",
                cwd=Path("/p/z-old"),
                file_path=Path("/x/z.jsonl"),
                message_count=12,
                mtime=0.0,
                summary="",
            ),
            DormantSession(
                session_id="sid-a",
                cwd=Path("/p/a-old"),
                file_path=Path("/x/a.jsonl"),
                message_count=3,
                mtime=0.0,
                summary="",
            ),
        ]

    h.service._ctx.dormant_index = fixed_index  # type: ignore[attr-defined]  # noqa: SLF001
    await h.channel.deliver_action(_open_event(ACTION_OPEN_RESUME))
    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    pick_actions = [
        cell.action
        for row in card.column_set_rows
        for cell in row
        if isinstance(cell, ActionCell) and cell.action.action_id == ACTION_DORMANT_PICK
    ]
    assert len(pick_actions) == 2
    # Ordered by cwd alphabetically.
    assert pick_actions[0].value["sid"] == "sid-a"
    assert pick_actions[1].value["sid"] == "sid-z"
    nav = card.rows[-1]
    assert {b.action_id for b in nav} == {
        ACTION_OPEN_RESUME,
        ACTION_SESSIONS_REFRESH,
        ACTION_MANAGE_DISMISS,
    }


# ── New sub-pane (after tapping 🆕 New) ─────────────────────────


async def test_open_new_lists_subdirectories(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """ACTION_OPEN_NEW → directory listing under new_projects_root."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()  # excluded by list_subdirs

    h = await _build_harness_with_new_root(tmp_path)
    try:
        await h.channel.deliver_action(_open_event(ACTION_OPEN_NEW))
        [(_anchor, edited)] = h.channel.edits
        card = edited.content.card  # type: ignore[attr-defined]
        pick_rows = [row for row in card.rows if row[0].action_id == ACTION_NEW_PICK]
        labels = sorted(row[0].label for row in pick_rows)
        assert labels == ["📁 alpha", "📁 beta"]
        nav = card.rows[-1]
        assert {b.action_id for b in nav} == {
            ACTION_OPEN_NEW,
            ACTION_SESSIONS_REFRESH,
            ACTION_MANAGE_DISMISS,
        }
    finally:
        await h.outbox.stop()


async def test_open_new_with_missing_root_shows_hint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = await _build_harness_with_new_root(tmp_path / "does-not-exist")
    try:
        await h.channel.deliver_action(_open_event(ACTION_OPEN_NEW))
        [(_anchor, edited)] = h.channel.edits
        card = edited.content.card  # type: ignore[attr-defined]
        assert "not found" in card.text.lower()
    finally:
        await h.outbox.stop()


async def test_new_pick_renders_confirmation_card(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Tap on a directory in the New sub-pane → confirmation card
    with 🚀 Start carrying the cwd path; not yet a fresh pane spawn."""
    (tmp_path / "myproj").mkdir()
    h = await _build_harness_with_new_root(tmp_path)
    try:
        cwd = tmp_path / "myproj"
        event = ActionEvent(
            sender=ALICE,
            conversation=CONV,
            card_anchor=Anchor(conversation=CONV, message_id="m-new"),
            action_id=ACTION_NEW_PICK,
            value={"cwd": str(cwd)},
            ack_token="cbq-np",
        )
        await h.channel.deliver_action(event)
        [(_anchor, edited)] = h.channel.edits
        card = edited.content.card  # type: ignore[attr-defined]
        flat = [b for row in card.rows for b in row]
        start = next(b for b in flat if b.action_id == ACTION_NEW_START)
        assert start.value == {"cwd": str(cwd)}
        # No spawn yet — confirmation step.
        assert h.mux.created == []
    finally:
        await h.outbox.stop()


async def test_new_start_spawns_pane_and_binds(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """ACTION_NEW_START commits the spawn: create_pane(claude
    --session-id ...), register_run, bind, edit anchor to ✓ Started."""
    (tmp_path / "myproj").mkdir()
    h = await _build_harness_with_new_root(tmp_path)
    try:
        cwd = tmp_path / "myproj"
        event = ActionEvent(
            sender=ALICE,
            conversation=CONV,
            card_anchor=Anchor(conversation=CONV, message_id="m-conf"),
            action_id=ACTION_NEW_START,
            value={"cwd": str(cwd)},
            ack_token="cbq-ns",
        )
        await h.channel.deliver_action(event)
        await h.outbox.stop()

        # Spawned a pane in the chosen cwd with the claude command.
        [created] = h.mux.created
        assert created.cwd == cwd
        # The send_keys call carries `claude --session-id <UUID>`
        # in dashed 8-4-4-4-12 form. claude rejects bare-hex with
        # "Invalid session ID. Must be a valid UUID." — regression
        # against that bug.
        [send_call] = h.mux.send_keys_calls
        assert send_call.text.startswith("claude --session-id ")
        sid = send_call.text.removeprefix("claude --session-id ")
        assert len(sid) == 36
        assert sid.count("-") == 4
        # Binding recorded against the new pane.
        assert h.registry.get_pane(ALICE, CONV) == created.pane_id
        # Card edited to ✓ Started confirmation.
        edits = [(a, e) for a, e in h.channel.edits if a.message_id == "m-conf"]
        assert len(edits) == 1
        _anchor, edited = edits[0]
        assert isinstance(edited.content, CardContent)
        assert "Started" in edited.content.card.text
    finally:
        pass  # outbox already stopped above


async def _build_harness_with_new_root(new_projects_root: Path):  # type: ignore[no-untyped-def]
    """Build a fresh harness with `new_projects_root` overridden so
    the New sub-pane scans a known temp directory in tests."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        new_projects_root=new_projects_root,
        dormant_index=_empty_index,
    )
    service.install(channel)

    class H:
        pass

    h = H()
    h.channel = channel  # type: ignore[attr-defined]
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    h.service = service  # type: ignore[attr-defined]
    return h


# ── bind action ──────────────────────────────────────────────────


def _bind_event(pane_id: str = "@1") -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-card"),
        action_id=ACTION_BIND,
        value={"pane_id": pane_id},
        ack_token="cbq-1",
    )


async def test_bind_action_records_binding(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))

    await h.channel.deliver_action(_bind_event("@1"))
    await h.outbox.stop()

    assert h.registry.get_pane(ALICE, CONV) == "@1"


async def test_bind_action_acks_with_pane_name(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@9", "myproj", Path("/p"))

    await h.channel.deliver_action(_bind_event("@9"))
    await h.outbox.stop()

    [(event, ack_text)] = h.channel.acks
    assert event.action_id == ACTION_BIND
    assert ack_text is not None
    assert "myproj" in ack_text


async def test_bind_action_edits_card_to_confirmation(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@9", "myproj", Path("/p"))

    await h.channel.deliver_action(_bind_event("@9"))
    await h.outbox.stop()

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-card"
    # CardContent (not TextContent) — the FeishuChannel inline-refresh
    # path needs a card to repaint the clicked surface atomically.
    assert isinstance(edited.content, CardContent)
    assert "Bound" in edited.content.card.text
    assert "myproj" in edited.content.card.text


async def test_bind_action_unknown_pane_acks_error(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    # Pane @99 doesn't exist on the multiplexer.
    await h.channel.deliver_action(_bind_event("@99"))
    await h.outbox.stop()

    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "Pane not found" in ack_text or "refresh" in ack_text.lower()
    assert h.registry.get_pane(ALICE, CONV) is None


async def test_bind_action_missing_pane_id_value_acks_invalid(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    bad_event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m"),
        action_id=ACTION_BIND,
        value={},  # missing pane_id
        ack_token="cbq-2",
    )
    await h.channel.deliver_action(bad_event)
    await h.outbox.stop()
    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "Invalid" in ack_text or "invalid" in ack_text


async def test_unrelated_action_id_is_ignored(harness) -> None:  # type: ignore[no-untyped-def]
    """Other services' action_ids should pass through SessionsService
    untouched."""
    h = harness
    other_event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m"),
        action_id="other:thing",
        value={},
        ack_token="cbq-x",
    )
    await h.channel.deliver_action(other_event)
    assert h.channel.acks == []
    assert h.registry.get_pane(ALICE, CONV) is None


# ── installation ────────────────────────────────────────────────


async def test_install_registers_command_and_action_handler(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    assert "sessions" in h.channel._command_handlers  # noqa: SLF001
    assert len(h.channel._action_handlers) >= 1  # noqa: SLF001


# ── allow-list gate ─────────────────────────────────────────────


async def test_disallowed_sender_cannot_run_sessions_or_bind() -> None:
    """A closed AllowList blocks both /sessions invocation and a
    bind action from a non-listed sender."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    allow = AllowList(["u-only-alice"])
    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=allow
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=allow,
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
    )
    service.install(channel)

    mux.add_pane("@1", "p", Path("/p"))
    await registry.register_run("@1", "sid", Path("/p"))

    bob = Person(user_id="u-bob")
    bob_inbound = Inbound(sender=bob, conversation=CONV, text="/sessions", message_id="m")
    bob_action = ActionEvent(
        sender=bob,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="9"),
        action_id=ACTION_BIND,
        value={"pane_id": "@1"},
        ack_token="t",
    )

    await channel.deliver_command("sessions", bob_inbound)
    await channel.deliver_action(bob_action)
    await outbox.stop()

    assert channel.sent == []
    assert channel.acks == []
    assert registry.get_pane(bob, CONV) is None


# Use Action import to silence ruff F401 on the Action symbol.
_action_ref = Action


# ── dormant sessions ─────────────────────────────────────────────


def _dormant(sid: str, cwd: str = "/p", count: int = 3) -> DormantSession:
    return DormantSession(
        session_id=sid,
        cwd=Path(cwd),
        summary=f"summary for {sid}",
        mtime=0.0,
        message_count=count,
        file_path=Path(cwd) / f"{sid}.jsonl",
    )


async def _build_with_dormants(
    dormants: list[DormantSession],
):  # type: ignore[no-untyped-def]
    """Spin up a SessionsService that returns `dormants` from its index."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)

    async def index(_root: Path, excluded: frozenset[str]) -> list[DormantSession]:
        return [d for d in dormants if d.session_id not in excluded]

    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        dormant_index=index,
    )
    service.install(channel)
    return channel, mux, registry, outbox, service


async def test_resume_subpane_lists_dormants() -> None:
    """The Resume sub-pane lists dormants ordered by cwd as
    ACTION_DORMANT_PICK rows; each row carries sid + cwd + file_path
    so the row-detail card has everything for Resume / Delete
    without re-resolving."""
    channel, mux, _registry, outbox, _service = await _build_with_dormants(
        [_dormant("s1", "/proj/a"), _dormant("s2", "/proj/b")]
    )
    try:
        evt = ActionEvent(
            sender=ALICE,
            conversation=CONV,
            card_anchor=Anchor(conversation=CONV, message_id="m-chooser"),
            action_id=ACTION_OPEN_RESUME,
            value={},
            ack_token="t",
        )
        await channel.deliver_action(evt)

        [(_anchor, edited)] = channel.edits
        assert isinstance(edited.content, CardContent)
        card = edited.content.card
        pick_buttons = [
            cell.action
            for row in card.column_set_rows
            for cell in row
            if isinstance(cell, ActionCell) and cell.action.action_id == ACTION_DORMANT_PICK
        ]
        assert {b.value["sid"] for b in pick_buttons} == {"s1", "s2"}
        assert all("file_path" in b.value for b in pick_buttons)
    finally:
        del mux


async def test_chooser_dormant_count_excludes_active_run_ids() -> None:
    """A live tracked pane must not double-count its JSONL as a
    dormant — same logic the legacy chooser had, just enforced
    against the chooser body's count + the Resume sub-pane listing."""
    channel, mux, registry, outbox, _ = await _build_with_dormants(
        [_dormant("live"), _dormant("dead")]
    )
    try:
        mux.add_pane("@1", "p", Path("/p"))
        await registry.register_run("@1", "live", Path("/p"))
        evt = ActionEvent(
            sender=ALICE,
            conversation=CONV,
            card_anchor=Anchor(conversation=CONV, message_id="m-chooser"),
            action_id=ACTION_OPEN_RESUME,
            value={},
            ack_token="t",
        )
        await channel.deliver_action(evt)

        [(_anchor, edited)] = channel.edits
        card = edited.content.card  # type: ignore[attr-defined]
        sids = {
            cell.action.value.get("sid")
            for row in card.column_set_rows
            for cell in row
            if isinstance(cell, ActionCell) and cell.action.action_id == ACTION_DORMANT_PICK
        }
        assert sids == {"dead"}
    finally:
        pass


async def test_chooser_body_shows_both_active_and_dormant_counts() -> None:
    channel, mux, registry, outbox, _ = await _build_with_dormants([_dormant("dorm-1")])
    try:
        mux.add_pane("@1", "live-pane", Path("/p"))
        await registry.register_run("@1", "live-rid", Path("/p"))
        await channel.deliver_command(
            "sessions",
            Inbound(sender=ALICE, conversation=CONV, text="/sessions", message_id="m"),
        )
        await outbox.stop()

        [sent] = channel.sent
        assert isinstance(sent.content, CardContent)
        body = sent.content.card.text
        assert "1 active" in body
        assert "1 dormant" in body
        flat = [b for row in sent.content.card.rows for b in row]
        active_button = next(b for b in flat if b.action_id == ACTION_OPEN_ACTIVE)
        resume_button = next(b for b in flat if b.action_id == ACTION_OPEN_RESUME)
        assert "(1)" in active_button.label
        assert "(1)" in resume_button.label
    finally:
        pass


async def test_resume_action_spawns_pane_and_binds(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    channel, mux, registry, outbox, _ = await _build_with_dormants([_dormant("sid-x", str(cwd))])
    try:
        resume = ActionEvent(
            sender=ALICE,
            conversation=CONV,
            card_anchor=Anchor(conversation=CONV, message_id="m-card"),
            action_id=ACTION_RESUME,
            value={"sid": "sid-x", "cwd": str(cwd)},
            ack_token="t",
        )
        await channel.deliver_action(resume)
        await outbox.stop()

        [pane] = mux.created
        assert pane.cwd == cwd
        # Multiplexer's create_pane(command=...) records the command
        # send via send_keys.
        [send] = mux.send_keys_calls
        assert send.text == "claude --resume sid-x"
        # Conversation got bound to the new pane.
        assert registry.get_pane(ALICE, CONV) == pane.pane_id
        [(_event, ack)] = channel.acks
        assert ack is not None
        assert "Resuming" in ack
        # Card edited to confirmation.
        assert channel.edits
    finally:
        pass


async def test_resume_falls_back_to_home_when_cwd_missing(tmp_path: Path) -> None:
    """If the encoded cwd doesn't actually exist (the inverse mapping
    is lossy), spawn in $HOME so tmux's startup doesn't fail."""
    missing_cwd = tmp_path / "does-not-exist"
    channel, mux, _registry, outbox, _ = await _build_with_dormants(
        [_dormant("s", str(missing_cwd))]
    )
    try:
        await channel.deliver_action(
            ActionEvent(
                sender=ALICE,
                conversation=CONV,
                card_anchor=Anchor(conversation=CONV, message_id="card"),
                action_id=ACTION_RESUME,
                value={"sid": "s", "cwd": str(missing_cwd)},
                ack_token="t",
            )
        )
        await outbox.stop()

        [pane] = mux.created
        # Fallback chosen because tmp_path/does-not-exist isn't a dir.
        assert pane.cwd != missing_cwd
        # Either Path.home() or a sensible substitute.
        assert pane.cwd.exists() or pane.cwd == Path.home()
    finally:
        pass


async def test_resume_invalid_action_value() -> None:
    channel, _mux, _registry, outbox, _ = await _build_with_dormants([])
    try:
        bad = ActionEvent(
            sender=ALICE,
            conversation=CONV,
            card_anchor=Anchor(conversation=CONV, message_id="m"),
            action_id=ACTION_RESUME,
            value={"sid": "", "cwd": ""},  # both blank
            ack_token="t",
        )
        await channel.deliver_action(bad)
        [(_event, ack)] = channel.acks
        assert ack == "Invalid action"
    finally:
        pass


# ── sub-pane drilldown (active / dormant) ───────────────────────


async def test_active_pick_repaints_into_detail_card(harness) -> None:  # type: ignore[no-untyped-def]
    """Tapping an active row should edit the listing anchor in place
    into the row-detail card (Bind / Refresh / Back / Dismiss).
    History was relocated to the Manage card (`/session`) — keeps
    the detail surface tight per the user's spec."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.register_run("@7", "sid", Path("/p"))

    pick = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-active"),
        action_id=ACTION_ACTIVE_PICK,
        value={"pane_id": "@7"},
        ack_token="t",
    )
    await h.channel.deliver_action(pick)
    await h.outbox.stop()

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-active"
    assert isinstance(edited.content, CardContent)
    card = edited.content.card
    assert "myproj" in card.text
    action_ids = {b.action_id for row in card.rows for b in row}
    assert ACTION_BIND in action_ids
    assert ACTION_MANAGE_HISTORY not in action_ids  # moved to /session
    assert ACTION_OPEN_ACTIVE in action_ids  # Back returns to listing
    assert ACTION_MANAGE_DISMISS in action_ids


async def test_active_pick_pane_gone_falls_back_to_listing(harness) -> None:  # type: ignore[no-untyped-def]
    """If the picked pane vanished between list and tap, repaint the
    Active sub-pane listing into the same anchor instead of
    dead-ending."""
    h = harness
    # No pane registered → find_pane returns None.
    pick = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-active"),
        action_id=ACTION_ACTIVE_PICK,
        value={"pane_id": "@404"},
        ack_token="t",
    )
    await h.channel.deliver_action(pick)
    await h.outbox.stop()

    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "Pane gone" in ack_text or "refreshing" in ack_text.lower()
    # An edit was attempted to the same anchor.
    assert h.channel.edits
    [(anchor, _edited)] = h.channel.edits
    assert anchor.message_id == "m-active"


async def test_dormant_pick_repaints_into_detail_card(tmp_path: Path) -> None:
    """Tapping a dormant row opens the row-detail card with Resume +
    Delete. Back routes to the Resume sub-pane listing (not the
    top-level chooser)."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    file_path = tmp_path / "-proj" / "sid-x.jsonl"
    channel, _mux, _registry, outbox, _service = await _build_with_dormants([])
    try:
        pick = ActionEvent(
            sender=ALICE,
            conversation=CONV,
            card_anchor=Anchor(conversation=CONV, message_id="m-resume"),
            action_id=ACTION_DORMANT_PICK,
            value={"sid": "sid-x", "cwd": str(cwd), "file_path": str(file_path)},
            ack_token="t",
        )
        await channel.deliver_action(pick)
        await outbox.stop()

        [(anchor, edited)] = channel.edits
        assert anchor.message_id == "m-resume"
        assert isinstance(edited.content, CardContent)
        card = edited.content.card
        action_ids = {b.action_id for row in card.rows for b in row}
        assert ACTION_RESUME in action_ids
        assert ACTION_DORMANT_ARCHIVE in action_ids
        assert ACTION_DORMANT_DELETE in action_ids
        assert ACTION_OPEN_RESUME in action_ids  # Back returns to listing
        assert ACTION_MANAGE_DISMISS in action_ids
        flat = [b for row in card.rows for b in row]
        resume = next(b for b in flat if b.action_id == ACTION_RESUME)
        archive = next(b for b in flat if b.action_id == ACTION_DORMANT_ARCHIVE)
        delete = next(b for b in flat if b.action_id == ACTION_DORMANT_DELETE)
        assert resume.value == {"sid": "sid-x", "cwd": str(cwd)}
        assert archive.value == {"file_path": str(file_path)}
        assert delete.value == {"file_path": str(file_path)}
    finally:
        pass


async def test_dormant_pick_invalid_value_acks(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    bad = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m"),
        action_id=ACTION_DORMANT_PICK,
        value={},  # missing sid/cwd/file_path
        ack_token="t",
    )
    await h.channel.deliver_action(bad)
    [(_event, ack)] = h.channel.acks
    assert ack == "Invalid action"
    assert h.channel.edits == []


async def test_dormant_delete_unlinks_and_repaints(tmp_path: Path) -> None:
    """Delete on the dormant sub-pane unlinks the JSONL and repaints
    the chooser in place — the deleted row should disappear."""
    proj = tmp_path / "-proj"
    proj.mkdir()
    f = proj / "sid-x.jsonl"
    f.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
    assert f.is_file()

    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)

    async def index(_root: Path, _excluded: frozenset[str]) -> list[DormantSession]:
        # Re-scan disk on each call so the post-delete render reflects
        # the unlink.
        if f.exists():
            return [
                DormantSession(
                    session_id="sid-x",
                    cwd=tmp_path / "proj",
                    summary="hi",
                    mtime=0.0,
                    message_count=1,
                    file_path=f,
                )
            ]
        return []

    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        claude_projects_root=tmp_path,
        dormant_index=index,
    )
    service.install(channel)

    delete = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-sub"),
        action_id=ACTION_DORMANT_DELETE,
        value={"file_path": str(f)},
        ack_token="t",
    )
    await channel.deliver_action(delete)
    await outbox.stop()

    assert not f.exists()
    [(_event, ack)] = channel.acks
    assert ack is not None
    assert "Deleted" in ack
    # Repaint into the same anchor — chooser is now empty.
    [(anchor, edited)] = channel.edits
    assert anchor.message_id == "m-sub"
    assert isinstance(edited.content, CardContent)


# ── archive flow ─────────────────────────────────────────────────


async def _build_with_archive_root(
    *,
    claude_root: Path,
):
    """Spin up a SessionsService rooted at `claude_root/projects` with
    archive_root at `claude_root/archive`. Uses real fs primitives so
    the archive/restore moves are observable on disk."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        claude_projects_root=claude_root / "projects",
    )
    service.install(channel)
    return channel, outbox, service


async def test_dormant_archive_moves_file_and_repaints(tmp_path: Path) -> None:
    """📦 Archive on the dormant detail card moves the JSONL into the
    sibling archive root and repaints the chooser. Source disappears,
    destination exists, ack reports 'Archived'."""
    proj = tmp_path / "projects" / "-proj"
    proj.mkdir(parents=True)
    f = proj / "sid-x.jsonl"
    f.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')

    channel, outbox, _ = await _build_with_archive_root(claude_root=tmp_path)

    evt = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-detail"),
        action_id=ACTION_DORMANT_ARCHIVE,
        value={"file_path": str(f)},
        ack_token="t",
    )
    await channel.deliver_action(evt)
    await outbox.stop()

    assert not f.exists()
    assert (tmp_path / "archive" / "-proj" / "sid-x.jsonl").is_file()
    [(_event, ack)] = channel.acks
    assert ack is not None and "Archived" in ack
    [(anchor, edited)] = channel.edits
    assert anchor.message_id == "m-detail"
    assert isinstance(edited.content, CardContent)


async def test_open_archive_lists_archived_sessions(tmp_path: Path) -> None:
    """Tapping 📦 Archive on the chooser opens the Archive sub-pane;
    the listing has one ACTION_ARCHIVE_PICK row per archived JSONL."""
    archive = tmp_path / "archive" / "-proj"
    archive.mkdir(parents=True)
    (archive / "sid-a.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"a"}}\n'
    )
    (archive / "sid-b.jsonl").write_text(
        '{"type":"user","message":{"role":"user","content":"b"}}\n'
    )

    channel, outbox, _ = await _build_with_archive_root(claude_root=tmp_path)

    evt = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-chooser"),
        action_id=ACTION_OPEN_ARCHIVE,
        value={},
        ack_token="t",
    )
    await channel.deliver_action(evt)
    await outbox.stop()

    [(_anchor, edited)] = channel.edits
    assert isinstance(edited.content, CardContent)
    card = edited.content.card
    pick_sids = {
        cell.action.value["sid"]
        for row in card.column_set_rows
        for cell in row
        if isinstance(cell, ActionCell) and cell.action.action_id == ACTION_ARCHIVE_PICK
    }
    assert pick_sids == {"sid-a", "sid-b"}


async def test_archive_restore_moves_file_back(tmp_path: Path) -> None:
    """♻ Restore on the archive detail card moves the JSONL back to
    its projects subdir and repaints the archive sub-pane in place."""
    archive = tmp_path / "archive" / "-proj"
    archive.mkdir(parents=True)
    f = archive / "sid-x.jsonl"
    f.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')

    channel, outbox, _ = await _build_with_archive_root(claude_root=tmp_path)

    evt = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-detail"),
        action_id=ACTION_ARCHIVE_RESTORE,
        value={"file_path": str(f)},
        ack_token="t",
    )
    await channel.deliver_action(evt)
    await outbox.stop()

    assert not f.exists()
    assert (tmp_path / "projects" / "-proj" / "sid-x.jsonl").is_file()
    [(_event, ack)] = channel.acks
    assert ack is not None and "Restored" in ack


async def test_archive_view_sends_history_card(tmp_path: Path) -> None:
    """📖 View on the archive detail card builds a History card from
    the archived JSONL and sends it as a *new* outbound (the detail
    card stays in place — no edit to its anchor)."""
    archive = tmp_path / "archive" / "-proj"
    archive.mkdir(parents=True)
    f = archive / "sid-x.jsonl"
    f.write_text(
        '{"type":"user","message":{"role":"user","content":"hello from archive"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}\n'
    )

    channel, outbox, _ = await _build_with_archive_root(claude_root=tmp_path)

    evt = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-detail"),
        action_id=ACTION_ARCHIVE_VIEW,
        value={"file_path": str(f)},
        ack_token="t",
    )
    await channel.deliver_action(evt)
    await outbox.stop()

    # No edit to the detail card — the History card lands as a fresh send.
    assert channel.edits == []
    [sent] = channel.sent
    assert isinstance(sent.content, CardContent)
    assert "hello from archive" in sent.content.card.text


async def test_sessions_refresh_repaints_chooser(harness) -> None:  # type: ignore[no-untyped-def]
    """Refresh button edits the chooser anchor with a fresh render —
    the active count in the body should reflect the latest state."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.register_run("@7", "sid", Path("/p"))

    refresh = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-chooser"),
        action_id=ACTION_SESSIONS_REFRESH,
        value={},
        ack_token="t",
    )
    await h.channel.deliver_action(refresh)
    await h.outbox.stop()

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-chooser"
    assert isinstance(edited.content, CardContent)
    card = edited.content.card
    assert "1 active" in (card.text or "")
    flat = [b for row in card.rows for b in row]
    assert any(b.action_id == ACTION_OPEN_ACTIVE for b in flat)


async def test_sessions_refresh_empty_still_paints_chooser(harness) -> None:  # type: ignore[no-untyped-def]
    """When everything's gone, refresh still repaints the chooser
    with the empty-body hint and Dismiss in the trailing nav row —
    no TextContent fallback (would 400 on a cross-type PATCH)."""
    h = harness
    refresh = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-chooser"),
        action_id=ACTION_SESSIONS_REFRESH,
        value={},
        ack_token="t",
    )
    await h.channel.deliver_action(refresh)
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    assert isinstance(edited.content, CardContent)
    flat = [b for row in edited.content.card.rows for b in row]
    assert any(b.action_id == ACTION_MANAGE_DISMISS for b in flat)
    assert any(b.action_id == ACTION_OPEN_NEW for b in flat)


# ── /session Manage card ────────────────────────────────────────


def _session_inbound() -> Inbound:
    return Inbound(sender=ALICE, conversation=CONV, text="/session", message_id="m1")


async def test_session_unbound_falls_through_to_sessions(harness) -> None:  # type: ignore[no-untyped-def]
    """Without a binding, /session shouldn't dead-end — it should
    open the chooser. The chooser is the new category card; the
    empty body hint advertises 🆕 New as the next step."""
    h = harness

    await h.channel.deliver_command("session", _session_inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert "none yet" in sent.content.card.text.lower()


async def test_session_bound_renders_manage_card(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p/myproj"))
    await h.registry.bind(ALICE, CONV, "@7")
    await h.registry.register_run("@7", "sid-x", Path("/p/myproj"))

    await h.channel.deliver_command("session", _session_inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    card = sent.content.card
    assert "myproj" in card.text
    assert "@7" in card.text
    assert "sid-x"[:8] in card.text
    # Manage rows: Unbind + History | Commands + Prefs | Back + Dismiss
    # → 3 rows, all bisected. The 5 forwarded slash-commands moved to a
    # Commands sub-pane (test_session_commands_subpane_*).
    assert len(card.rows) == 3
    assert all(len(row) <= 2 for row in card.rows)
    action_ids = {b.action_id for row in card.rows for b in row}
    assert action_ids == {
        ACTION_MANAGE_UNBIND,
        ACTION_MANAGE_HISTORY,
        ACTION_MANAGE_COMMANDS,
        ACTION_MANAGE_PREFS,
        ACTION_MANAGE_BACK,
        ACTION_MANAGE_DISMISS,
    }
    # No forwarded-command buttons appear directly on Manage anymore;
    # they're behind the Commands sub-pane.
    assert not any(b.action_id == ACTION_MANAGE_CMD for row in card.rows for b in row)


def _manage_event(action_id: str) -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-manage"),
        action_id=action_id,
        value={},
        ack_token="cbq",
    )


async def test_manage_unbind_clears_binding_and_edits_card(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_UNBIND))
    await h.outbox.stop()

    assert h.registry.get_pane(ALICE, CONV) is None
    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-manage"
    assert isinstance(edited.content, CardContent)
    assert "Unbound" in edited.content.card.text


async def test_manage_unbind_when_already_unbound_acks_quietly(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_UNBIND))
    await h.outbox.stop()

    [(_event_, ack)] = h.channel.acks
    assert ack is not None
    assert "Already unbound" in ack


async def test_manage_back_re_renders_chooser(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.register_run("@1", "sid", Path("/p"))

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_BACK))
    await h.outbox.stop()

    # Back triggers /sessions; with one active run, we should see the
    # chooser with the Open Active button (rows are now categories,
    # not sessions).
    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert any(b.action_id == ACTION_OPEN_ACTIVE for row in sent.content.card.rows for b in row)


async def test_manage_dismiss_deletes_card(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_DISMISS))
    await h.outbox.stop()

    [deleted] = h.channel.deleted
    assert deleted.message_id == "m-manage"


async def test_manage_cmd_sends_slash_command_to_pane(harness) -> None:  # type: ignore[no-untyped-def]
    """Quick-action button forwards the slash command verbatim — same
    behavior as typing `/clear` in IM, just one tap. Manage card
    stays open so the user can fire several in a row."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-manage"),
        action_id=ACTION_MANAGE_CMD,
        value={"cmd": "clear"},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)
    await h.outbox.stop()

    [call] = h.mux.send_keys_calls
    assert call.pane_id == "@7"
    assert call.text == "/clear"
    assert call.enter is True
    assert call.literal is True
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "/clear"
    # Manage card was NOT edited or deleted — stays open.
    assert h.channel.edits == []
    assert h.channel.deleted == []


async def test_manage_cmd_without_binding_acks_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-manage"),
        action_id=ACTION_MANAGE_CMD,
        value={"cmd": "clear"},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)

    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "No bound pane"
    assert h.mux.send_keys_calls == []


# ── Preferences sub-panel ────────────────────────────────────────


async def test_manage_prefs_opens_preferences_card(harness) -> None:  # type: ignore[no-untyped-def]
    """Tapping ⚙ Preferences edits the Manage card in place into the
    sub-panel — same anchor, no second card stacked in the thread."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_PREFS))

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-manage"
    assert isinstance(edited.content, CardContent)
    card = edited.content.card
    assert card.header_title == "⚙ Prefs"
    # Each toggle button advertises the current state in its label.
    flat = [b for row in card.rows for b in row]
    toggle_labels = [b.label for b in flat if b.action_id == ACTION_PREFS_TOGGLE]
    assert any("📝 Replies: FULL" in label for label in toggle_labels)
    assert any("🔧 Tool calls: FULL" in label for label in toggle_labels)
    assert any("📤 Tool output: FULL" in label for label in toggle_labels)


async def test_prefs_toggle_flips_verbosity_and_repaints(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.registry.bind(ALICE, CONV, "@7")

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-prefs"),
        action_id=ACTION_PREFS_TOGGLE,
        value={"kind": ContentKind.TOOL_USE.value},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)

    # Default was FULL; toggle flipped to BRIEF.
    assert (
        h.service._manage._verbosity.get(ALICE, CONV, ContentKind.TOOL_USE)  # noqa: SLF001
        is Verbosity.BRIEF
    )
    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-prefs"
    card = edited.content.card  # type: ignore[attr-defined]
    assert card.header_title == "⚙ Prefs"
    flat = [b for row in card.rows for b in row]
    args_label = next(
        b.label
        for b in flat
        if b.action_id == ACTION_PREFS_TOGGLE and b.value.get("kind") == ContentKind.TOOL_USE.value
    )
    assert "BRIEF" in args_label
    [(_event, ack_text)] = h.channel.acks
    assert "tool_use" in (ack_text or "")
    assert "brief" in (ack_text or "")


async def test_prefs_msg_seq_toggle_flips_state(harness) -> None:  # type: ignore[no-untyped-def]
    """The msg-seq button toggles the per-(person, conversation)
    flag on MessageSeqService and re-renders the Prefs card with
    the new state in its label."""
    h = harness
    await h.registry.bind(ALICE, CONV, "@7")

    # Start: off.
    assert h.service._ctx.message_seq.is_enabled(ALICE, CONV) is False  # noqa: SLF001

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-prefs"),
        action_id=ACTION_PREFS_MSG_SEQ,
        value={},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)

    # Toggle is now on.
    assert h.service._ctx.message_seq.is_enabled(ALICE, CONV) is True  # noqa: SLF001
    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    flat = [b for row in card.rows for b in row]
    seq_button = next(b for b in flat if b.action_id == ACTION_PREFS_MSG_SEQ)
    assert seq_button.label == "🔢 Msg seq: ON"
    [(_event, ack_text)] = h.channel.acks
    assert "msg seq" in (ack_text or "")
    assert "on" in (ack_text or "")


async def test_prefs_back_returns_to_manage(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-prefs"),
        action_id=ACTION_PREFS_BACK,
        value={},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-prefs"
    card = edited.content.card  # type: ignore[attr-defined]
    # Manage card has the ⚡ pane header.
    assert card.header_title.startswith("⚡ ")


# ── 🛠 Commands sub-pane ─────────────────────────────────────────


async def test_manage_commands_opens_commands_subpane(harness) -> None:  # type: ignore[no-untyped-def]
    """Tapping 🛠 Commands edits the Manage card in place into the
    Commands sub-pane — the 5 forwarded slash-commands appear as
    buttons there. Mirrors the Prefs sub-pane shape."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_COMMANDS))

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-manage"
    assert isinstance(edited.content, CardContent)
    card = edited.content.card
    assert card.header_title == "🛠 Commands"
    cmd_values = sorted(
        b.value["cmd"] for row in card.rows for b in row if b.action_id == ACTION_MANAGE_CMD
    )
    assert cmd_values == ["clear", "compact", "cost", "memory", "model"]
    # Back routes through the shared ACTION_PREFS_BACK so the same
    # repaint handler returns to Manage in place.
    action_ids = {b.action_id for row in card.rows for b in row}
    assert ACTION_PREFS_BACK in action_ids
    assert ACTION_MANAGE_DISMISS in action_ids


async def test_commands_subpane_back_returns_to_manage(harness) -> None:  # type: ignore[no-untyped-def]
    """The Back button on the Commands sub-pane carries
    ACTION_PREFS_BACK so the Manage repaint handler runs verbatim
    — exercise that path explicitly to lock the contract."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-cmds"),
        action_id=ACTION_PREFS_BACK,
        value={},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    assert card.header_title.startswith("⚡ ")  # Manage header


async def test_commands_subpane_cmd_button_still_forwards(harness) -> None:  # type: ignore[no-untyped-def]
    """Clicking a forwarded-command button from inside the Commands
    sub-pane goes through the same ACTION_MANAGE_CMD handler as the
    pre-refactor card — only the placement moved, the click contract
    didn't."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-cmds"),
        action_id=ACTION_MANAGE_CMD,
        value={"cmd": "compact"},
        ack_token="cbq",
    )
    await h.channel.deliver_action(event)
    await h.outbox.stop()

    [call] = h.mux.send_keys_calls
    assert call.text == "/compact"


# ── Manage host badge (multi-host UX scaffold) ──────────────────


async def test_manage_card_no_badge_when_single_host(harness) -> None:  # type: ignore[no-untyped-def]
    """The default `harness` doesn't wire a HostsService, so single-
    host installs (today's only mode) shouldn't see a `🖥 …` badge —
    showing `🖥 local` on every Manage card would be noise."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p/myproj"))
    await h.registry.bind(ALICE, CONV, "@7")
    await h.registry.register_run("@7", "sid-x", Path("/p/myproj"))

    await h.channel.deliver_command("session", _session_inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert "🖥" not in sent.content.card.text


async def test_manage_card_shows_host_badge_when_multi_host() -> None:
    """Once `~/.paige/hosts.toml` lists at least one remote host,
    the Manage card body grows a `🖥 {host_name}` badge so the user
    knows which box they're operating on. Single-host wins remain
    quiet (covered by `test_manage_card_no_badge_when_single_host`)."""
    from paige.application.hosts import HostsService
    from paige.domain.host import Host

    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    hosts = HostsService([Host(host_id="dev-1", name="Dev box")])
    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        hosts=hosts,
        dormant_index=_empty_index,
    )
    service.install(channel)
    mux.add_pane("@7", "myproj", Path("/p/myproj"))
    await registry.bind(ALICE, CONV, "@7", host_id="dev-1")
    await registry.register_run("@7", "sid-x", Path("/p/myproj"), host_id="dev-1")

    await channel.deliver_command("session", _session_inbound())
    await outbox.stop()

    [sent] = channel.sent
    assert isinstance(sent.content, CardContent)
    assert "🖥 Dev box" in sent.content.card.text


async def test_sessions_multi_host_renders_overview() -> None:
    """When ≥2 hosts are configured (HostsService.list() > 1), the
    top-level /sessions entry renders the host-overview card —
    each host as a row, plus a Refresh / Dismiss nav row."""
    from paige.application.hosts import HostsService
    from paige.application.sessions import (
        ACTION_OPEN_HOST,
        ACTION_OPEN_OVERVIEW,
    )
    from paige.domain.host import Host

    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    hosts = HostsService([Host(host_id="dev-1", name="Dev box")])
    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        hosts=hosts,
        dormant_index=_empty_index,
    )
    service.install(channel)

    await channel.deliver_command("sessions", _inbound())
    await outbox.stop()

    [sent] = channel.sent
    assert isinstance(sent.content, CardContent)
    card = sent.content.card
    assert card.header_title == "🔗 Sessions"
    flat = [b for row in card.rows for b in row]
    host_picks = [b for b in flat if b.action_id == ACTION_OPEN_HOST]
    # Both `local` and `dev-1` get a row.
    assert len(host_picks) == 2
    host_ids = {b.value["host_id"] for b in host_picks}
    assert host_ids == {"local", "dev-1"}
    # Local marked online (●), dev-1 marked disconnected (placeholder
    # until SSH adapters land).
    local_label = next(b.label for b in host_picks if b.value["host_id"] == "local")
    dev1_label = next(b.label for b in host_picks if b.value["host_id"] == "dev-1")
    assert "●" in local_label
    assert "disconnected" in dev1_label
    # Trailing nav row uses ACTION_OPEN_OVERVIEW for self-refresh.
    nav_ids = {b.action_id for b in card.rows[-1]}
    assert nav_ids == {ACTION_OPEN_OVERVIEW, ACTION_MANAGE_DISMISS}


async def test_sessions_single_host_still_renders_chooser(harness) -> None:  # type: ignore[no-untyped-def]
    """Zero-regression check: a single-host install (no HostsService
    or just `local`) sees the existing category chooser, never the
    overview."""
    from paige.application.sessions import (
        ACTION_OPEN_ACTIVE,
        ACTION_OPEN_HOST,
    )

    h = harness  # default fixture has no hosts injected
    await h.channel.deliver_command("sessions", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    flat = [b for row in sent.content.card.rows for b in row]  # type: ignore[attr-defined]
    action_ids = {b.action_id for b in flat}
    # Existing chooser action, not overview.
    assert ACTION_OPEN_ACTIVE in action_ids
    assert ACTION_OPEN_HOST not in action_ids


async def test_open_host_event_renders_chooser() -> None:
    """Tapping a host row in the overview opens the category chooser
    into the same anchor. Today the chooser is host-agnostic — the
    SSH-slice will add filtering by host_id."""
    from paige.application.hosts import HostsService
    from paige.application.sessions import ACTION_OPEN_ACTIVE, ACTION_OPEN_HOST
    from paige.domain.host import Host

    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    hosts = HostsService([Host(host_id="dev-1")])
    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        hosts=hosts,
        dormant_index=_empty_index,
    )
    service.install(channel)

    event = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-overview"),
        action_id=ACTION_OPEN_HOST,
        value={"host_id": "dev-1"},
        ack_token="cbq",
    )
    await channel.deliver_action(event)

    [(_anchor, edited)] = channel.edits
    flat = [b for row in edited.content.card.rows for b in row]  # type: ignore[attr-defined]
    # The chooser's hallmark Active/Resume/New buttons appear.
    assert any(b.action_id == ACTION_OPEN_ACTIVE for b in flat)


async def test_manage_card_badge_falls_back_to_host_id() -> None:
    """A configured host without an explicit `name` shows its
    `host_id` as the badge text (Host.display_name fallback)."""
    from paige.application.hosts import HostsService
    from paige.domain.host import Host

    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    hosts = HostsService([Host(host_id="lab-2")])  # no name field
    history_service = HistoryService(
        registry=registry, outbox=outbox, channel=channel, allow_list=AllowList()
    )
    history_service.install(channel)
    service = SessionsService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),
        history_service=history_service,
        verbosity=VerbosityService(),
        message_seq=MessageSeqService(),
        hosts=hosts,
        dormant_index=_empty_index,
    )
    service.install(channel)
    mux.add_pane("@7", "myproj", Path("/p/myproj"))
    await registry.bind(ALICE, CONV, "@7", host_id="lab-2")
    await registry.register_run("@7", "sid-x", Path("/p/myproj"), host_id="lab-2")

    await channel.deliver_command("session", _session_inbound())
    await outbox.stop()

    [sent] = channel.sent
    assert isinstance(sent.content, CardContent)
    assert "🖥 lab-2" in sent.content.card.text


async def test_manage_history_unbound_sends_hint_and_leaves_anchor(harness) -> None:  # type: ignore[no-untyped-def]
    """Tap 📋 History with no binding → HistoryService sends UNBOUND_HINT
    as a text message and the Manage anchor stays untouched (no edit).
    The text hint is observable evidence that the delegation ran; the
    absent edit confirms `_on_manage_history` correctly skips the
    anchor repaint when `build_card_for` returns None."""
    h = harness
    # No binding → HistoryService.UNBOUND_HINT is the response.

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_HISTORY))
    await h.outbox.stop()

    assert any(
        isinstance(o.content, TextContent) and "session bound" in o.content.text.lower()
        for o in h.channel.sent
    )
    assert h.channel.edits == [], "Manage anchor must not be edited on the unbound path"


async def test_manage_history_bound_edits_manage_anchor_in_place(harness) -> None:  # type: ignore[no-untyped-def]
    """Tap 📋 History with a built card → the Manage anchor is edited
    in place with the History card (same anchor for the whole sub-flow,
    matching the Prefs / Commands repaint-in-place shape).

    Uses a stub `build_card_for` so the test focuses on the dispatch
    wiring; the full read+paginate flow is exercised by test_history.py.
    """
    h = harness
    stub_card = Card(text="📜 history body", header_title="📜 History", header_color="wathet")

    async def _stub_build(_sender, _conv):  # type: ignore[no-untyped-def]
        return stub_card

    h.history_service.build_card_for = _stub_build  # type: ignore[method-assign]

    await h.channel.deliver_action(_manage_event(ACTION_MANAGE_HISTORY))
    await h.outbox.stop()

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-manage"
    assert isinstance(edited.content, CardContent)
    assert edited.content.card is stub_card
