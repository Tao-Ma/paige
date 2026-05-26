"""Config — startup env-var loader.

Full inventory (every PAIGE_* var, defaults, consumer service) lives
in `doc/config.md`. The companion install template is `env.example`
at the repo root. Keep all three (this file's parser, the doc table,
the template) in sync on every addition — see the "Adding a new env
var" checklist in `doc/config.md`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required config is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Top-level config; immutable for the process lifetime."""

    paige_dir: Path
    tmux_session: str = "paige"
    status_interval: float = 1.0
    watcher_interval: float = 2.0
    allowed_users: frozenset[str] = frozenset()
    admin_users: frozenset[str] = frozenset()
    projects_root: Path = Path.home() / "projects"
    # Feishu credentials
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = ""
    # Feishu topic-mode group: when set, the operator has opted into
    # pane-per-topic routing within a shared topic-mode group
    # (`chat_mode=group`, `group_message_type=thread`). Paige doesn't
    # gate behavior on this — bindings just key on whatever
    # `topic_id` the parser reports — but logging it at startup
    # makes operator intent discoverable.
    feishu_group_id: str = ""
    # Voice transcription (optional, off by default)
    openai_api_key: str = ""
    openai_base_url: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        """Load from `os.environ` (or an explicit dict for tests).

        Raises `ConfigError` for missing or invalid required fields.
        """
        e = env if env is not None else dict(os.environ)

        paige_dir_str = e.get("PAIGE_DIR", "~/.paige").strip() or "~/.paige"
        paige_dir = Path(paige_dir_str).expanduser().resolve()

        tmux_session = e.get("PAIGE_TMUX_SESSION", "paige").strip() or "paige"

        status_interval = _parse_float(e, "PAIGE_STATUS_INTERVAL", 1.0)
        watcher_interval = _parse_float(e, "PAIGE_WATCHER_INTERVAL", 2.0)

        allowed_users = _parse_csv(e, "PAIGE_ALLOWED_USERS")
        admin_users = _parse_csv(e, "PAIGE_ADMIN_USERS")

        projects_root_str = e.get("PAIGE_PROJECTS_ROOT", "").strip() or "~/projects"
        projects_root = Path(projects_root_str).expanduser().resolve()

        feishu_app_id = e.get("PAIGE_FEISHU_APP_ID", "").strip()
        feishu_app_secret = e.get("PAIGE_FEISHU_APP_SECRET", "").strip()
        feishu_domain = e.get("PAIGE_FEISHU_DOMAIN", "").strip()
        feishu_group_id = e.get("PAIGE_FEISHU_GROUP_ID", "").strip()
        if not feishu_app_id or not feishu_app_secret:
            raise ConfigError("PAIGE_FEISHU_APP_ID and PAIGE_FEISHU_APP_SECRET are required")

        openai_api_key = e.get("PAIGE_OPENAI_API_KEY", "").strip()
        openai_base_url = e.get("PAIGE_OPENAI_BASE_URL", "").strip()

        return cls(
            paige_dir=paige_dir,
            tmux_session=tmux_session,
            status_interval=status_interval,
            watcher_interval=watcher_interval,
            allowed_users=allowed_users,
            admin_users=admin_users,
            projects_root=projects_root,
            feishu_app_id=feishu_app_id,
            feishu_app_secret=feishu_app_secret,
            feishu_domain=feishu_domain,
            feishu_group_id=feishu_group_id,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
        )


def _parse_float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number: {raw!r}") from e
    if value <= 0:
        raise ConfigError(f"{key} must be positive: {value}")
    return value


def _parse_csv(env: dict[str, str], key: str) -> frozenset[str]:
    raw = env.get(key, "").strip()
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())
