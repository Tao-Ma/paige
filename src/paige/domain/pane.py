"""Pane + Binding — the multiplexer surface and how it links to a topic."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .conversation import Conversation
from .person import Person


@dataclass(frozen=True)
class Pane:
    """A multiplexer pane — a unit of independent text I/O.

    Maps to a tmux *window* in our deployment (libtmux uses "window"
    for what we call "pane" — a top-level container with its own
    cwd + process). The disambiguation matters because tmux also
    has "panes" inside windows (split-screen); we don't model
    those.

    `pane_id` is the multiplexer-unique id (`@0`, `@12`).
    `multiplexer_session` is the parent session name when the
    backend supports multiple (tmux: optional; default "" means
    "any").
    """

    pane_id: str
    pane_name: str
    cwd: Path
    multiplexer_session: str = ""


@dataclass(frozen=True)
class Binding:
    """A topic-to-pane binding.

    Each (`person`, `conversation`) → (`host_id`, `pane_id`) tuple
    says "messages in this topic route to that pane on that host."
    Many topics may bind to one pane (sharing) or one topic to one
    pane (most common).

    `host_id` defaults to `"local"` so existing single-host code
    paths and persisted state stay valid; a future SSH slice will
    populate it from `~/.paige/hosts.toml` entries. See
    `domain/host.py` and `doc/multi-host.md`.
    """

    person: Person
    conversation: Conversation
    pane_id: str
    host_id: str = "local"
