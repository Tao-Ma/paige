"""HostsService — registry of hosts paige can operate on."""

from __future__ import annotations

from pathlib import Path

from paige.application.hosts import HostsService, load_hosts_toml
from paige.domain.host import LOCAL, LOCAL_HOST_ID, Host


def test_default_constructor_has_local_only() -> None:
    s = HostsService()
    assert s.list() == [LOCAL]


def test_get_local_returns_local_constant() -> None:
    s = HostsService()
    assert s.get(LOCAL_HOST_ID) is LOCAL


def test_get_unknown_id_returns_local_as_fallback() -> None:
    """Stale bindings (host removed from config but binding still
    referencing it) should still resolve to a working multiplexer
    rather than crashing the lookup."""
    s = HostsService()
    assert s.get("never-configured") is LOCAL


def test_get_empty_or_none_returns_local() -> None:
    s = HostsService()
    assert s.get("") is LOCAL
    assert s.get(None) is LOCAL


def test_extra_hosts_appear_in_list() -> None:
    dev1 = Host(host_id="dev-1", name="Dev box")
    s = HostsService([dev1])
    assert s.list() == [LOCAL, dev1]
    assert s.get("dev-1") is dev1


def test_local_cannot_be_shadowed_by_extra_host() -> None:
    """Even if a config entry literally names itself `local`, the
    synthetic LOCAL stays — paige's own machinery (channel, outbox,
    transcribers) is bound to the local box and there's nowhere for
    them to run if the local entry were replaced."""
    masquerade = Host(host_id=LOCAL_HOST_ID, name="not really local")
    s = HostsService([masquerade])
    assert s.get(LOCAL_HOST_ID) is LOCAL
    # The masquerade entry shouldn't appear in the listing either.
    assert s.list() == [LOCAL]


# ── load_hosts_toml ─────────────────────────────────────────────────


def test_load_hosts_toml_missing_file_returns_empty(tmp_path: Path) -> None:
    """No hosts.toml is the common case — must not warn or error."""
    assert load_hosts_toml(tmp_path / "hosts.toml") == []


def test_load_hosts_toml_empty_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text("", encoding="utf-8")
    assert load_hosts_toml(p) == []


def test_load_hosts_toml_no_host_table_returns_empty(tmp_path: Path) -> None:
    """A file with unrelated keys but no `[[host]]` array is fine."""
    p = tmp_path / "hosts.toml"
    p.write_text('# comment\nother = "value"\n', encoding="utf-8")
    assert load_hosts_toml(p) == []


def test_load_hosts_toml_parse_error_returns_empty(tmp_path: Path, caplog) -> None:
    """A typo in hosts.toml must not bring paige down — warn + empty."""
    p = tmp_path / "hosts.toml"
    p.write_text("this is = not = valid toml [[\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_hosts_toml(p) == []
    assert any("hosts.toml parse failed" in r.message for r in caplog.records)


def test_load_hosts_toml_valid_entry(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        '[[host]]\nid = "dev-1"\nname = "Dev box"\nssh = "user@dev-1:22"\n',
        encoding="utf-8",
    )
    hosts = load_hosts_toml(p)
    assert hosts == [Host(host_id="dev-1", name="Dev box", ssh="user@dev-1:22")]


def test_load_hosts_toml_minimal_entry(tmp_path: Path) -> None:
    """`name` and `ssh` are optional; only `id` is required."""
    p = tmp_path / "hosts.toml"
    p.write_text('[[host]]\nid = "dev-2"\n', encoding="utf-8")
    assert load_hosts_toml(p) == [Host(host_id="dev-2", name="", ssh="")]


def test_load_hosts_toml_missing_id_skipped(tmp_path: Path, caplog) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        '[[host]]\nname = "no id here"\n[[host]]\nid = "ok"\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        hosts = load_hosts_toml(p)
    assert hosts == [Host(host_id="ok")]
    assert any("missing/empty `id`" in r.message for r in caplog.records)


def test_load_hosts_toml_empty_id_skipped(tmp_path: Path) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text('[[host]]\nid = "   "\n', encoding="utf-8")
    assert load_hosts_toml(p) == []


def test_load_hosts_toml_local_id_dropped(tmp_path: Path, caplog) -> None:
    """`local` is synthesised by HostsService — silently dropping a
    user typo here keeps it from disabling local-host operations."""
    p = tmp_path / "hosts.toml"
    p.write_text(
        '[[host]]\nid = "local"\nname = "user typo"\n[[host]]\nid = "dev-1"\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        hosts = load_hosts_toml(p)
    assert hosts == [Host(host_id="dev-1")]
    assert any("ignoring `local` entry" in r.message for r in caplog.records)


def test_load_hosts_toml_duplicate_id_first_wins(tmp_path: Path, caplog) -> None:
    p = tmp_path / "hosts.toml"
    p.write_text(
        '[[host]]\nid = "dev-1"\nname = "first"\n[[host]]\nid = "dev-1"\nname = "second"\n',
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        hosts = load_hosts_toml(p)
    assert hosts == [Host(host_id="dev-1", name="first")]
    assert any("duplicate id" in r.message for r in caplog.records)


def test_load_hosts_toml_non_list_host_key_warned(tmp_path: Path, caplog) -> None:
    """`host = "..."` (scalar) instead of `[[host]]` (array of tables)
    is a common typo — log and skip rather than crash."""
    p = tmp_path / "hosts.toml"
    p.write_text('host = "dev-1"\n', encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_hosts_toml(p) == []
    assert any("expected `host` array" in r.message for r in caplog.records)


def test_load_hosts_toml_loaded_entries_visible_in_service(tmp_path: Path) -> None:
    """Round-trip: loader output is consumable by HostsService."""
    p = tmp_path / "hosts.toml"
    p.write_text(
        '[[host]]\nid = "dev-1"\nname = "Dev box"\nssh = "dev1.lan"\n',
        encoding="utf-8",
    )
    s = HostsService(load_hosts_toml(p))
    assert s.get("dev-1").ssh == "dev1.lan"
    assert s.list()[0] is LOCAL
    assert len(s.list()) == 2
