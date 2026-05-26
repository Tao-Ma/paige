"""MultiplexerRouter — host_id → Multiplexer adapter dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.multiplexer_router import MultiplexerRouter
from paige.domain.host import LOCAL_HOST_ID
from paige.testing.fakes import FakeMultiplexer


def _new_router_local_only() -> tuple[MultiplexerRouter, FakeMultiplexer]:
    local = FakeMultiplexer()
    return MultiplexerRouter({LOCAL_HOST_ID: local}), local


def _new_router_two_hosts() -> tuple[MultiplexerRouter, FakeMultiplexer, FakeMultiplexer]:
    local = FakeMultiplexer()
    dev1 = FakeMultiplexer()
    return MultiplexerRouter({LOCAL_HOST_ID: local, "dev-1": dev1}), local, dev1


def test_constructor_requires_local_entry() -> None:
    """Local is the unknown-host fallback and the home of paige's
    own machinery — without it the router would be ill-defined."""
    with pytest.raises(ValueError, match="local"):
        MultiplexerRouter({"dev-1": FakeMultiplexer()})


def test_for_host_returns_local_by_default() -> None:
    router, local = _new_router_local_only()
    assert router.for_host(LOCAL_HOST_ID) is local


def test_for_host_unknown_falls_back_to_local() -> None:
    """Stale binding referring to a host removed from config still
    routes to a working multiplexer rather than crashing the lookup."""
    router, local = _new_router_local_only()
    assert router.for_host("never-configured") is local


def test_for_host_returns_named_adapter() -> None:
    router, _local, dev1 = _new_router_two_hosts()
    assert router.for_host("dev-1") is dev1


# ── Default host_id routes to local ──────────────────────────────


async def test_send_keys_default_host_id_routes_to_local() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "p", Path("/p"))
    dev1.add_pane("@1", "p", Path("/p"))

    await router.send_keys("@1", "hello")

    # Default host_id="local" → local saw the call, dev1 didn't.
    assert len(local.send_keys_calls) == 1
    assert local.send_keys_calls[0].text == "hello"
    assert dev1.send_keys_calls == []


async def test_send_keys_explicit_host_id_routes_to_remote() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "p", Path("/p"))
    dev1.add_pane("@1", "p", Path("/p"))

    await router.send_keys("@1", "remote payload", host_id="dev-1")

    assert local.send_keys_calls == []
    assert len(dev1.send_keys_calls) == 1
    assert dev1.send_keys_calls[0].text == "remote payload"


async def test_send_keys_unknown_host_falls_back_to_local() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "p", Path("/p"))

    ok = await router.send_keys("@1", "huh", host_id="vanished")

    # Local got it; dev-1 didn't (the unknown id fell to local fallback).
    assert ok is True
    assert len(local.send_keys_calls) == 1
    assert dev1.send_keys_calls == []


# ── Coverage: every dispatched method honours host_id ────────────


async def test_capture_routes_by_host() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@7", "p", Path("/p"))
    dev1.add_pane("@7", "p", Path("/p"))
    local.set_capture("@7", "from-local")
    dev1.set_capture("@7", "from-dev1")

    assert await router.capture("@7") == "from-local"
    assert await router.capture("@7", host_id="dev-1") == "from-dev1"


async def test_list_panes_routes_by_host() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "loc", Path("/p"))
    dev1.add_pane("@9", "rem", Path("/p"))

    local_panes = await router.list_panes()
    dev1_panes = await router.list_panes(host_id="dev-1")

    assert {p.pane_id for p in local_panes} == {"@1"}
    assert {p.pane_id for p in dev1_panes} == {"@9"}


async def test_find_pane_routes_by_host() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "loc", Path("/p"))
    dev1.add_pane("@1", "rem", Path("/p"))

    found_local = await router.find_pane("@1")
    found_dev1 = await router.find_pane("@1", host_id="dev-1")

    assert found_local is not None and found_local.pane_name == "loc"
    assert found_dev1 is not None and found_dev1.pane_name == "rem"


async def test_create_pane_routes_by_host() -> None:
    router, local, dev1 = _new_router_two_hosts()

    await router.create_pane(name="loc-1", cwd=Path("/p"))
    await router.create_pane(name="rem-1", cwd=Path("/p"), host_id="dev-1")

    assert [p.pane_name for p in local.created] == ["loc-1"]
    assert [p.pane_name for p in dev1.created] == ["rem-1"]


async def test_kill_pane_routes_by_host() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "p", Path("/p"))
    dev1.add_pane("@1", "p", Path("/p"))

    await router.kill_pane("@1")
    await router.kill_pane("@1", host_id="dev-1")

    assert local.killed == ["@1"]
    assert dev1.killed == ["@1"]


async def test_rename_pane_routes_by_host() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "old", Path("/p"))
    dev1.add_pane("@1", "old", Path("/p"))

    await router.rename_pane("@1", "fresh")
    await router.rename_pane("@1", "fresh-rem", host_id="dev-1")

    assert local.renamed == [("@1", "fresh")]
    assert dev1.renamed == [("@1", "fresh-rem")]


async def test_get_foreground_pid_routes_by_host() -> None:
    router, local, dev1 = _new_router_two_hosts()
    local.add_pane("@1", "p", Path("/p"))
    dev1.add_pane("@1", "p", Path("/p"))
    local.set_foreground_pid("@1", 1111)
    dev1.set_foreground_pid("@1", 9999)

    assert await router.get_foreground_pid("@1") == 1111
    assert await router.get_foreground_pid("@1", host_id="dev-1") == 9999
