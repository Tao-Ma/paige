"""Host — the box paige is operating on.

Today every paige deployment manages tmux + claude on a single host
(its own). The multi-host plan (`doc/multi-host.md`) extends this so
one paige instance can broker bindings on remote hosts via SSH. To
get the in-memory model ready for that without shipping the SSH
adapter yet, we introduce `Host` as a first-class domain object and
thread `host_id` through `Binding` / `RunPointer` / persistence.

For now the only `Host` that exists at runtime is the synthetic
`LOCAL`. Adding remote hosts is a future config-file slice
(`~/.paige/hosts.toml`) plus the SSH multiplexer / watcher
adapters — neither implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass

LOCAL_HOST_ID = "local"


@dataclass(frozen=True)
class Host:
    """A box paige can operate on.

    `host_id` is the persistent key — written into `Binding.host_id`
    and `RunPointer.host_id`, used to look up the right multiplexer
    instance when the SSH router slice lands.

    `name` is the display label (Manage card badge, /server card
    rows). Falls back to `host_id` when empty.

    `ssh` is the SSH destination string (anything `ssh(1)` accepts —
    `user@host:port` or a `~/.ssh/config` alias). Empty for the
    synthetic `local` host. Populated from `~/.paige/hosts.toml`'s
    `ssh = "..."` field on remote entries; consumed by the future
    SSH multiplexer/watcher adapters when they pick the destination.
    """

    host_id: str
    name: str = ""
    ssh: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.host_id


LOCAL = Host(host_id=LOCAL_HOST_ID, name="local")


__all__ = ["LOCAL", "LOCAL_HOST_ID", "Host"]
