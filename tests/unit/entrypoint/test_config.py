"""Config — env loading + validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.entrypoint.config import Config, ConfigError


# Helper — most tests need a valid Feishu credential pair seeded
# before exercising other knobs.
def _feishu_env(**extra: str) -> dict[str, str]:
    return {
        "PAIGE_FEISHU_APP_ID": "cli_x",
        "PAIGE_FEISHU_APP_SECRET": "secret_x",
        **extra,
    }


# ── feishu credentials ──────────────────────────────────────────


def test_missing_feishu_app_id_raises() -> None:
    with pytest.raises(ConfigError, match="FEISHU"):
        Config.from_env({"PAIGE_FEISHU_APP_SECRET": "y"})


def test_missing_feishu_app_secret_raises() -> None:
    with pytest.raises(ConfigError, match="FEISHU"):
        Config.from_env({"PAIGE_FEISHU_APP_ID": "x"})


def test_feishu_domain_optional() -> None:
    cfg = Config.from_env(_feishu_env())
    assert cfg.feishu_domain == ""


def test_feishu_domain_explicit() -> None:
    cfg = Config.from_env(_feishu_env(PAIGE_FEISHU_DOMAIN="https://open.larksuite.com"))
    assert cfg.feishu_domain == "https://open.larksuite.com"


# ── paths + intervals ───────────────────────────────────────────


def test_default_paige_dir_is_under_home() -> None:
    cfg = Config.from_env(_feishu_env())
    assert cfg.paige_dir.is_absolute()
    assert ".paige" in cfg.paige_dir.parts


def test_explicit_paige_dir() -> None:
    cfg = Config.from_env(_feishu_env(PAIGE_DIR="/tmp/state"))
    assert cfg.paige_dir == Path("/tmp/state")


def test_default_tmux_session() -> None:
    cfg = Config.from_env(_feishu_env())
    assert cfg.tmux_session == "paige"


def test_explicit_tmux_session() -> None:
    cfg = Config.from_env(_feishu_env(PAIGE_TMUX_SESSION="ci-runner"))
    assert cfg.tmux_session == "ci-runner"


def test_default_intervals() -> None:
    cfg = Config.from_env(_feishu_env())
    assert cfg.status_interval == 1.0
    assert cfg.watcher_interval == 2.0


def test_explicit_intervals() -> None:
    cfg = Config.from_env(_feishu_env(PAIGE_STATUS_INTERVAL="0.5", PAIGE_WATCHER_INTERVAL="5"))
    assert cfg.status_interval == 0.5
    assert cfg.watcher_interval == 5.0


def test_invalid_interval_raises() -> None:
    with pytest.raises(ConfigError, match="STATUS_INTERVAL"):
        Config.from_env(_feishu_env(PAIGE_STATUS_INTERVAL="abc"))


def test_negative_interval_raises() -> None:
    with pytest.raises(ConfigError, match="positive"):
        Config.from_env(_feishu_env(PAIGE_STATUS_INTERVAL="-1"))


def test_zero_interval_raises() -> None:
    with pytest.raises(ConfigError, match="positive"):
        Config.from_env(_feishu_env(PAIGE_WATCHER_INTERVAL="0"))


def test_config_is_frozen() -> None:
    cfg = Config.from_env(_feishu_env())
    with pytest.raises(Exception):
        cfg.feishu_app_id = "changed"  # type: ignore[misc]


# ── allow-list ─────────────────────────────────────────────────


def test_default_allowed_users_is_empty() -> None:
    cfg = Config.from_env(_feishu_env())
    assert cfg.allowed_users == frozenset()


def test_allowed_users_csv_parses() -> None:
    cfg = Config.from_env(_feishu_env(PAIGE_ALLOWED_USERS="u-1, u-2,u-3"))
    assert cfg.allowed_users == frozenset({"u-1", "u-2", "u-3"})


def test_allowed_users_blank_entries_dropped() -> None:
    cfg = Config.from_env(_feishu_env(PAIGE_ALLOWED_USERS="u-1,, ,u-2"))
    assert cfg.allowed_users == frozenset({"u-1", "u-2"})


def test_default_admin_users_is_empty() -> None:
    cfg = Config.from_env(_feishu_env())
    assert cfg.admin_users == frozenset()


def test_admin_users_csv_parses() -> None:
    cfg = Config.from_env(_feishu_env(PAIGE_ADMIN_USERS="u-alice, u-bob"))
    assert cfg.admin_users == frozenset({"u-alice", "u-bob"})
