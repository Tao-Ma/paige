"""FeishuClient — adapter-internal abstraction over lark-oapi.

`FeishuChannel` works against this Protocol so:
  - tests use `FakeFeishuClient` (in `paige.testing`) and don't
    pull in lark-oapi
  - the production wrapper (`LarkClientWrapper`, slice 15d) can
    layer retries + token refresh + WS lifecycle without making the
    channel any more complex
  - request/response shapes are paige-normalized dicts, not lark
    types — keeps Feishu codes (230002 etc.) inspectable by the
    channel without leaking the SDK

A `FeishuResponse` carries the Feishu `code`/`msg`/`data` triple.
On success `code == 0`; non-zero codes signal API-level errors
(rate-limit, deleted-chat, stale-token, etc.). The channel
inspects them rather than raising, so handlers can decide what
they mean (probe maps "deleted-chat" codes to False; outbound
treats most non-zero as "log + drop").

`register_message_handler` / `register_action_handler` install
delivery callbacks. Inbound dispatch is kept dict-typed at this
boundary (mirroring the inbound.py converter) so tests don't
need lark-oapi types either.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable

# Handlers receive the raw event dict the WS gave us; the channel
# normalizes it into Inbound/ActionEvent via inbound.py.
MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
ActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class FeishuResponse:
    """Feishu's standard `code`/`msg`/`data` envelope.

    `code == 0` means success. Specific non-zero codes carry
    semantic meaning the channel layer cares about:
      - 230002 disbanded chat
      - 230003 bot not in chat
      - 230020 chat not found
      - 99991663 stale tenant token (retry-once trigger)
      - 99991400 rate-limit
    """

    code: int
    msg: str = ""
    data: dict[str, Any] = field(default_factory=lambda: cast("dict[str, Any]", {}))

    @property
    def ok(self) -> bool:
        return self.code == 0


@runtime_checkable
class FeishuClient(Protocol):
    """Adapter-internal client surface; production wraps lark-oapi,
    tests use `FakeFeishuClient`."""

    # ── lifecycle ────────────────────────────────────────────────
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # ── outbound: text (post) ────────────────────────────────────
    async def create_text_message(
        self,
        *,
        chat_id: str,
        post_content: dict[str, Any],
        reply_to_message_id: str | None = None,
    ) -> FeishuResponse:
        """Send `msg_type=post` with the given post envelope. On
        success, `data["message_id"]` carries the new om_xxx id."""
        ...

    async def patch_text_message(
        self,
        *,
        message_id: str,
        post_content: dict[str, Any],
    ) -> FeishuResponse: ...

    # ── outbound: cards (interactive) ────────────────────────────
    async def create_card_message(
        self,
        *,
        chat_id: str,
        card_content: dict[str, Any],
        reply_to_message_id: str | None = None,
    ) -> FeishuResponse:
        """Send `msg_type=interactive` with the given card JSON.
        Same return shape as `create_text_message`."""
        ...

    async def patch_card_message(
        self,
        *,
        message_id: str,
        card_content: dict[str, Any],
    ) -> FeishuResponse: ...

    # ── outbound: image upload ───────────────────────────────────
    async def upload_image(self, *, image_data: bytes) -> FeishuResponse:
        """Upload image bytes to Feishu's image store. On success
        `data["image_key"]` carries the `img_xxx` key referenceable
        from `tag: img` card elements. Used by `DocumentContent(
        as_image=True, ...)` sends — Feishu image messages can't
        carry buttons, so paige wraps the image inside an
        interactive card."""
        ...

    async def delete_message(self, *, message_id: str) -> FeishuResponse: ...

    # ── inbound media ────────────────────────────────────────────
    async def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
    ) -> bytes:
        """Fetch the bytes of an attachment.

        `resource_type` is Feishu's enum: `"image"` for image_key
        keys, `"file"` for file_key keys (audio + file message
        types both go through `"file"`).

        Raises on transport / API errors — the channel surfaces
        them to the caller (Outbox typically logs and drops).
        """
        ...

    # ── liveness ─────────────────────────────────────────────────
    async def get_chat(self, *, chat_id: str) -> FeishuResponse:
        """Probe — the channel maps response codes to chat-alive
        booleans."""
        ...

    # ── bot identity ─────────────────────────────────────────────
    async def get_bot_info(self) -> FeishuResponse:
        """Fetch the bot's own identity. On success
        `data["bot"]["open_id"]` carries the bot's open_id —
        FeishuChannel uses it to filter group messages that don't
        @-mention the bot. Requires Feishu scope `im:bot:readonly`."""
        ...

    # ── inbound ──────────────────────────────────────────────────
    def register_message_handler(self, handler: MessageHandler) -> None: ...
    def register_action_handler(self, handler: ActionHandler) -> None: ...


__all__ = [
    "ActionHandler",
    "FeishuClient",
    "FeishuResponse",
    "MessageHandler",
]
