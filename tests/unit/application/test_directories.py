"""DirectoryService — /start picker + tap-to-spawn-claude."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.directories import (
    ACTION_PICK,
    EMPTY_HINT_TMPL,
    NO_ROOT_HINT_TMPL,
    PICK_HEADER,
    DirectoryService,
)
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent, TextContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


@pytest.fixture
async def harness(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Wire DirectoryService onto fakes + a tmp projects root."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    service = DirectoryService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        channel=channel,
        allow_list=AllowList(),  # open
        projects_root=tmp_path / "projects",
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
    h.projects_root = tmp_path / "projects"  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _start_inbound() -> Inbound:
    return Inbound(sender=ALICE, conversation=CONV, text="/start", message_id="m1")


# ── /start in unbound conversation ───────────────────────────────


async def test_start_no_root_yet_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    """Projects root doesn't exist → friendly hint, no card."""
    h = harness
    # tmp_path / "projects" intentionally not created.
    await h.channel.deliver_command("start", _start_inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == NO_ROOT_HINT_TMPL.format(root=h.projects_root)


async def test_start_empty_root_sends_empty_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.projects_root.mkdir()
    await h.channel.deliver_command("start", _start_inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == EMPTY_HINT_TMPL.format(root=h.projects_root)


async def test_start_lists_directories(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.projects_root.mkdir()
    (h.projects_root / "alpha").mkdir()
    (h.projects_root / "beta").mkdir()
    (h.projects_root / "gamma").mkdir()

    await h.channel.deliver_command("start", _start_inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    assert sent.content.card.text == PICK_HEADER
    rows = sent.content.card.rows
    labels = [row[0].label for row in rows]
    # Sorted alphabetically.
    assert labels == ["📁 alpha", "📁 beta", "📁 gamma"]
    # Each row carries an index.
    indices = [row[0].value["i"] for row in rows]
    assert indices == ["0", "1", "2"]
    for row in rows:
        assert row[0].action_id == ACTION_PICK


async def test_start_skips_hidden_dirs_and_files(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.projects_root.mkdir()
    (h.projects_root / ".hidden").mkdir()
    (h.projects_root / "visible").mkdir()
    (h.projects_root / "README.md").write_text("hi")

    await h.channel.deliver_command("start", _start_inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    [row] = sent.content.card.rows
    assert row[0].label == "📁 visible"


# ── /start in bound conversation ────────────────────────────────


async def test_start_bound_shows_binding_status(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@7", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@7")

    await h.channel.deliver_command("start", _start_inbound())
    await h.outbox.stop()
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert "@7" in sent.content.text
    assert "/unbind" in sent.content.text
    # No card was rendered.
    assert all(not isinstance(o.content, CardContent) for o in h.channel.sent)


# ── pick action ──────────────────────────────────────────────────


async def _open_picker(h, dirs: list[str]) -> None:  # type: ignore[no-untyped-def]
    h.projects_root.mkdir()
    for d in dirs:
        (h.projects_root / d).mkdir()
    await h.channel.deliver_command("start", _start_inbound())
    # Don't drain outbox — the test will poke the action handler too.


def _pick_event(idx: str) -> ActionEvent:
    return ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m-card"),
        action_id=ACTION_PICK,
        value={"i": idx},
        ack_token="cbq-1",
    )


async def test_pick_spawns_pane_and_binds(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await _open_picker(h, ["alpha", "beta"])
    await h.channel.deliver_action(_pick_event("1"))  # pick "beta"
    await h.outbox.stop()

    # A pane was created via the multiplexer.
    [pane] = h.mux.created
    assert pane.pane_name == "beta"
    assert pane.cwd == h.projects_root / "beta"

    # The send_keys command spawning claude carries --session-id.
    [send] = h.mux.send_keys_calls
    assert send.text.startswith("claude --session-id ")
    assert send.enter is True

    # Registry binding established + run pointer registered.
    assert h.registry.get_pane(ALICE, CONV) == pane.pane_id
    ptr = h.registry.get_run_pointer(pane.pane_id)
    assert ptr is not None
    assert ptr.cwd == h.projects_root / "beta"
    # Run id should match the --session-id arg the multiplexer
    # received.
    sid_in_command = send.text.removeprefix("claude --session-id ")
    assert ptr.run_id == sid_in_command


async def test_pick_acks_with_dir_name(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await _open_picker(h, ["alpha"])
    await h.channel.deliver_action(_pick_event("0"))
    await h.outbox.stop()
    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "alpha" in ack_text


async def test_pick_edits_card_to_confirmation(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await _open_picker(h, ["alpha"])
    await h.channel.deliver_action(_pick_event("0"))
    await h.outbox.stop()

    [(anchor, edited)] = h.channel.edits
    assert anchor.message_id == "m-card"
    # CardContent (not TextContent) so FeishuChannel's inline-refresh
    # path patches the clicked card via the click response.
    assert isinstance(edited.content, CardContent)
    assert "alpha" in edited.content.card.text
    assert "Started" in edited.content.card.text


async def test_pick_with_invalid_index_acks_error(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await _open_picker(h, ["alpha"])
    await h.channel.deliver_action(_pick_event("99"))
    await h.outbox.stop()
    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "Invalid" in ack_text
    # No pane created, no binding.
    assert h.mux.created == []
    assert h.registry.get_pane(ALICE, CONV) is None


async def test_pick_with_no_pending_listing_acks_expired(harness) -> None:  # type: ignore[no-untyped-def]
    """Tap on a card whose listing was never seeded (e.g., the
    service was restarted between /start and the tap)."""
    h = harness
    await h.channel.deliver_action(_pick_event("0"))
    await h.outbox.stop()
    [(_event, ack_text)] = h.channel.acks
    assert ack_text is not None
    assert "expired" in ack_text.lower()


async def test_listing_is_consumed_on_successful_pick(harness) -> None:  # type: ignore[no-untyped-def]
    """A second tap with the same picker state should fail —
    successful pick consumes the listing (no double-spawn)."""
    h = harness
    await _open_picker(h, ["alpha"])
    await h.channel.deliver_action(_pick_event("0"))
    await h.channel.deliver_action(_pick_event("0"))  # second tap
    await h.outbox.stop()

    # Two panes should NOT have been created.
    assert len(h.mux.created) == 1
    # Second ack reports expired.
    last_ack = h.channel.acks[-1][1]
    assert last_ack is not None
    assert "expired" in last_ack.lower()


async def test_unrelated_action_id_passes_through(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    bad = ActionEvent(
        sender=ALICE,
        conversation=CONV,
        card_anchor=Anchor(conversation=CONV, message_id="m"),
        action_id="other:thing",
        value={},
        ack_token="cbq",
    )
    await h.channel.deliver_action(bad)
    assert h.channel.acks == []


# ── installation ────────────────────────────────────────────────


async def test_install_registers_start_and_action_handler(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    assert "start" in h.channel._command_handlers  # noqa: SLF001
    assert len(h.channel._action_handlers) >= 1  # noqa: SLF001
