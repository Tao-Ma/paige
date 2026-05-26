"""ServerService — `/server` admin-gated system overview.

Top-level `/server` is a **four-category chooser** so the card stays
tight even when there are many panes:

  Row 1: 🖥 Hosts (N) | 🪟 Panes (N)
  Row 2: 💾 Storage   | ⚙ Process
  Row 3: 🔄 Refresh   | ✕ Dismiss

Each category drills into a sub-pane:

  - **🖥 Hosts** — boxes paige can operate on. Today only the
    synthetic `local` host; future SSH adapter slice (see
    `doc/multi-host.md`) populates remotes from `~/.paige/hosts.toml`.
    Tap a host → host-detail card with that host's process info.
  - **🪟 Panes** — list of multiplexer panes (one row per pane,
    sorted by name). Tap a pane → pane-detail card with ⚠ Kill,
    matching `/sessions`' row-detail pattern.
  - **💾 Storage** — paige dir size, projects dir size, container
    memory. Read-only.
  - **⚙ Process** — paige's own pid / uptime / RSS. Read-only.

Each sub-pane has a trailing `◀ Back | 🔄 Refresh | ✕ Dismiss` nav
row where Back returns to the chooser; row-detail cards have a
primary action (only `⚠ Kill` today) plus Back/Refresh/Dismiss.

Why admin-gated: the kill action affects shared infrastructure —
shouldn't be available to a regular allowed user with a chat
window. `AdminList` defaults to "every allowed user is admin" so
solo deploys aren't burdened by yet another env var.

What's NOT here (deliberately):
- Per-process actions beyond Kill (no signal-sending, no log peek).
- Container start/stop.
- Drill-downs into a pane's transcript (that's `/history`).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.card import Action, ActionEvent, Card
from ..domain.host import LOCAL, LOCAL_HOST_ID, Host
from ..domain.inbound import Inbound
from ..domain.outbound import CardContent, Outbound, TextContent
from ..infrastructure.format import format_bytes, format_duration
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from .access import AdminList
from .hosts import HostsService
from .message_seq import MessageSeqService
from .outbox import Outbox
from .proc_scan import (
    dir_size_bytes,
    read_cgroup_memory,
    read_comm_for_pid,
    read_rss_bytes_for_pid,
)
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

ACTION_REFRESH = "sv:ref"
ACTION_KILL = "sv:kill"
# Top-level chooser → sub-pane drilldowns. ACTION_REFRESH doubles as
# the Back button on every sub-pane (re-renders the chooser into the
# same anchor); each sub-pane's own 🔄 Refresh re-fires its own
# `OPEN_*` action_id to re-fetch the relevant data.
ACTION_OPEN_HOSTS = "sv:oh"
ACTION_OPEN_PANES = "sv:op"
ACTION_OPEN_STORAGE = "sv:os"
ACTION_OPEN_PROCESS = "sv:ow"
# Row taps in the Hosts / Panes sub-panes → row-detail cards.
ACTION_HOST_PICK = "sv:hp"
ACTION_PANE_PICK = "sv:pp"
ACTION_DISMISS = "sv:di"

ADMIN_ONLY_HINT = "❌ Admin only. Set `PAIGE_ADMIN_USERS` to allow this user."

# Captured at module import — close enough to bot-start for a
# human-readable uptime; not worth /proc/self/stat parsing.
_PROCESS_BOOT_AT: float = time.time()


@dataclass(frozen=True)
class _PaneRow:
    pane_id: str
    pane_name: str
    multiplexer_session: str
    foreground_pid: int | None
    foreground_comm: str | None
    foreground_rss: int | None


@dataclass(frozen=True)
class ServerInfo:
    """Snapshot of everything the /server card renders."""

    bot_pid: int
    bot_uptime_sec: float
    bot_rss_bytes: int | None
    multiplexer_session_name: str
    pane_count: int
    tracked_pane_count: int
    container_mem_used: int | None
    container_mem_limit: int | None
    projects_dir_bytes: int
    paige_dir_bytes: int
    pane_rows: tuple[_PaneRow, ...] = field(default_factory=tuple)


# Async hook so tests can swap the dir-walk with an instant fake.
DirSizeFn = Callable[[Path], Awaitable[int]]


async def _default_dir_size(path: Path) -> int:
    import asyncio

    return await asyncio.to_thread(dir_size_bytes, path)


class ServerService:
    """`/server` command + Refresh / Kill action handlers."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        channel: Channel,
        admin_list: AdminList,
        multiplexer_session_name: str = "paige",
        projects_root: Path | None = None,
        paige_dir: Path | None = None,
        dir_size: DirSizeFn = _default_dir_size,
        hosts: HostsService | None = None,
        message_seq: MessageSeqService | None = None,
    ) -> None:
        self._registry = registry
        self._multiplexer = multiplexer
        self._outbox = outbox
        self._channel = channel
        self._admins = admin_list
        # Drill-down repaints go direct to the channel (inline-refresh),
        # bypassing the Outbox — stamp the seq footer here to match the
        # click-edit convention.
        self._message_seq = message_seq
        self._mx_session = multiplexer_session_name
        self._projects_root = projects_root or (Path.home() / ".claude" / "projects")
        self._paige_dir = paige_dir or (Path.home() / ".paige")
        self._dir_size = dir_size
        # Optional: when None, the Hosts sub-pane synthesises a
        # single `local` host on the fly. Tests don't need a
        # HostsService unless they care about the listing's
        # composition.
        self._hosts = hosts

    def install(self, channel: Channel) -> None:
        # /server is admin-gated, but we register without a guard
        # wrapper so the unauthorized user gets a textual hint
        # instead of silent drop. Same for taps.
        channel.on_command("server", self._server)
        channel.on_action(self._handle_action)

    # ── /server ──────────────────────────────────────────────────

    async def _server(self, inbound: Inbound, _arg: str) -> None:
        if not self._admins.is_admin(inbound.sender.user_id):
            self._reply_text(inbound, ADMIN_ONLY_HINT)
            return
        info = await self._collect()
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=CardContent(card=_render_chooser(info, hosts=self._hosts_list())),
            ),
        )

    # ── action dispatch ──────────────────────────────────────────

    async def _handle_action(self, event: ActionEvent) -> None:
        if event.action_id == ACTION_REFRESH:
            await self._on_refresh(event)
        elif event.action_id == ACTION_KILL:
            await self._on_kill(event)
        elif event.action_id == ACTION_OPEN_HOSTS:
            await self._on_open_hosts(event)
        elif event.action_id == ACTION_OPEN_PANES:
            await self._on_open_panes(event)
        elif event.action_id == ACTION_OPEN_STORAGE:
            await self._on_open_storage(event)
        elif event.action_id == ACTION_OPEN_PROCESS:
            await self._on_open_process(event)
        elif event.action_id == ACTION_HOST_PICK:
            await self._on_host_pick(event)
        elif event.action_id == ACTION_PANE_PICK:
            await self._on_pane_pick(event)
        elif event.action_id == ACTION_DISMISS:
            await self._on_dismiss(event)
        # Other action_ids belong to other services — silent skip.

    async def _gate(self, event: ActionEvent) -> bool:
        """Common admin-gate for /server actions. Returns True when
        the event may proceed, False after sending the unauthorised
        ack hint."""
        if self._admins.is_admin(event.sender.user_id):
            return True
        await self._channel.ack(event, ADMIN_ONLY_HINT)
        return False

    async def _edit_card(self, event: ActionEvent, card: Card, ack: str) -> None:
        await self._channel.ack(event, ack)
        # Direct channel.edit (not the Outbox) so the repaint rides the
        # click-response inline-refresh slot. An out-of-band PATCH to the
        # just-clicked card repaints unreliably on the tapper — Feishu
        # often renders it as a *new* card instead of updating in place.
        # Stamp the seq footer here since the Outbox is bypassed.
        outbound = Outbound(conversation=event.conversation, content=CardContent(card=card))
        if self._message_seq is not None:
            outbound, _ = self._message_seq.stamp_edit(
                event.sender, event.conversation, event.card_anchor, outbound
            )
        await self._channel.edit(event.card_anchor, outbound)

    async def _on_refresh(self, event: ActionEvent) -> None:
        """Refresh on the chooser AND `◀ Back` from any sub-pane —
        same shape: re-fetch and render the chooser into the same
        anchor."""
        if not await self._gate(event):
            return
        info = await self._collect()
        card = _render_chooser(info, hosts=self._hosts_list())
        await self._edit_card(event, card, "🔄")

    async def _on_open_hosts(self, event: ActionEvent) -> None:
        if not await self._gate(event):
            return
        await self._edit_card(event, _render_hosts_listing(self._hosts_list()), "🖥 Hosts")

    async def _on_open_panes(self, event: ActionEvent) -> None:
        if not await self._gate(event):
            return
        info = await self._collect()
        await self._edit_card(event, _render_panes_listing(info), "🪟 Panes")

    async def _on_open_storage(self, event: ActionEvent) -> None:
        if not await self._gate(event):
            return
        info = await self._collect()
        await self._edit_card(event, _render_storage(info), "💾 Storage")

    async def _on_open_process(self, event: ActionEvent) -> None:
        if not await self._gate(event):
            return
        info = await self._collect()
        await self._edit_card(event, _render_process(info), "⚙ Process")

    async def _on_host_pick(self, event: ActionEvent) -> None:
        if not await self._gate(event):
            return
        host_id = event.value.get("host_id", "")
        if not host_id:
            await self._channel.ack(event, "Invalid action")
            return
        host = next((h for h in self._hosts_list() if h.host_id == host_id), None)
        if host is None:
            # Host vanished from config between list and tap — fall
            # back to the listing so the user isn't stranded.
            await self._channel.ack(event, "Host gone — refreshing")
            await self._edit_card(event, _render_hosts_listing(self._hosts_list()), "🖥 Hosts")
            return
        info = await self._collect() if host_id == LOCAL_HOST_ID else None
        await self._edit_card(event, _render_host_detail(host, info), host.display_name)

    async def _on_pane_pick(self, event: ActionEvent) -> None:
        if not await self._gate(event):
            return
        pane_id = event.value.get("pane_id", "")
        if not pane_id:
            await self._channel.ack(event, "Invalid action")
            return
        info = await self._collect()
        row = next((r for r in info.pane_rows if r.pane_id == pane_id), None)
        if row is None:
            await self._channel.ack(event, "Pane gone — refreshing")
            await self._edit_card(event, _render_panes_listing(info), "🪟 Panes")
            return
        await self._edit_card(event, _render_pane_detail(row), row.pane_name)

    async def _on_dismiss(self, event: ActionEvent) -> None:
        await self._channel.ack(event, "✕")
        self._outbox.enqueue_delete(event.sender, event.card_anchor)

    async def _on_kill(self, event: ActionEvent) -> None:
        if not await self._gate(event):
            return
        pane_id = event.value.get("p", "")
        if not pane_id:
            await self._channel.ack(event, "Invalid action")
            return
        ok = await self._multiplexer.kill_pane(pane_id)
        # Cascade-clean the registry (clears bindings + run pointer)
        # whether or not the kill itself reported success — the pane
        # is either gone OR was already gone before the click.
        await self._registry.remove_pane(pane_id)
        info = await self._collect()
        # Repaint to the Panes listing rather than the chooser — the
        # user was operating on panes, the listing is the natural
        # next view (one fewer entry, easier to confirm what's left).
        await self._edit_card(
            event, _render_panes_listing(info), "Killed" if ok else "Already gone"
        )

    def _hosts_list(self) -> list[Host]:
        """Fall back to a synthetic local-only listing when no
        HostsService is wired (tests / minimal deploys). Tests that
        don't care about the Hosts sub-pane composition can leave
        the parameter unset."""
        if self._hosts is not None:
            return self._hosts.list()
        return [LOCAL]

    # ── collect ──────────────────────────────────────────────────

    async def _collect(self) -> ServerInfo:
        import asyncio

        bot_pid = os.getpid()
        bot_uptime = max(0.0, time.time() - _PROCESS_BOOT_AT)
        bot_rss = read_rss_bytes_for_pid(bot_pid)
        cg_used, cg_limit = read_cgroup_memory()

        projects_bytes, paige_bytes = await asyncio.gather(
            self._dir_size(self._projects_root),
            self._dir_size(self._paige_dir),
        )

        panes = await self._multiplexer.list_panes()
        tracked_pane_ids = {p for p in self._registry.list_panes()}
        tracked_count = sum(1 for p in panes if p.pane_id in tracked_pane_ids)

        rows: list[_PaneRow] = []
        for pane in panes:
            fg_pid = await self._multiplexer.get_foreground_pid(pane.pane_id)
            fg_comm = read_comm_for_pid(fg_pid) if fg_pid is not None else None
            fg_rss = read_rss_bytes_for_pid(fg_pid) if fg_pid is not None else None
            rows.append(
                _PaneRow(
                    pane_id=pane.pane_id,
                    pane_name=pane.pane_name,
                    multiplexer_session=pane.multiplexer_session,
                    foreground_pid=fg_pid,
                    foreground_comm=fg_comm,
                    foreground_rss=fg_rss,
                )
            )
        rows.sort(key=lambda r: (r.multiplexer_session, r.pane_name))

        return ServerInfo(
            bot_pid=bot_pid,
            bot_uptime_sec=bot_uptime,
            bot_rss_bytes=bot_rss,
            multiplexer_session_name=self._mx_session,
            pane_count=len(panes),
            tracked_pane_count=tracked_count,
            container_mem_used=cg_used,
            container_mem_limit=cg_limit,
            projects_dir_bytes=projects_bytes,
            paige_dir_bytes=paige_bytes,
            pane_rows=tuple(rows),
        )

    def _reply_text(self, inbound: Inbound, text: str) -> None:
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=TextContent(text),
            ),
        )


def _render_chooser(info: ServerInfo, *, hosts: list[Host]) -> Card:
    """Top-level /server chooser — 6 buttons in 3 rows. Body shows
    a one-line summary of the most-glanced metrics so the user can
    spot a problem before drilling in."""
    body = " · ".join(
        [
            f"*paige* uptime {format_duration(info.bot_uptime_sec)}",
            f"{info.pane_count} panes",
            format_bytes(info.bot_rss_bytes),
        ]
    )
    rows: tuple[tuple[Action, ...], ...] = (
        (
            Action(label=f"🖥 Hosts ({len(hosts)})", action_id=ACTION_OPEN_HOSTS),
            Action(label=f"🪟 Panes ({info.pane_count})", action_id=ACTION_OPEN_PANES),
        ),
        (
            Action(label="💾 Storage", action_id=ACTION_OPEN_STORAGE),
            Action(label="⚙ Process", action_id=ACTION_OPEN_PROCESS),
        ),
        (
            Action(label="🔄 Refresh", action_id=ACTION_REFRESH),
            Action(label="✕ Dismiss", action_id=ACTION_DISMISS),
        ),
    )
    return Card(text=body, rows=rows, header_title="🖥 Server", header_color="red")


def _server_subpane_nav(*, self_action: str) -> tuple[Action, ...]:
    """Trailing nav row reused by every /server sub-pane. Same
    Back/Refresh/Dismiss order as `/sessions` sub-panes:
    - `◀ Back` always fires ACTION_REFRESH (re-renders the chooser).
    - `🔄 Refresh` re-fires the sub-pane's own open action so
      tapping it re-fetches the data and stays on the same view.
    - `✕ Dismiss` deletes the card."""
    return (
        Action(label="◀ Back", action_id=ACTION_REFRESH),
        Action(label="🔄 Refresh", action_id=self_action),
        Action(label="✕ Dismiss", action_id=ACTION_DISMISS),
    )


def _render_hosts_listing(hosts: list[Host]) -> Card:
    """🖥 Hosts sub-pane — list of configured hosts. Today only the
    synthetic `local` host; future SSH adapter slice will populate
    remotes from `~/.paige/hosts.toml`."""
    if not hosts:
        body = "*No hosts configured.*"
        return Card(
            text=body,
            rows=(_server_subpane_nav(self_action=ACTION_OPEN_HOSTS),),
            header_title="🖥 Hosts",
            header_color="red",
        )
    body = f"*{len(hosts)} host(s) configured*"
    pick_rows: list[tuple[Action, ...]] = []
    for host in hosts:
        # ● up for local (paige itself); future SSH hosts will carry
        # real status once probe wiring lands.
        marker = "●" if host.host_id == LOCAL_HOST_ID else "○"
        pick_rows.append(
            (
                Action(
                    label=f"{marker} {host.display_name}",
                    action_id=ACTION_HOST_PICK,
                    value={"host_id": host.host_id},
                ),
            )
        )
    pick_rows.append(_server_subpane_nav(self_action=ACTION_OPEN_HOSTS))
    return Card(
        text=body,
        rows=tuple(pick_rows),
        header_title="🖥 Hosts",
        header_color="red",
    )


def _render_host_detail(host: Host, info: ServerInfo | None) -> Card:
    """Per-host detail card. For `local`, show paige's own process
    info (pid / uptime / rss) — they're the local-host process. For
    future SSH hosts, the detail card will surface the remote-host
    probe results instead."""
    lines: list[str] = [f"*{host.display_name}*", f"_id `{host.host_id}`_"]
    if host.host_id == LOCAL_HOST_ID and info is not None:
        lines.extend(
            [
                "",
                f"pid     {info.bot_pid}",
                f"uptime  {format_duration(info.bot_uptime_sec)}",
                f"rss     {format_bytes(info.bot_rss_bytes)}",
            ]
        )
    else:
        # SSH adapter slice will fill these out (probe status, ping,
        # tmux pane count on the remote host, etc.).
        lines.extend(
            [
                "",
                "_(remote host detail not yet implemented — see doc/multi-host.md)_",
            ]
        )
    rows: tuple[tuple[Action, ...], ...] = (
        (
            Action(label="◀ Back", action_id=ACTION_OPEN_HOSTS),
            Action(label="🔄 Refresh", action_id=ACTION_OPEN_HOSTS),
            Action(label="✕ Dismiss", action_id=ACTION_DISMISS),
        ),
    )
    return Card(
        text="\n".join(lines),
        rows=rows,
        header_title=f"🖥 {host.display_name}",
        header_color="red",
    )


def _render_panes_listing(info: ServerInfo) -> Card:
    """🪟 Panes sub-pane — one row per multiplexer pane, ordered by
    name. Tap a row → pane-detail card with ⚠ Kill (consistent
    with the `/sessions` row-detail pattern)."""
    if not info.pane_rows:
        body = "*No panes.*"
        return Card(
            text=body,
            rows=(_server_subpane_nav(self_action=ACTION_OPEN_PANES),),
            header_title="🪟 Panes",
            header_color="red",
        )
    resolved = sum(1 for r in info.pane_rows if r.foreground_pid is not None)
    total_rss = sum(r.foreground_rss or 0 for r in info.pane_rows)
    body = f"*{len(info.pane_rows)} pane(s)* · {resolved} resolved · {format_bytes(total_rss)} rss"
    pick_rows: list[tuple[Action, ...]] = []
    for r in info.pane_rows:
        fg = f"{r.foreground_comm or '?'}/{r.foreground_pid}" if r.foreground_pid else "shell"
        rss_str = format_bytes(r.foreground_rss) if r.foreground_rss else "—"
        pick_rows.append(
            (
                Action(
                    label=f"🪟 {r.pane_name} — {fg} · {rss_str}",
                    action_id=ACTION_PANE_PICK,
                    value={"pane_id": r.pane_id},
                ),
            )
        )
    pick_rows.append(_server_subpane_nav(self_action=ACTION_OPEN_PANES))
    return Card(
        text=body,
        rows=tuple(pick_rows),
        header_title="🪟 Panes",
        header_color="red",
    )


def _render_pane_detail(row: _PaneRow) -> Card:
    """Per-pane detail card — body has full process info, primary
    button is ⚠ Kill. Back returns to the Panes listing (not the
    chooser) so the user can quickly act on another pane."""
    fg_line = (
        f"_foreground `{row.foreground_comm or '?'}/{row.foreground_pid}`_"
        if row.foreground_pid is not None
        else "_(shell only — no claude child)_"
    )
    rss_str = format_bytes(row.foreground_rss) if row.foreground_rss else "—"
    session_line = f"_session `{row.multiplexer_session}`_" if row.multiplexer_session else ""
    body = "\n".join(
        line
        for line in [
            f"*{row.pane_name}*",
            f"_pane `{row.pane_id}`_",
            session_line,
            fg_line,
            f"_rss {rss_str}_",
        ]
        if line
    )
    rows: tuple[tuple[Action, ...], ...] = (
        (
            Action(label="⚠ Kill", action_id=ACTION_KILL, value={"p": row.pane_id}),
            Action(
                label="🔁 Refresh",
                action_id=ACTION_PANE_PICK,
                value={"pane_id": row.pane_id},
            ),
        ),
        (
            Action(label="◀ Back", action_id=ACTION_OPEN_PANES),
            Action(label="✕ Dismiss", action_id=ACTION_DISMISS),
        ),
    )
    return Card(
        text=body,
        rows=rows,
        header_title=f"🪟 {row.pane_name}",
        header_color="red",
    )


def _render_storage(info: ServerInfo) -> Card:
    """💾 Storage sub-pane — paige dir, projects dir, container
    memory. Read-only (no kill/clean buttons here; deletion lives
    in the per-dormant flow on `/sessions`)."""
    if info.container_mem_limit is None:
        mem_str = f"{format_bytes(info.container_mem_used)} / unbounded"
    else:
        mem_str = (
            f"{format_bytes(info.container_mem_used)} / {format_bytes(info.container_mem_limit)}"
        )
    body = "\n".join(
        [
            f"*paige*       {format_bytes(info.paige_dir_bytes)}",
            f"*projects*    {format_bytes(info.projects_dir_bytes)}",
            f"*container*   {mem_str}",
        ]
    )
    return Card(
        text=body,
        rows=(_server_subpane_nav(self_action=ACTION_OPEN_STORAGE),),
        header_title="💾 Storage",
        header_color="red",
    )


def _render_process(info: ServerInfo) -> Card:
    """⚙ Process sub-pane — paige's own pid / uptime / RSS. Read-only."""
    body = "\n".join(
        [
            f"*pid*       {info.bot_pid}",
            f"*uptime*    {format_duration(info.bot_uptime_sec)}",
            f"*rss*       {format_bytes(info.bot_rss_bytes)}",
            f"*panes*     {info.pane_count} total · {info.tracked_pane_count} tracked",
        ]
    )
    return Card(
        text=body,
        rows=(_server_subpane_nav(self_action=ACTION_OPEN_PROCESS),),
        header_title="⚙ Process",
        header_color="red",
    )


__all__ = [
    "ACTION_DISMISS",
    "ACTION_HOST_PICK",
    "ACTION_KILL",
    "ACTION_OPEN_HOSTS",
    "ACTION_OPEN_PANES",
    "ACTION_OPEN_PROCESS",
    "ACTION_OPEN_STORAGE",
    "ACTION_PANE_PICK",
    "ACTION_REFRESH",
    "ADMIN_ONLY_HINT",
    "ServerInfo",
    "ServerService",
]
