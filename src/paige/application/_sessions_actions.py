"""Action-id string constants for the /sessions and /session surfaces.

Split out of `sessions.py` so the public-API constants live in a tight,
scrollable module; `sessions.py` re-exports them for back-compat so
existing `from paige.application.sessions import ACTION_*` imports
keep working.
"""

from __future__ import annotations

ACTION_BIND = "ses:bind"
ACTION_RESUME = "ses:rs"
# Top-level /sessions chooser is a category picker: Active / Resume
# / New. Each opens a sub-pane that lists its members ordered by
# cwd; tapping a row opens a per-row detail card with the primary
# action button (Bind / Resume / Start). The chooser doesn't carry
# session rows itself — keeps the top-level surface tight even
# when the user has many sessions.
ACTION_OPEN_ACTIVE = "ses:oa"
ACTION_OPEN_RESUME = "ses:or"
ACTION_OPEN_NEW = "ses:on"
# Row taps in the Active / Resume sub-panes open the per-row detail
# card. ACTION_BIND / ACTION_RESUME stay live as the detail card's
# primary buttons so handlers don't move.
ACTION_ACTIVE_PICK = "ses:ap"
ACTION_DORMANT_PICK = "ses:dp"
# Page-tap in the Resume sub-pane (◀ Prev / N/M / Next ▶). The full
# dormant list is cached per (user, conversation) by ChooserHandlers
# on the first listing render; page taps slice from the cache so
# repeated taps don't re-walk the JSONL tree. Refresh re-fetches.
ACTION_DORMANT_PAGE = "ses:dpg"
# New sub-pane row tap → confirmation card; ACTION_NEW_START commits
# the spawn from there. Two-step prevents accidental fresh-pane
# creation on a mistap.
ACTION_NEW_PICK = "ses:np"
ACTION_NEW_START = "ses:ns"
# Sub-pane secondary action — unlinks the JSONL transcript.
ACTION_DORMANT_DELETE = "ses:dd"
# Resume-detail tertiary action — move the JSONL to ~/.claude/archive
# preserving the encoded-cwd subdir. Soft-delete tier: the file stays
# recoverable via `♻ Restore` from the Archive sub-pane.
ACTION_DORMANT_ARCHIVE = "ses:dar"
# Top-level chooser opener for the Archive category — peer of Active /
# Resume / New. Surfaced regardless of archive count (no "(K)" gating)
# so users can find archives even when they're empty.
ACTION_OPEN_ARCHIVE = "ses:oar"
# Archive sub-pane row tap → archived-row detail card. Mirrors
# ACTION_DORMANT_PICK / ACTION_ACTIVE_PICK.
ACTION_ARCHIVE_PICK = "ses:arp"
# Page-tap in the Archive sub-pane. Same caching strategy as
# ACTION_DORMANT_PAGE — first listing walks the archive tree, page
# taps slice the cached list.
ACTION_ARCHIVE_PAGE = "ses:apg"
# Archived-row detail actions: 📖 View (build a fresh History card
# from the archived JSONL and send it as a new card) and ♻ Restore
# (move the JSONL back to ~/.claude/projects).
ACTION_ARCHIVE_VIEW = "ses:av"
ACTION_ARCHIVE_RESTORE = "ses:ar"
# Re-renders the /sessions chooser into the same anchor. Used as
# the Back button on the Active / Resume / New sub-panes (which all
# return to the chooser).
ACTION_SESSIONS_REFRESH = "ses:rf"
# Multi-host overview entry points. Only surfaced when ≥2 hosts are
# configured (HostsService). Tapping a host opens the existing
# chooser; the overview is reached via /sessions top-level. Single-
# host installs never see the overview — zero regression.
ACTION_OPEN_HOST = "ses:oh"  # open chooser for a host_id (value: {"host_id": "..."})
ACTION_OPEN_OVERVIEW = "ses:ov"  # re-render the host overview (Refresh button on the overview card)
ACTION_MANAGE_UNBIND = "ses:mng:ub"
ACTION_MANAGE_HISTORY = "ses:mng:hi"
ACTION_MANAGE_BACK = "ses:mng:bk"
ACTION_MANAGE_DISMISS = "ses:mng:di"
# Quick-action buttons for the slash commands paige forwards to
# Claude Code (`/clear`, `/compact`, …). Click sends the command
# verbatim to the pane via send_keys, mirroring what typing the
# command does — saves the user from typing on mobile and keeps
# the Manage card as a one-stop session control surface.
ACTION_MANAGE_CMD = "ses:mng:cmd"
# 🛠 Commands sub-pane — collects the forwarded slash-commands
# (`/clear /compact /cost /memory /model`) behind a single button
# on the Manage card. Without this the Manage card carried 10
# buttons in 5 rows; collapsing the cmds keeps the top-level surface
# at 6 buttons in 3 rows. Shares the Manage card's anchor like
# Prefs does — Back returns to Manage in place.
ACTION_MANAGE_COMMANDS = "ses:mng:cm"
# Preferences sub-panel — opens from the Manage card and exposes
# per-(person, conversation) verbosity toggles. Shares the Manage
# card's anchor (single in-place repaint) so the user never sees a
# new card stack for a sub-flow.
ACTION_MANAGE_PREFS = "ses:mng:pf"
ACTION_PREFS_TOGGLE = "ses:pf:tg"
ACTION_PREFS_BACK = "ses:pf:bk"
# Toggle for the message-seq debug stamping. Lives in the Prefs
# panel alongside the verbosity toggles. Routed separately from
# `ACTION_PREFS_TOGGLE` because it doesn't carry a `kind` value.
ACTION_PREFS_MSG_SEQ = "ses:pf:sq"
# Cycle through collapse-threshold values: 25 → 50 → 100 → 0 (off).
# Keyed per-(person, conversation), same shape as message_seq.
ACTION_PREFS_COLLAPSE = "ses:pf:cl"


__all__ = [
    "ACTION_ACTIVE_PICK",
    "ACTION_ARCHIVE_PAGE",
    "ACTION_ARCHIVE_PICK",
    "ACTION_ARCHIVE_RESTORE",
    "ACTION_ARCHIVE_VIEW",
    "ACTION_BIND",
    "ACTION_DORMANT_ARCHIVE",
    "ACTION_DORMANT_DELETE",
    "ACTION_DORMANT_PAGE",
    "ACTION_DORMANT_PICK",
    "ACTION_MANAGE_BACK",
    "ACTION_MANAGE_CMD",
    "ACTION_MANAGE_COMMANDS",
    "ACTION_MANAGE_DISMISS",
    "ACTION_MANAGE_HISTORY",
    "ACTION_MANAGE_PREFS",
    "ACTION_MANAGE_UNBIND",
    "ACTION_NEW_PICK",
    "ACTION_NEW_START",
    "ACTION_OPEN_ACTIVE",
    "ACTION_OPEN_ARCHIVE",
    "ACTION_OPEN_HOST",
    "ACTION_OPEN_NEW",
    "ACTION_OPEN_OVERVIEW",
    "ACTION_OPEN_RESUME",
    "ACTION_PREFS_BACK",
    "ACTION_PREFS_COLLAPSE",
    "ACTION_PREFS_MSG_SEQ",
    "ACTION_PREFS_TOGGLE",
    "ACTION_RESUME",
    "ACTION_SESSIONS_REFRESH",
]
