"""App — the assembled service graph + start/stop lifecycle.

`App` is a frozen container of everything wired together. Tests
construct it manually with fakes; production goes through
`build_app(config)` which constructs concrete adapters.

Lifecycle:

    start()  →  channel.start (long polling),
                watcher.start (JSONL polling),
                status_service.start (pane scrape).
                Order: outbound surface up first so any inbound
                processed by handlers can already produce sends.

    stop()   →  status_service.stop  (no new spinner edits),
                watcher.stop         (no new transcript events),
                channel.stop         (no new inbound),
                outbox.stop          (drains pending sends).
                Order: shut INBOUND sources down first, then drain
                whatever's still in flight on the outbound side.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..application.access import AdminList, AllowList
from ..application.ask_user import AskUserService
from ..application.collapse_pref import CollapsePrefService
from ..application.commands import CommandService
from ..application.directories import DirectoryService
from ..application.dispatcher import Dispatcher
from ..application.echo_dedup import EchoDedup
from ..application.end_turn_panel import EndTurnPanelService
from ..application.history import HistoryService
from ..application.hosts import HostsService, load_hosts_toml
from ..application.interactive_ui import InteractiveUIService
from ..application.live_pane import LivePaneService
from ..application.message_seq import MessageSeqService
from ..application.multiplexer_router import MultiplexerRouter
from ..application.outbox import Outbox
from ..application.quick_reply_prefs import QuickReplyPrefs
from ..application.readiness import ReadinessService
from ..application.run_discovery import RunDiscovery
from ..application.run_registry import RunRegistry
from ..application.screenshot import ScreenshotService
from ..application.server import ServerService
from ..application.sessions import SessionsService
from ..application.status_carrier import StatusCarrierService
from ..application.status_service import StatusService
from ..application.usage import UsageService
from ..application.verbosity import VerbosityService
from ..application.voice import VoiceService
from ..application.watcher_router import WatcherRouter
from ..domain.host import LOCAL_HOST_ID
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from ..ports.storage import Storage
from ..ports.transcriber import Transcriber
from ..ports.watcher import Watcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class App:
    """The composed service graph."""

    channel: Channel
    multiplexer: Multiplexer
    watcher: Watcher
    storage: Storage
    registry: RunRegistry
    outbox: Outbox
    echo_dedup: EchoDedup
    verbosity: VerbosityService
    dispatcher: Dispatcher
    status_service: StatusService
    command_service: CommandService
    sessions_service: SessionsService
    directory_service: DirectoryService
    screenshot_service: ScreenshotService
    live_pane_service: LivePaneService
    history_service: HistoryService
    usage_service: UsageService
    server_service: ServerService
    voice_service: VoiceService | None
    interactive_ui_service: InteractiveUIService
    run_discovery: RunDiscovery
    readiness: ReadinessService
    quick_reply: QuickReplyPrefs
    end_turn_panel: EndTurnPanelService
    status_carrier: StatusCarrierService
    hosts: HostsService
    allow_list: AllowList
    admin_list: AdminList

    async def start(self) -> None:
        logger.info("App starting")
        await self.channel.start()
        await self.watcher.start()
        await self.run_discovery.start()
        await self.status_service.start()
        await self.interactive_ui_service.start()
        logger.info("App started")

    async def stop(self) -> None:
        logger.info("App stopping")
        # Inbound sources first — no new events.
        await self.interactive_ui_service.stop()
        await self.status_service.stop()
        await self.run_discovery.stop()
        await self.watcher.stop()
        await self.channel.stop()
        # Cancel any /livepane poll loops still running.
        await self.live_pane_service.stop()
        # Drain pending outbound work.
        await self.outbox.stop()
        logger.info("App stopped")


def assemble(
    *,
    channel: Channel,
    multiplexer: Multiplexer,
    watcher: Watcher,
    storage: Storage,
    registry: RunRegistry,
    allow_list: AllowList | None = None,
    admin_list: AdminList | None = None,
    transcriber: Transcriber | None = None,
    projects_root: Path | None = None,
    paige_dir: Path | None = None,
    multiplexer_session_name: str = "paige",
    status_interval: float = 1.0,
    discovery_interval: float = 10.0,
) -> App:
    """Wire up all application services on top of pre-constructed
    adapters + registry. Public so tests can assemble with fakes
    without going through `build_app(config)`.

    The caller is expected to have called `await registry.load()`
    before passing it here — App.start() does NOT redo that.

    `allow_list=None` is treated as an open allow-list (everyone
    passes), matching the default-open Config behavior. Production
    callers should pass an explicit one.

    `admin_list=None` defaults to "every allowed user is admin"
    (`AdminList(allowed=allow_list._users)` semantics). Tests that
    care about admin gating pass an explicit one.
    """
    if allow_list is None:
        allow_list = AllowList()
    if admin_list is None:
        admin_list = AdminList()
    if projects_root is None:
        projects_root = Path.home() / "projects"

    echo_dedup = EchoDedup()
    verbosity = VerbosityService()
    message_seq = MessageSeqService()
    collapse_pref = CollapsePrefService()
    # Hosts registry — LOCAL is always synthesised; remote entries
    # come from `<paige_dir>/hosts.toml` when present. The actual SSH
    # adapter slice (see doc/multi-host.md Steps 9-10) hasn't shipped,
    # so loaded remote entries surface as "disconnected" placeholders
    # in /sessions and /server until the multiplexer router learns
    # how to dispatch to them.
    extra_hosts = load_hosts_toml(paige_dir / "hosts.toml") if paige_dir else []
    hosts = HostsService(extra_hosts)
    # Wrap the concrete multiplexer (libtmux) in a router keyed by
    # host_id. Today there's only one entry — `local` → the libtmux
    # adapter — but every service that takes a `Multiplexer` now
    # receives the router as a drop-in. Adding SSH later means
    # registering another entry in this dict; no service code has
    # to know.
    mux_router = MultiplexerRouter({LOCAL_HOST_ID: multiplexer})
    # Same router shape as the Multiplexer side — wraps the
    # JSONL-on-local-fs Watcher under `local`. Adding remote
    # hosts later means registering another entry alongside.
    watcher_router = WatcherRouter({LOCAL_HOST_ID: watcher})
    # Outbox is constructed AFTER the seq + collapse_pref services
    # so its enqueue paths can stamp both at submission time.
    outbox = Outbox(channel, message_seq=message_seq, collapse_pref=collapse_pref)

    # ReadinessService subscribes to the watcher BEFORE the
    # dispatcher. Order matters: the watcher fans out events to
    # handlers sequentially in registration order, and the panel
    # subscriber records an echo (via `echo_dedup.record`) for
    # tmux-typed user text so the dispatcher's subsequent
    # `_all_echos` check trips and we don't double-render the same
    # text as both a panel-receipt AND a standalone text card.
    # End_turn_panel itself is constructed below (it needs services
    # that aren't built yet); it hooks into readiness via
    # `on_change` — not the watcher directly — so this install
    # alone reserves the slot.
    readiness = ReadinessService()
    readiness.install(watcher_router)

    dispatcher = Dispatcher(
        channel=channel,
        watcher=watcher_router,
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        echo_dedup=echo_dedup,
        verbosity=verbosity,
        allow_list=allow_list,
    )
    dispatcher.install()

    # StatusService no longer owns its own status card surface —
    # it just scrapes spinner state and fans it out to handlers.
    # `EndTurnPanelService` subscribes below so the panel header
    # carries the live `Worked Ns` line. No more standalone
    # spinner card → no tombstones in the chat history when claude
    # goes idle.
    status_service = StatusService(
        multiplexer=mux_router,
        registry=registry,
        poll_interval=status_interval,
    )

    command_service = CommandService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        allow_list=allow_list,
    )
    command_service.install(channel)

    # HistoryService constructed before SessionsService so the
    # /session Manage card can delegate the History button into it.
    history_service = HistoryService(
        registry=registry,
        outbox=outbox,
        channel=channel,
        allow_list=allow_list,
        message_seq=message_seq,
    )
    history_service.install(channel)

    sessions_service = SessionsService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        channel=channel,
        allow_list=allow_list,
        history_service=history_service,
        verbosity=verbosity,
        message_seq=message_seq,
        collapse_pref=collapse_pref,
        hosts=hosts,
        new_projects_root=projects_root,
    )
    sessions_service.install(channel)

    ask_user_service = AskUserService(
        registry=registry,
        multiplexer=mux_router,
        channel=channel,
        allow_list=allow_list,
        message_seq=message_seq,
    )
    ask_user_service.install(channel)

    directory_service = DirectoryService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        channel=channel,
        allow_list=allow_list,
        projects_root=projects_root,
        message_seq=message_seq,
    )
    directory_service.install(channel)

    screenshot_service = ScreenshotService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        channel=channel,
        allow_list=allow_list,
        message_seq=message_seq,
    )
    screenshot_service.install(channel)

    live_pane_service = LivePaneService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        channel=channel,
        allow_list=allow_list,
        message_seq=message_seq,
    )
    live_pane_service.install(channel)

    usage_service = UsageService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        allow_list=allow_list,
    )
    usage_service.install(channel)

    server_service = ServerService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        channel=channel,
        admin_list=admin_list,
        multiplexer_session_name=multiplexer_session_name,
        paige_dir=paige_dir,
        hosts=hosts,
        message_seq=message_seq,
    )
    server_service.install(channel)

    voice_service: VoiceService | None = None
    # VoiceService runs unconditionally if installed — internally it
    # short-circuits on text-bearing inbounds and on missing
    # transcriber config. Construct it whenever the channel might
    # produce audio inbounds (any backend), and pass the transcriber
    # through; the service handles None gracefully with a hint.
    voice_service = VoiceService(
        registry=registry,
        multiplexer=mux_router,
        outbox=outbox,
        transcriber=transcriber,
        allow_list=allow_list,
    )
    voice_service.install(channel)

    interactive_ui_service = InteractiveUIService(
        multiplexer=mux_router,
        registry=registry,
        outbox=outbox,
        channel=channel,
        allow_list=allow_list,
        message_seq=message_seq,
        poll_interval=status_interval,
        live_pane=live_pane_service,
    )
    interactive_ui_service.install(channel)

    run_discovery = RunDiscovery(
        multiplexer=mux_router,
        registry=registry,
        watcher=watcher_router,
        poll_interval=discovery_interval,
    )

    # Quick-reply prefs + end-turn panel — fires a 4-input card to
    # every binding when the run's stop_reason flips to end_turn.
    quick_reply = QuickReplyPrefs()
    end_turn_panel = EndTurnPanelService(
        channel=channel,
        registry=registry,
        outbox=outbox,
        multiplexer=mux_router,
        echo_dedup=echo_dedup,
        readiness=readiness,
        quick_reply=quick_reply,
        allow_list=allow_list,
    )
    end_turn_panel.install()

    # StatusCarrierService — migrates the live status badge to the
    # most recent outbound card per (person, conversation). Two
    # subscriptions: `Outbox.on_send_complete` (tracks new
    # carriers) and `StatusService.on_change` (updates the badge
    # text). Without it, the status would stay frozen on the
    # original panel anchor as new tool_use / tool_result cards
    # pile up below — Phase 1 of the status-on-panel design left
    # that gap; this closes it.
    status_carrier = StatusCarrierService(outbox=outbox)
    status_carrier.install()
    status_service.on_change(status_carrier.on_status_change)

    return App(
        channel=channel,
        multiplexer=mux_router,
        watcher=watcher_router,
        storage=storage,
        registry=registry,
        outbox=outbox,
        echo_dedup=echo_dedup,
        verbosity=verbosity,
        dispatcher=dispatcher,
        status_service=status_service,
        command_service=command_service,
        sessions_service=sessions_service,
        directory_service=directory_service,
        screenshot_service=screenshot_service,
        live_pane_service=live_pane_service,
        history_service=history_service,
        usage_service=usage_service,
        server_service=server_service,
        voice_service=voice_service,
        interactive_ui_service=interactive_ui_service,
        run_discovery=run_discovery,
        readiness=readiness,
        quick_reply=quick_reply,
        end_turn_panel=end_turn_panel,
        status_carrier=status_carrier,
        hosts=hosts,
        allow_list=allow_list,
        admin_list=admin_list,
    )


__all__ = ["App", "assemble"]
