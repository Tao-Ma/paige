"""HostsService — registry of hosts paige can operate on.

Today this is a thin wrapper around a single synthetic `local` host
plus any remotes loaded from `~/.paige/hosts.toml`. The SSH adapter
(Steps 9–10 in `doc/multi-host.md`) hasn't shipped yet, so remote
entries show up in `/sessions` and `/server` cards as "disconnected"
placeholders — the UX is wired, the actual remote behaviour is not.

`get(host_id)` returns LOCAL for `"local"` / None / unknown ids so
callers can route a "missing host_id" binding to the local mux
without crashing.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, cast

from ..domain.host import LOCAL, LOCAL_HOST_ID, Host

logger = logging.getLogger(__name__)


class HostsService:
    """Registry of available hosts. Local-only until the SSH slice."""

    def __init__(self, hosts: list[Host] | None = None) -> None:
        # The synthetic LOCAL host is always present, even if the
        # caller passes an explicit list — paige's own machinery
        # (channel, outbox, transcribers) is bound to the local box
        # and would have nowhere to run if local were removable.
        self._by_id: dict[str, Host] = {LOCAL_HOST_ID: LOCAL}
        if hosts:
            for h in hosts:
                if h.host_id == LOCAL_HOST_ID:
                    continue  # don't let callers shadow LOCAL
                self._by_id[h.host_id] = h

    def get(self, host_id: str | None) -> Host:
        """Return the Host for `host_id`, or LOCAL when unknown.

        Defaults to LOCAL on missing / empty / unknown ids so callers
        with a stale `host_id` (e.g. a binding that referred to a
        host that's since been removed from config) still resolve to
        a working multiplexer rather than raising. The caller is
        responsible for showing a "host gone" hint if that matters."""
        if not host_id:
            return LOCAL
        return self._by_id.get(host_id, LOCAL)

    def list(self) -> list[Host]:
        """All known hosts. Order: LOCAL first, then config order."""
        return list(self._by_id.values())


# ── hosts.toml loader ──────────────────────────────────────────────


def load_hosts_toml(path: Path) -> list[Host]:
    """Read `path` (typically `~/.paige/hosts.toml`) and return the
    Host entries. Resilient: missing file / parse failure / malformed
    entries each produce a logged warning + empty (or partial) list,
    never an exception. paige's startup must survive a typo in
    hosts.toml.

    Schema (per `doc/multi-host.md`):

        [[host]]
        id = "dev-1"            # required, non-empty, NOT "local"
        name = "Dev box"        # optional display label
        ssh  = "user@dev-1:22"  # optional SSH destination

    A `local` entry is silently dropped — `HostsService` synthesises
    LOCAL itself and rejecting it here keeps the user's typo from
    disabling local-host operations. Duplicate ids: the first wins,
    subsequent are warned and skipped.
    """
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        logger.warning("hosts.toml parse failed at %s: %s", path, e)
        return []
    raw_hosts = data.get("host")
    if raw_hosts is None:
        return []  # empty file or no [[host]] tables — fine
    if not isinstance(raw_hosts, list):
        logger.warning(
            "hosts.toml: expected `host` array, got %s; ignoring",
            type(raw_hosts).__name__,
        )
        return []
    out: list[Host] = []
    seen: set[str] = set()
    for entry in cast(list[Any], raw_hosts):
        host = _parse_host_entry(entry, seen)
        if host is not None:
            out.append(host)
            seen.add(host.host_id)
    return out


def _parse_host_entry(entry: Any, seen: set[str]) -> Host | None:
    """Validate one `[[host]]` table. Returns None (with a warning
    logged) for any entry that's malformed, missing a non-empty
    `id`, attempts to shadow `local`, or duplicates an earlier id."""
    if not isinstance(entry, dict):
        logger.warning("hosts.toml: skip non-table host entry: %r", entry)
        return None
    table = cast(dict[str, Any], entry)
    host_id = str(table.get("id", "")).strip()
    if not host_id:
        logger.warning("hosts.toml: skip host with missing/empty `id`")
        return None
    if host_id == LOCAL_HOST_ID:
        logger.warning("hosts.toml: ignoring `local` entry (synthesised by HostsService)")
        return None
    if host_id in seen:
        logger.warning("hosts.toml: duplicate id %r — keeping the first occurrence", host_id)
        return None
    name = str(table.get("name", "")).strip()
    ssh = str(table.get("ssh", "")).strip()
    return Host(host_id=host_id, name=name, ssh=ssh)


__all__ = ["HostsService", "load_hosts_toml"]
