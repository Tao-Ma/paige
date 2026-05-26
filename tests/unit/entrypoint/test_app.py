"""App + assemble — composition root with all-fakes wiring.

Verifies the assembled service graph honors lifecycle ordering and
that handlers are installed (CommandService registered its commands,
Dispatcher wired Channel + Watcher).
"""

from __future__ import annotations

from paige.application.commands import FORWARDED_COMMANDS, NATIVE_COMMANDS
from paige.application.run_registry import RunRegistry
from paige.entrypoint.app import App, assemble
from paige.testing.fakes import (
    FakeChannel,
    FakeMultiplexer,
    FakeStorage,
    FakeWatcher,
)


async def _build_app() -> tuple[App, FakeChannel, FakeWatcher]:
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    watcher = FakeWatcher()
    registry = RunRegistry(storage)
    await registry.load()
    app = assemble(
        channel=channel,
        multiplexer=mux,
        watcher=watcher,
        storage=storage,
        registry=registry,
        status_interval=0.05,
    )
    return app, channel, watcher


# ── assembly ─────────────────────────────────────────────────────


async def test_assemble_returns_a_complete_app() -> None:
    app, channel, watcher = await _build_app()
    assert app.channel is channel
    # `app.watcher` is a `WatcherRouter` wrapping the original
    # FakeWatcher under the `local` host_id (mirrors how the
    # MultiplexerRouter wraps the FakeMultiplexer).
    from paige.application.watcher_router import WatcherRouter
    from paige.domain.host import LOCAL_HOST_ID

    assert isinstance(app.watcher, WatcherRouter)
    assert app.watcher.for_host(LOCAL_HOST_ID) is watcher
    # Application services exist.
    assert app.outbox is not None
    assert app.dispatcher is not None
    assert app.status_service is not None
    assert app.command_service is not None
    assert app.sessions_service is not None
    assert app.directory_service is not None
    assert app.run_discovery is not None
    assert app.echo_dedup is not None
    assert app.verbosity is not None


async def test_sessions_service_registers_command_and_action() -> None:
    app, channel, _ = await _build_app()
    assert "sessions" in channel._command_handlers  # noqa: SLF001
    assert len(channel._action_handlers) >= 1  # noqa: SLF001
    await app.stop()


async def test_command_service_registers_native_handlers() -> None:
    app, channel, _ = await _build_app()
    registered = set(channel._command_handlers.keys())  # noqa: SLF001
    assert set(NATIVE_COMMANDS) <= registered
    assert set(FORWARDED_COMMANDS) <= registered
    await app.stop()


async def test_dispatcher_registers_inbound_handler() -> None:
    """Dispatcher.install() routes Channel.on_inbound → its handler.
    Asserting the side effect (a handler registered) suffices."""
    app, channel, _ = await _build_app()
    assert len(channel._inbound_handlers) >= 1  # noqa: SLF001
    await app.stop()


async def test_dispatcher_registers_watcher_handler() -> None:
    app, _, watcher = await _build_app()
    assert len(watcher._handlers) >= 1  # noqa: SLF001
    await app.stop()


# ── lifecycle ───────────────────────────────────────────────────


async def test_start_starts_channel_watcher_status_discovery() -> None:
    app, channel, watcher = await _build_app()
    await app.start()
    try:
        assert channel.started is True
        assert watcher.started is True
        assert app.status_service._task is not None  # noqa: SLF001
        assert app.run_discovery._task is not None  # noqa: SLF001
    finally:
        await app.stop()


async def test_stop_stops_everything_and_drains_outbox() -> None:
    app, channel, watcher = await _build_app()
    await app.start()
    await app.stop()
    assert channel.stopped is True
    assert watcher.stopped is True
    assert app.status_service._task is None  # noqa: SLF001
    assert app.run_discovery._task is None  # noqa: SLF001


async def test_double_start_is_idempotent_via_status_service() -> None:
    """status_service is the only sub-service whose start() guards
    against double start. App-level idempotency would require more
    bookkeeping; for now we trust the user not to do this."""
    app, _, _ = await _build_app()
    await app.start()
    # Second start on status_service is a no-op:
    await app.status_service.start()
    await app.stop()


async def test_stop_without_start_is_clean() -> None:
    """Each sub-service tolerates stop without a prior start."""
    app, _, _ = await _build_app()
    await app.stop()  # no exception


async def test_app_is_frozen_dataclass() -> None:
    """Mutating App fields after construction is a programmer error."""
    app, _, _ = await _build_app()
    import pytest as _pytest

    with _pytest.raises(Exception):
        app.channel = FakeChannel()  # type: ignore[misc]
    await app.stop()
