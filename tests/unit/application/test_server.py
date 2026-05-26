"""ServerService — /server admin overview + Refresh + Kill actions."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AdminList
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.application.server import (
    ACTION_DISMISS,
    ACTION_HOST_PICK,
    ACTION_KILL,
    ACTION_OPEN_HOSTS,
    ACTION_OPEN_PANES,
    ACTION_OPEN_PROCESS,
    ACTION_OPEN_STORAGE,
    ACTION_PANE_PICK,
    ACTION_REFRESH,
    ADMIN_ONLY_HINT,
    ServerService,
)
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent, TextContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
BOB = Person(user_id="u-bob", display_name="Bob")
CONV = Conversation(chat_id="-100", thread_id="42")


@pytest.fixture
async def harness(tmp_path: Path):  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)

    # No-op dir-size so the test doesn't hit the filesystem.
    async def fake_dir_size(_path: Path) -> int:
        return 0

    service = ServerService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        admin_list=AdminList(admins=["u-alice"]),
        multiplexer_session_name="paige-test",
        projects_root=tmp_path / "projects",
        paige_dir=tmp_path / "paige",
        dir_size=fake_dir_size,
    )
    service.install(channel)

    class Harness:
        pass

    h = Harness()
    h.channel = channel  # type: ignore[attr-defined]
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _inbound(sender: Person = ALICE) -> Inbound:
    return Inbound(sender=sender, conversation=CONV, text="/server", message_id="m1")


def _action(action_id: str, sender: Person = ALICE, **value: str) -> ActionEvent:
    return ActionEvent(
        sender=sender,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="card-1"),
        action_id=action_id,
        value=dict(value),
        ack_token="tok",
    )


# ── /server gate ─────────────────────────────────────────────────


async def test_non_admin_gets_text_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_command("server", _inbound(BOB))
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == ADMIN_ONLY_HINT


async def test_admin_gets_chooser_card(harness) -> None:  # type: ignore[no-untyped-def]
    """Top-level /server is a chooser with 6 buttons in 3 rows: the 4
    drilldown categories + Refresh / Dismiss. Body is a one-liner
    so the most-glanced metrics still surface without scrolling."""
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"), multiplexer_session="paige-test")

    await h.channel.deliver_command("server", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    card = sent.content.card
    assert card.header_title == "🖥 Server"
    assert "paige" in card.text  # one-line body summary mentions paige
    rows = card.rows
    assert len(rows) == 3
    flat = [b for row in rows for b in row]
    action_ids = {b.action_id for b in flat}
    assert action_ids == {
        ACTION_OPEN_HOSTS,
        ACTION_OPEN_PANES,
        ACTION_OPEN_STORAGE,
        ACTION_OPEN_PROCESS,
        ACTION_REFRESH,
        ACTION_DISMISS,
    }


async def test_chooser_pane_count_button_label(harness) -> None:  # type: ignore[no-untyped-def]
    """The Panes button label carries the count so the user knows
    whether the listing is worth opening before drilling in."""
    h = harness
    h.mux.add_pane("@1", "alpha", Path("/a"))
    h.mux.add_pane("@2", "beta", Path("/b"))

    await h.channel.deliver_command("server", _inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    flat = [b for row in sent.content.card.rows for b in row]  # type: ignore[attr-defined]
    panes_button = next(b for b in flat if b.action_id == ACTION_OPEN_PANES)
    assert panes_button.label == "🪟 Panes (2)"


# ── 🪟 Panes sub-pane (listing + detail card) ────────────────────


async def test_open_panes_renders_listing_with_pick_rows(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "alpha", Path("/a"))
    h.mux.add_pane("@2", "beta", Path("/b"))

    await h.channel.deliver_action(_action(ACTION_OPEN_PANES))
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    pick_rows = [r for r in card.rows if r[0].action_id == ACTION_PANE_PICK]
    assert len(pick_rows) == 2
    assert {r[0].value["pane_id"] for r in pick_rows} == {"@1", "@2"}


async def test_panes_listing_marks_tracked_count(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "tracked", Path("/x"))
    h.mux.add_pane("@2", "ghost", Path("/y"))
    await h.registry.register_run("@1", "rid", Path("/x"))

    await h.channel.deliver_action(_action(ACTION_OPEN_PANES))
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    assert "2 pane" in card.text
    assert "1 resolved" in card.text or "0 resolved" in card.text


async def test_pane_pick_renders_detail_with_kill_button(harness) -> None:  # type: ignore[no-untyped-def]
    """Tap a pane row → row-detail card with primary ⚠ Kill button.
    Back routes to the Panes listing (not the chooser)."""
    h = harness
    h.mux.add_pane("@7", "myproj", Path("/p"))
    h.mux.set_foreground_pid("@7", 4242)

    await h.channel.deliver_action(_action(ACTION_PANE_PICK, pane_id="@7"))
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    flat = [b for row in card.rows for b in row]
    kill = next(b for b in flat if b.action_id == ACTION_KILL)
    assert kill.value == {"p": "@7"}
    # Back routes to the listing.
    action_ids = {b.action_id for b in flat}
    assert ACTION_OPEN_PANES in action_ids
    assert ACTION_DISMISS in action_ids
    assert "myproj" in card.text
    # PID surfaces in the detail body.
    assert "4242" in card.text


async def test_pane_pick_pane_gone_falls_back_to_listing(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    # No panes registered.
    await h.channel.deliver_action(_action(ACTION_PANE_PICK, pane_id="@404"))
    await h.outbox.stop()

    # Two acks fire: the "Pane gone — refreshing" hint, then the
    # listing-repaint ack ("🪟 Panes"). Both come through the same
    # channel call sequence — assert the diagnostic ack is present.
    ack_texts = [t for _, t in h.channel.acks]
    assert any(t and "Pane gone" in t for t in ack_texts)
    assert h.channel.edits


# ── 🖥 Hosts sub-pane ────────────────────────────────────────────


async def test_open_hosts_lists_local_synthetically(harness) -> None:  # type: ignore[no-untyped-def]
    """Without a HostsService injected the listing falls back to a
    single synthetic local row (paige's own host)."""
    h = harness
    await h.channel.deliver_action(_action(ACTION_OPEN_HOSTS))
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    pick_rows = [r for r in card.rows if r[0].action_id == ACTION_HOST_PICK]
    assert len(pick_rows) == 1
    assert pick_rows[0][0].value == {"host_id": "local"}


async def test_host_pick_renders_local_detail(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_action(_action(ACTION_HOST_PICK, host_id="local"))
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    # Local host detail surfaces paige's own pid / uptime / rss
    # — for a remote host the detail will be SSH probe results
    # instead.
    assert "pid" in card.text
    assert "uptime" in card.text


# ── 💾 Storage / ⚙ Process sub-panes ─────────────────────────────


async def test_open_storage_renders_dir_sizes(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_action(_action(ACTION_OPEN_STORAGE))
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    body = card.text
    assert "paige" in body
    assert "projects" in body
    assert "container" in body


async def test_open_process_renders_paige_pid_uptime_rss(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_action(_action(ACTION_OPEN_PROCESS))
    await h.outbox.stop()

    [(_anchor, edited)] = h.channel.edits
    card = edited.content.card  # type: ignore[attr-defined]
    body = card.text
    assert "pid" in body
    assert "uptime" in body
    assert "rss" in body
    # Trailing nav row is back/refresh/dismiss.
    nav = card.rows[-1]
    nav_ids = {b.action_id for b in nav}
    assert nav_ids == {ACTION_REFRESH, ACTION_OPEN_PROCESS, ACTION_DISMISS}


async def test_subpane_back_returns_to_chooser(harness) -> None:  # type: ignore[no-untyped-def]
    """The ◀ Back button on every sub-pane fires ACTION_REFRESH so
    the same handler that refreshes the chooser also serves as the
    universal `back` from any drilldown."""
    h = harness
    await h.channel.deliver_action(_action(ACTION_OPEN_STORAGE))
    await h.outbox.stop()
    [(_anchor, edited)] = h.channel.edits
    nav = edited.content.card.rows[-1]  # type: ignore[attr-defined]
    back = next(b for b in nav if b.label == "◀ Back")
    assert back.action_id == ACTION_REFRESH


async def test_dismiss_deletes_card(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_action(_action(ACTION_DISMISS))
    await h.outbox.stop()

    [deleted] = h.channel.deleted
    assert deleted.message_id == "card-1"


# ── refresh ──────────────────────────────────────────────────────


async def test_refresh_admin_edits_card(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "alpha", Path("/a"))
    await h.channel.deliver_command("server", _inbound())

    await h.channel.deliver_action(_action(ACTION_REFRESH))
    await h.outbox.stop()

    [(_anchor, edit_outbound)] = h.channel.edits
    assert isinstance(edit_outbound.content, CardContent)
    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "🔄"


async def test_refresh_non_admin_acks_only(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "alpha", Path("/a"))

    await h.channel.deliver_action(_action(ACTION_REFRESH, sender=BOB))
    await h.outbox.stop()

    [(_event, ack_text)] = h.channel.acks
    assert ack_text == ADMIN_ONLY_HINT
    assert h.channel.edits == []


# ── kill ─────────────────────────────────────────────────────────


async def test_kill_removes_pane_and_cascade_unbinds(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "alpha", Path("/a"))
    await h.registry.bind(ALICE, CONV, "@1")
    await h.registry.register_run("@1", "rid", Path("/a"))
    await h.channel.deliver_command("server", _inbound())

    await h.channel.deliver_action(_action(ACTION_KILL, p="@1"))
    await h.outbox.stop()

    assert "@1" in h.mux.killed
    # Binding cascades — registry no longer knows about (Alice, CONV).
    assert h.registry.get_pane(ALICE, CONV) is None
    # Card was edited (the post-kill repaint).
    assert h.channel.edits


async def test_kill_invalid_pane_id(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_action(_action(ACTION_KILL))  # no `p` value

    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "Invalid action"
    assert h.mux.killed == []


async def test_kill_already_gone_still_cleans_registry(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    # Pane exists in registry but not on the multiplexer.
    await h.registry.bind(ALICE, CONV, "@orphan")
    await h.registry.register_run("@orphan", "rid", Path("/x"))

    await h.channel.deliver_action(_action(ACTION_KILL, p="@orphan"))
    await h.outbox.stop()

    [(_event, ack_text)] = h.channel.acks
    assert ack_text == "Already gone"
    assert h.registry.get_pane(ALICE, CONV) is None


async def test_kill_non_admin_acks_only(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "alpha", Path("/a"))
    await h.channel.deliver_action(_action(ACTION_KILL, sender=BOB, p="@1"))
    await h.outbox.stop()

    [(_event, ack_text)] = h.channel.acks
    assert ack_text == ADMIN_ONLY_HINT
    assert h.mux.killed == []


# ── action routing ───────────────────────────────────────────────


async def test_other_action_ids_ignored(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_action(_action("dir:pick"))
    assert h.channel.acks == []
    assert h.channel.edits == []
