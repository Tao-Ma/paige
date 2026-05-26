"""main — async entry point.

`asyncio.run(amain())` is the production launcher. SIGINT / SIGTERM
flip a stop event; App.stop() drains queues and shuts adapters down
in order before the process exits.

This is the **only** module that imports concrete adapters
(`paige.adapters.*`). Everywhere else routes through the ports.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from ..adapters.jsonl_watcher import JsonlWatcher
from ..adapters.storage import FileStorage
from ..adapters.tmux import TmuxMultiplexer
from ..application.access import AdminList, AllowList
from ..application.run_registry import RunRegistry
from ..ports.channel import Channel
from ..ports.transcriber import Transcriber
from .app import App, assemble
from .config import Config

logger = logging.getLogger(__name__)


async def build_app(config: Config) -> App:
    """Construct concrete adapters and assemble an App.

    The state directory is created if missing; FileStorage's atomic
    writes need it to exist.
    """
    config.paige_dir.mkdir(parents=True, exist_ok=True)
    storage = FileStorage(config.paige_dir)

    multiplexer = TmuxMultiplexer(default_session=config.tmux_session)
    watcher = JsonlWatcher(storage, poll_interval=config.watcher_interval)
    channel = _build_channel(config)
    transcriber = _build_transcriber(config)

    registry = RunRegistry(storage)
    await registry.load()

    return assemble(
        channel=channel,
        multiplexer=multiplexer,
        watcher=watcher,
        storage=storage,
        registry=registry,
        allow_list=AllowList(config.allowed_users),
        admin_list=AdminList(
            admins=config.admin_users,
            allowed=config.allowed_users,
        ),
        transcriber=transcriber,
        projects_root=config.projects_root,
        paige_dir=config.paige_dir,
        multiplexer_session_name=config.tmux_session,
        status_interval=config.status_interval,
    )


def _build_channel(config: Config) -> Channel:
    """Construct the Feishu channel. Import is local so the
    [feishu] extra stays optional."""
    from ..adapters.feishu.channel import FeishuChannel
    from ..adapters.feishu.lark_client import LarkClientWrapper

    client = LarkClientWrapper(
        config.feishu_app_id,
        config.feishu_app_secret,
        domain=config.feishu_domain or None,
    )
    return FeishuChannel(client=client, paige_group_id=config.feishu_group_id)


def _build_transcriber(config: Config) -> Transcriber | None:
    """Optional voice-to-text. Constructed only when the key is set;
    otherwise audio inbounds get a 'not configured' hint at runtime.
    Local import keeps `[voice]` extra optional."""
    if not config.openai_api_key:
        return None
    from ..adapters.openai_transcriber import OpenAITranscriber

    kwargs: dict[str, object] = {}
    if config.openai_base_url:
        kwargs["base_url"] = config.openai_base_url
    return OpenAITranscriber(config.openai_api_key, **kwargs)  # type: ignore[arg-type]


async def amain() -> None:
    """Async entrypoint. Blocks until SIGINT/SIGTERM, then drains."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = Config.from_env()
    if config.feishu_group_id:
        logger.info("Feishu topic-mode group declared: %s", config.feishu_group_id)
    app = await build_app(config)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await app.start()
    try:
        await stop_event.wait()
        logger.info("Shutdown signal received")
    finally:
        await app.stop()


def main() -> None:
    """Sync entry — wraps `amain` in `asyncio.run`."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()
