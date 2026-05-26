"""TaskTrackerService — coalesce TaskCreate/TaskUpdate into task cards."""

from __future__ import annotations

import json

from paige.application.outbox import Outbox
from paige.application.task_tracker import TaskTrackerService
from paige.domain.conversation import Conversation
from paige.domain.outbound import CardContent
from paige.domain.pane import Binding
from paige.domain.person import Person
from paige.domain.transcript import Block, BlockKind
from paige.testing.fakes import FakeChannel

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="t1")
BINDING = Binding(person=ALICE, conversation=CONV, pane_id="@1")


def _create_use(tool_id: str, subject: str) -> Block:
    return Block(
        kind=BlockKind.TOOL_USE,
        tool_id=tool_id,
        tool_name="TaskCreate",
        text=json.dumps({"subject": subject, "description": "d"}),
    )


def _create_result(tool_id: str, n: int, subject: str) -> Block:
    return Block(
        kind=BlockKind.TOOL_RESULT,
        tool_id=tool_id,
        text=f"Task #{n} created successfully: {subject}",
    )


def _update_use(tool_id: str, task_id: str, status: str) -> Block:
    return Block(
        kind=BlockKind.TOOL_USE,
        tool_id=tool_id,
        tool_name="TaskUpdate",
        text=json.dumps({"taskId": task_id, "status": status}),
    )


async def _svc() -> tuple[FakeChannel, Outbox, TaskTrackerService]:
    channel = FakeChannel()
    outbox = Outbox(channel)
    return channel, outbox, TaskTrackerService(outbox=outbox)


def _last_text(channel: FakeChannel) -> str:
    events: list[object] = list(channel.sent)
    events += [ob for _a, ob in channel.edits]
    last = events[-1]
    assert isinstance(last.content, CardContent)  # type: ignore[attr-defined]
    return last.content.card.text  # type: ignore[attr-defined]


async def _create(svc: TaskTrackerService, tool_id: str, n: int, subject: str) -> None:
    await svc.on_use([BINDING], _create_use(tool_id, subject))
    await svc.on_result([BINDING], _create_result(tool_id, n, subject))


async def test_create_burst_is_one_card() -> None:
    channel, outbox, svc = await _svc()
    await _create(svc, "c1", 1, "Add docker.js")
    await _create(svc, "c2", 2, "Wire router")
    await _create(svc, "c3", 3, "Tests")
    await outbox.stop()

    # First create sends the card; the next two patch it.
    assert len(channel.sent) == 1
    assert len(channel.edits) == 2
    body = _last_text(channel)
    assert "#1 Add docker.js" in body
    assert "#2 Wire router" in body
    assert "#3 Tests" in body
    assert body.count("◯") == 3  # all pending


async def test_update_changes_status_in_place() -> None:
    channel, outbox, svc = await _svc()
    await _create(svc, "c1", 1, "Add docker.js")
    await _create(svc, "c2", 2, "Wire router")
    await svc.on_use([BINDING], _update_use("u1", "1", "in_progress"))
    await svc.on_use([BINDING], _update_use("u2", "2", "completed"))
    await outbox.stop()

    # Still one card (no new sends after the first create).
    assert len(channel.sent) == 1
    last = channel.edits[-1][1]
    assert isinstance(last.content, CardContent)
    assert "🔄 #1" in last.content.card.text
    assert "✓ #2" in last.content.card.text
    assert last.content.card.header_title == "📋 Tasks · 1/2"


async def test_create_after_execution_starts_new_card() -> None:
    channel, outbox, svc = await _svc()
    await _create(svc, "c1", 1, "First plan task")
    await svc.on_use([BINDING], _update_use("u1", "1", "in_progress"))  # execution begins
    await _create(svc, "c2", 2, "New phase task")  # create after exec → new group
    await outbox.stop()

    # Two cards: one per group.
    assert len(channel.sent) == 2
    bodies = [o.content.card.text for o in channel.sent if isinstance(o.content, CardContent)]
    assert any("First plan task" in b for b in bodies)
    assert any("New phase task" in b for b in bodies)


async def test_update_to_old_group_patches_its_card_not_the_new_one() -> None:
    channel, outbox, svc = await _svc()
    await _create(svc, "c1", 1, "Old")
    await svc.on_use([BINDING], _update_use("u1", "1", "in_progress"))
    await _create(svc, "c2", 2, "New")  # opens group B
    # Now complete the OLD task #1 — must patch group A's card.
    await svc.on_use([BINDING], _update_use("u2", "1", "completed"))
    await outbox.stop()

    # The final edit completed #1 (group A), so some edit shows ✓ #1.
    edit_bodies = [
        ob.content.card.text for _a, ob in channel.edits if isinstance(ob.content, CardContent)
    ]
    assert any("✓ #1 Old" in b for b in edit_bodies)


async def test_deleted_task_dropped_completed_kept() -> None:
    channel, outbox, svc = await _svc()
    await _create(svc, "c1", 1, "Keep me done")
    await _create(svc, "c2", 2, "Delete me")
    await svc.on_use([BINDING], _update_use("u1", "1", "completed"))
    await svc.on_use([BINDING], _update_use("u2", "2", "deleted"))
    await outbox.stop()

    body = _last_text(channel)
    assert "✓ #1 Keep me done" in body  # completed kept
    assert "Delete me" not in body  # deleted dropped


async def test_orphan_update_is_stubbed() -> None:
    channel, outbox, svc = await _svc()
    # Update for a task whose create we never saw.
    await svc.on_use([BINDING], _update_use("u1", "9", "in_progress"))
    await outbox.stop()
    body = _last_text(channel)
    assert "🔄 #9" in body


async def test_owns_tracks_create_and_update_ids() -> None:
    _channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _create_use("c1", "x"))
    await svc.on_use([BINDING], _update_use("u1", "1", "pending"))
    await outbox.stop()
    assert svc.owns("c1")
    assert svc.owns("u1")
    assert not svc.owns("other")


async def test_unparseable_create_result_is_skipped() -> None:
    channel, outbox, svc = await _svc()
    await svc.on_use([BINDING], _create_use("c1", "x"))
    await svc.on_result(
        [BINDING], Block(kind=BlockKind.TOOL_RESULT, tool_id="c1", text="weird output, no id")
    )
    await outbox.stop()
    # Nothing rendered — we couldn't determine the id.
    assert channel.sent == []
    assert channel.edits == []
