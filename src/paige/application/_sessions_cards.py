"""Pure card builders for the /sessions and /session surfaces.

These are leaf-level functions on dataclasses + primitives — no
`SessionsService` `self` dependency. Split out of `sessions.py` so
the service module stays focused on dispatch + handler logic.

All builders return a `Card`; the layout/copy lives here, the
side-effecting bits (channel.edit, registry mutations, multiplexer
spawns) stay in `SessionsService`.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.card import Action, Card
from ..domain.conversation import Conversation
from ..domain.host import Host
from ..domain.person import Person
from ._sessions_actions import (
    ACTION_ACTIVE_PICK,
    ACTION_ARCHIVE_RESTORE,
    ACTION_ARCHIVE_VIEW,
    ACTION_BIND,
    ACTION_DORMANT_ARCHIVE,
    ACTION_DORMANT_DELETE,
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
    ACTION_PREFS_COLLAPSE,
    ACTION_PREFS_MSG_SEQ,
    ACTION_PREFS_TOGGLE,
    ACTION_RESUME,
    ACTION_SESSIONS_REFRESH,
)
from .collapse_pref import CollapsePrefService
from .message_seq import MessageSeqService
from .run_registry import RunPointer
from .verbosity import ContentKind, VerbosityService

# Verbosity toggle order for the Preferences card. Matches the
# render-block kinds the user can dial. Labels describe the slice
# of *Claude's streaming output* each toggle controls — paige's
# own command cards (/sessions, /history, /server, the spinner,
# /screenshot, …) always render full and aren't on this dial.
_PREFS_KINDS: tuple[tuple[ContentKind, str], ...] = (
    (ContentKind.TEXT, "📝 Replies"),
    (ContentKind.TOOL_USE, "🔧 Tool calls"),
    (ContentKind.TOOL_RESULT, "📤 Tool output"),
)

# Forwarded command name → button label. Same set as
# `commands.FORWARDED_COMMANDS`; duplicated here only because the
# button layout cares about the order/labels and a registry import
# loop would otherwise be needed. Labels read as plain verbs/nouns
# (no `/cmd` syntax) so they don't look like raw shell commands —
# users still type `/clear` etc. directly in IM if they prefer.
_MANAGE_CMD_BUTTONS: tuple[tuple[str, str], ...] = (
    ("clear", "🧹 Clear"),
    ("compact", "📦 Compact"),
    ("cost", "💰 Cost"),
    ("memory", "🧠 Memory"),
    ("model", "🤖 Model"),
)


def subpane_nav(*, self_action: str) -> tuple[Action, ...]:
    """Trailing nav row for sub-pane listing cards: ◀ Back / 🔄 Refresh
    / ✕ Dismiss. `self_action` is the action_id that re-renders the
    same listing (so `🔄 Refresh` re-fetches data into the same
    anchor); `◀ Back` always returns to the top-level chooser. Order
    is back-first to match the user's left-to-right reading flow."""
    return (
        Action(label="◀ Back", action_id=ACTION_SESSIONS_REFRESH),
        Action(label="🔄 Refresh", action_id=self_action),
        Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
    )


def build_active_detail_card(*, pane_id: str, pane_name: str, ptr: RunPointer | None) -> Card:
    """Active row-detail card — opened by tapping a row in the
    Active sub-pane.

    Body shows pane name + cwd + run id so the user can confirm what
    they're about to bind. Rows: 🔗 Bind | 🔁 Refresh, ◀ Back |
    ✕ Dismiss. The History action moved to the Manage card (`/session`)
    in this redesign — keeps the row-detail surface tight per the
    user's spec.
    """
    cwd_display = "(unknown)" if ptr is None else _short_cwd(ptr.cwd)
    run_short = "(no run)" if ptr is None else ptr.run_id[:8]
    body = "\n".join(
        [
            f"*{pane_name}*",
            "● active session",
            "",
            f"_pane `{pane_id}` · run `{run_short}` · `{cwd_display}`_",
        ]
    )
    rows: tuple[tuple[Action, ...], ...] = (
        (
            Action(label="🔗 Bind", action_id=ACTION_BIND, value={"pane_id": pane_id}),
            Action(
                label="🔁 Refresh",
                action_id=ACTION_ACTIVE_PICK,
                value={"pane_id": pane_id},
            ),
        ),
        (
            Action(label="◀ Back", action_id=ACTION_OPEN_ACTIVE),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title=f"⚡ {pane_name}",
        header_color="green",
    )


def build_dormant_detail_card(*, sid: str, cwd: Path, file_path: Path) -> Card:
    """Dormant row-detail card — opened by tapping a row in the Resume
    sub-pane. Rows: ▶ Resume (full-width primary) / 📦 Archive | 🗑 Delete
    / ◀ Back | ✕ Dismiss. Back returns to the Resume sub-pane (not the
    top-level chooser). Archive is the soft-delete tier — moves the
    JSONL to `~/.claude/archive/`, recoverable via `/sessions → 📦
    Archive → ♻ Restore`."""
    cwd_display = _short_cwd(cwd)
    body = "\n".join(
        [
            f"*{cwd.name or sid[:8]}*",
            "○ dormant session",
            "",
            f"_sid `{sid[:8]}` · `{cwd_display}`_",
        ]
    )
    resume_value = {"sid": sid, "cwd": str(cwd)}
    file_value = {"file_path": str(file_path)}
    rows: tuple[tuple[Action, ...], ...] = (
        (Action(label="▶ Resume", action_id=ACTION_RESUME, value=resume_value),),
        (
            Action(label="📦 Archive", action_id=ACTION_DORMANT_ARCHIVE, value=file_value),
            Action(label="🗑 Delete", action_id=ACTION_DORMANT_DELETE, value=file_value),
        ),
        (
            Action(label="◀ Back", action_id=ACTION_OPEN_RESUME),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title=f"▶ {cwd.name or sid[:8]}",
        header_color="wathet",
    )


def build_archived_detail_card(*, sid: str, cwd: Path, file_path: Path) -> Card:
    """Archived row-detail card — opened by tapping a row in the
    Archive sub-pane. Rows: 📖 View | ♻ Restore / ◀ Back | ✕ Dismiss.
    View sends a fresh History card as a *new* message (keeping the
    detail card in place) so the user can flip between detail + history
    without losing either. Restore moves the JSONL back to
    `~/.claude/projects/` and repaints the chooser. No permanent-delete
    here in v1 — `rm` from a shell if you really want it gone."""
    cwd_display = _short_cwd(cwd)
    body = "\n".join(
        [
            f"*{cwd.name or sid[:8]}*",
            "📦 archived session",
            "",
            f"_sid `{sid[:8]}` · `{cwd_display}`_",
        ]
    )
    file_value = {"file_path": str(file_path)}
    rows: tuple[tuple[Action, ...], ...] = (
        (
            Action(label="📖 View", action_id=ACTION_ARCHIVE_VIEW, value=file_value),
            Action(label="♻ Restore", action_id=ACTION_ARCHIVE_RESTORE, value=file_value),
        ),
        (
            Action(label="◀ Back", action_id=ACTION_OPEN_ARCHIVE),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title=f"📦 {cwd.name or sid[:8]}",
        header_color="grey",
    )


def build_new_detail_card(*, cwd: Path) -> Card:
    """New row-detail card — opened by tapping a directory in the
    New sub-pane. Two-step pattern: Start spawns a fresh `claude`
    pane and binds to it, Back returns to the directory listing.
    Rows: 🚀 Start | 🔁 Refresh, ◀ Back | ✕ Dismiss."""
    cwd_display = _short_cwd(cwd)
    body = "\n".join(
        [
            f"*Start in {cwd.name or cwd_display}?*",
            "",
            f"_`{cwd_display}`_",
            "",
            "Tap *Start* to spawn `claude` and bind this conversation.",
        ]
    )
    rows: tuple[tuple[Action, ...], ...] = (
        (
            Action(label="🚀 Start", action_id=ACTION_NEW_START, value={"cwd": str(cwd)}),
            Action(label="🔁 Refresh", action_id=ACTION_NEW_PICK, value={"cwd": str(cwd)}),
        ),
        (
            Action(label="◀ Back", action_id=ACTION_OPEN_NEW),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title=f"🆕 {cwd.name or '(root)'}",
        header_color="wathet",
    )


def build_manage_card(
    *,
    pane_id: str,
    pane_name: str,
    ptr: RunPointer | None,
    host: Host | None = None,
) -> Card:
    """Manage card body + action rows.

    Body: pane name, status line (with optional `🖥 {host}` badge
    when multi-host is in play — see `SessionsService._badge_host`),
    and a metadata footer (pane id, run id short, cwd).

    Rows are kept tight — six buttons in three rows. The five
    forwarded-command quick-actions (`/clear /compact /cost /memory
    /model`) live in a separate Commands sub-pane reachable via the
    `🛠 Commands` button; this avoids the 10-button-in-5-rows wall
    that prior versions of the card used.
    """
    cwd_display = "(unknown)" if ptr is None else _short_cwd(ptr.cwd)
    run_short = "(no run)" if ptr is None else ptr.run_id[:8]
    status_line = "● active · bound to this topic"
    if host is not None:
        status_line = f"{status_line} · 🖥 {host.display_name}"
    body = "\n".join(
        [
            f"*{pane_name}*",
            status_line,
            "",
            f"_pane `{pane_id}` · run `{run_short}` · `{cwd_display}`_",
        ]
    )
    rows: tuple[tuple[Action, ...], ...] = (
        (
            Action(label="🔓 Unbind", action_id=ACTION_MANAGE_UNBIND),
            Action(label="📋 History", action_id=ACTION_MANAGE_HISTORY),
        ),
        (
            Action(label="🛠 Commands", action_id=ACTION_MANAGE_COMMANDS),
            Action(label="⚙ Prefs", action_id=ACTION_MANAGE_PREFS),
        ),
        (
            Action(label="◀ Back", action_id=ACTION_MANAGE_BACK),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title=f"⚡ {pane_name} (this topic)",
        header_color="green",
    )


def build_commands_card() -> Card:
    """Commands sub-pane — opened from the Manage card's `🛠 Commands`
    button. Holds the forwarded slash-commands paige knows how to
    send to claude (`/clear /compact /cost /memory /model`).

    Each button posts the literal `/cmd` to the bound pane via
    `_on_manage_cmd` (same handler as before — just relocated). Back
    returns to the Manage card via the shared `ACTION_PREFS_BACK`
    handler since the repaint shape is identical.
    """
    body = "Send a forwarded command to claude:"
    cmd_buttons = tuple(
        Action(label=label, action_id=ACTION_MANAGE_CMD, value={"cmd": name})
        for name, label in _MANAGE_CMD_BUTTONS
    )
    # Pair the 5 commands two-per-row; the trailing `🤖 Model` lands
    # alone on its own row so it gets full-width and doesn't hide
    # behind another command's label on mobile.
    cmd_rows = tuple(cmd_buttons[i : i + 2] for i in range(0, len(cmd_buttons) - 1, 2))
    tail_row: tuple[Action, ...] = (cmd_buttons[-1],)
    rows: tuple[tuple[Action, ...], ...] = (
        *cmd_rows,
        tail_row,
        (
            Action(label="◀ Back", action_id=ACTION_PREFS_BACK),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title="🛠 Commands",
        header_color="wathet",
    )


def build_prefs_card(
    person: Person,
    conversation: Conversation,
    verbosity: VerbosityService,
    message_seq: MessageSeqService,
    collapse_pref: CollapsePrefService | None = None,
) -> Card:
    """Preferences sub-panel — verbosity toggles per content kind,
    plus debug + render-shape controls.

    Body explains what BRIEF / FULL means; each toggle button shows
    the current state ("📝 Text: FULL") and tapping flips it. Three
    verbosity toggles in 2+1 layout, then the debug Msg-seq toggle,
    then the long-body Collapse cycle (when wired). Trailing nav
    row routes Back to the Manage card and Dismiss to delete the
    card outright.
    """
    # Body is a single space — Feishu rejects cards with no body
    # element, but the toggle labels carry their own meaning so an
    # explanatory blurb above them was just noise.
    body = " "
    # One toggle per row, full-width — keeps each label unclipped
    # and makes the panel scan top-to-bottom as a checklist.
    toggle_rows = tuple(
        (
            Action(
                label=f"{prefix}: {verbosity.get(person, conversation, kind).value.upper()}",
                action_id=ACTION_PREFS_TOGGLE,
                value={"kind": kind.value},
            ),
        )
        for kind, prefix in _PREFS_KINDS
    )
    seq_state = "ON" if message_seq.is_enabled(person, conversation) else "OFF"
    msg_seq_row = (Action(label=f"🔢 Msg seq: {seq_state}", action_id=ACTION_PREFS_MSG_SEQ),)
    extra_rows: tuple[tuple[Action, ...], ...] = ()
    if collapse_pref is not None:
        threshold = collapse_pref.threshold(person, conversation)
        label = "OFF" if threshold == 0 else f"{threshold} lines"
        extra_rows = ((Action(label=f"📄 Collapse: {label}", action_id=ACTION_PREFS_COLLAPSE),),)
    rows: tuple[tuple[Action, ...], ...] = (
        *toggle_rows,
        msg_seq_row,
        *extra_rows,
        (
            Action(label="◀ Back", action_id=ACTION_PREFS_BACK),
            Action(label="✕ Dismiss", action_id=ACTION_MANAGE_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title="⚙ Prefs",
        header_color="wathet",
    )


def _short_cwd(cwd: Path) -> str:
    home = str(Path.home())
    s = str(cwd)
    if s.startswith(home):
        return "~" + s[len(home) :]
    return s
