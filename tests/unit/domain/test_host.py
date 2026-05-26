"""Host domain object — id + display name."""

from __future__ import annotations

from paige.domain.host import LOCAL, LOCAL_HOST_ID, Host


def test_local_constant_has_known_id() -> None:
    assert LOCAL.host_id == LOCAL_HOST_ID == "local"


def test_display_name_falls_back_to_id() -> None:
    assert Host(host_id="dev-1").display_name == "dev-1"


def test_display_name_prefers_explicit_name() -> None:
    assert Host(host_id="dev-1", name="Dev box").display_name == "Dev box"


def test_host_is_frozen_and_hashable() -> None:
    """Bindings + RunPointers carry host_id (a str), but Host
    itself should still be safe to put in sets / dict keys for
    future routing tables."""
    s = {Host(host_id="a"), Host(host_id="b"), Host(host_id="a")}
    assert len(s) == 2


def test_ssh_defaults_to_empty() -> None:
    """`ssh` is the SSH destination string, populated from
    hosts.toml's `ssh = "..."` field. Empty for the synthetic LOCAL
    and for entries that omit the field."""
    assert Host(host_id="dev-1").ssh == ""
    assert LOCAL.ssh == ""


def test_ssh_field_round_trip() -> None:
    h = Host(host_id="dev-1", name="Dev box", ssh="user@dev-1:22")
    assert h.ssh == "user@dev-1:22"
