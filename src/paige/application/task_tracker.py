"""TaskTrackerService — coalesce TaskCreate/TaskUpdate spam into a task card.

Claude's task tools fire one tool_use per operation — a session can
emit dozens of `TaskCreate` + `TaskUpdate` calls, each rendered as its
own card. This service reconstructs the task list from those ops and
shows it in a single card per *group*, patched in place as tasks are
created and their status changes:

    📋 Tasks · 1/3
    ✓ #1 Add js/fs/docker.js
    🔄 #2 Wire the fs router
    ◯ #3 Unit tests

Unlike `TaskCreate`/`TaskUpdate`, the Agent tool is named `Task` /
`Agent` — a different namespace handled by `AgentBatchService`. No
overlap.

**Where the data comes from.** A task's id only appears in the
TaskCreate *result* (`Task #N created successfully: …`); the subject
is in the tool_use *input*. So a create is finalized on its result
(input subject + result id). A TaskUpdate carries `{taskId, status}`
in its input, so it's handled on the tool_use; its result is a bare
confirmation and is swallowed.

**Grouping.** New creates join the open group until that group starts
executing — the moment any of its tasks gets an update. A create that
lands after execution began opens a *fresh* group (a new card), so a
new plan/phase gets its own card rather than growing the old one.
Updates always route to the group that owns the task id, even after
that group closed to new creates.

Card edits go through the Outbox (out-of-band PATCH) — transcript-
driven, not click-driven, so no inline-refresh slot needed.

Known limitation: `_create_ids`, `_update_ids`, and `_by_id` grow for
the life of the process (updates can land long after a group closed,
so the maps can't be pruned on close). Entries are small and bounded
by the session's task count; acceptable for a debug-grade aid. A
`clear_binding` hook on unbind would cap it if needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, cast

from ..domain.card import Card
from ..domain.conversation import Anchor
from ..domain.outbound import CardContent, Outbound
from ..domain.pane import Binding
from ..domain.transcript import Block
from ..infrastructure.markdown_safe import inline_safe
from .outbox import Outbox

logger = logging.getLogger(__name__)

TASK_CREATE = "TaskCreate"
TASK_UPDATE = "TaskUpdate"
TASK_TOOL_NAMES: frozenset[str] = frozenset({TASK_CREATE, TASK_UPDATE})

_CREATED_RE = re.compile(r"#(\d+)")
_SUBJECT_CLIP = 60
_BindingKey = tuple[str, str, str]
_DELETED = "deleted"

_GLYPH = {"completed": "✓", "in_progress": "🔄", "pending": "◯"}


@dataclass
class _Task:
    id: str
    subject: str
    status: str = "pending"


@dataclass
class _Group:
    tasks: list[_Task] = field(default_factory=lambda: [])
    started: bool = False  # an update landed → new creates open a fresh group
    anchor: Anchor | None = None
    anchor_future: asyncio.Future[Anchor | None] | None = None


class TaskTrackerService:
    """Per-binding coalescing of TaskCreate/TaskUpdate into task cards."""

    def __init__(self, *, outbox: Outbox) -> None:
        self._outbox = outbox
        self._open: dict[_BindingKey, _Group] = {}
        self._by_id: dict[tuple[_BindingKey, str], _Group] = {}
        # tool_use_id → subject, stashed at TaskCreate use, read on result.
        self._pending_subject: dict[str, str] = {}
        self._create_ids: set[str] = set()
        self._update_ids: set[str] = set()

    def owns(self, tool_id: str) -> bool:
        """True for a TaskCreate/TaskUpdate tool_id — the dispatcher
        routes both their tool_results here (create → finalize, update
        → swallow) instead of the generic 1:1 morph."""
        return tool_id in self._create_ids or tool_id in self._update_ids

    async def on_use(self, bindings: list[Binding], block: Block) -> None:
        if block.tool_id is None:
            return
        if block.tool_name == TASK_CREATE:
            # Stash the subject; the task is materialised on the result,
            # which is where the assigned id appears.
            self._create_ids.add(block.tool_id)
            self._pending_subject[block.tool_id] = _parse_subject(block.text)
            return
        # TaskUpdate — input carries everything we need.
        self._update_ids.add(block.tool_id)
        task_id, status, subject = _parse_update(block.text)
        if task_id is None:
            return
        for binding in bindings:
            key = _key(binding)
            group = self._by_id.get((key, task_id))
            if group is None:
                # Update for a task we never saw created (created before
                # paige attached, or an unparsed result). Stub it into
                # the open group so the change is at least visible.
                group = self._placement_group(key)
                task = _Task(id=task_id, subject=subject or "")
                group.tasks.append(task)
                self._by_id[(key, task_id)] = group
            else:
                task = next(t for t in group.tasks if t.id == task_id)
                if subject:
                    task.subject = subject
            if status:
                task.status = status
            group.started = True
            await self._repaint(binding, group)

    async def on_result(self, bindings: list[Binding], block: Block) -> None:
        if block.tool_id is None:
            return
        if block.tool_id in self._update_ids:
            return  # TaskUpdate result is a bare confirmation — already rendered on use
        if block.tool_id not in self._create_ids:
            return
        task_id = _parse_created_id(block.text)
        subject = self._pending_subject.pop(block.tool_id, "")
        if task_id is None:
            return  # couldn't read the assigned id — skip rather than guess
        for binding in bindings:
            key = _key(binding)
            group = self._placement_group(key)
            group.tasks.append(_Task(id=task_id, subject=subject))
            self._by_id[(key, task_id)] = group
            await self._repaint(binding, group)

    # ── internals ────────────────────────────────────────────────

    def _placement_group(self, key: _BindingKey) -> _Group:
        """The group a new task joins: the open one while it's still
        being assembled, or a fresh group once execution began."""
        group = self._open.get(key)
        if group is None or group.started:
            group = _Group()
            self._open[key] = group
        return group

    async def _repaint(self, binding: Binding, group: _Group) -> None:
        outbound = Outbound(
            conversation=binding.conversation,
            content=CardContent(card=_build_card(group.tasks)),
        )
        if group.anchor is None and group.anchor_future is None:
            group.anchor_future = self._outbox.enqueue_send(binding.person, outbound)
            return
        anchor = await self._anchor(group)
        if anchor is None:
            group.anchor_future = self._outbox.enqueue_send(binding.person, outbound)
            return
        self._outbox.enqueue_edit(binding.person, anchor, outbound)

    async def _anchor(self, group: _Group) -> Anchor | None:
        if group.anchor is not None:
            return group.anchor
        if group.anchor_future is None:
            return None
        try:
            group.anchor = await group.anchor_future
        except Exception as e:  # send failed — _repaint will fresh-send
            logger.debug("task card send had no anchor: %s", e)
            group.anchor = None
        return group.anchor


def _key(binding: Binding) -> _BindingKey:
    return (
        binding.person.user_id,
        binding.conversation.chat_id,
        binding.conversation.thread_id or "",
    )


def _coerce_dict(text: str | None) -> dict[str, Any]:
    try:
        raw: Any = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}


def _clean_subject(raw: object) -> str:
    # Subjects are Claude-authored free text rendered inline next to the
    # task id — flatten newlines + drop backticks so they can't leak a
    # heading or bleed an inline-code span across the card.
    subject = inline_safe(str(raw or ""))
    return subject[: _SUBJECT_CLIP - 1] + "…" if len(subject) > _SUBJECT_CLIP else subject


def _parse_subject(text: str | None) -> str:
    return _clean_subject(_coerce_dict(text).get("subject"))


def _parse_update(text: str | None) -> tuple[str | None, str, str]:
    d = _coerce_dict(text)
    raw_id = d.get("taskId")
    task_id = str(raw_id) if raw_id is not None else None
    status = str(d.get("status") or "").strip()
    subject = _clean_subject(d.get("subject"))
    return task_id, status, subject


def _parse_created_id(result_text: str | None) -> str | None:
    """Pull the assigned id out of `Task #N created successfully: …`."""
    if not result_text:
        return None
    m = _CREATED_RE.search(result_text)
    return m.group(1) if m else None


def _build_card(tasks: list[_Task]) -> Card:
    visible = [t for t in tasks if t.status != _DELETED]
    done = sum(1 for t in visible if t.status == "completed")
    lines: list[str] = []
    for t in visible:
        glyph = _GLYPH.get(t.status, "◯")
        line = f"{glyph} #{t.id} {t.subject}".rstrip()
        lines.append(line)
    body = "\n".join(lines) if lines else "_(no tasks)_"
    return Card(
        text=body,
        header_title=f"📋 Tasks · {done}/{len(visible)}",
        header_color="wathet",
        is_status_carrier=True,
    )


__all__ = ["TASK_TOOL_NAMES", "TaskTrackerService"]
