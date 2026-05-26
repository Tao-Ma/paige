"""FakeFeishuClient — observable in-memory FeishuClient for tests.

Records every outbound call (`created` / `patched` / `deleted` /
`probed`) so tests assert on the wire shape. Inbound is pushed
via `deliver_message` / `deliver_action`, which invoke the
registered handlers and return synchronously — fits the same
test pattern FakeChannel uses for the IM-side fakes.

Per-method response injection (`code_next`, `data_next`,
`fail_next`) lets a test simulate API errors without contortion.
The default response is `code=0` with a fresh sequential
`message_id`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..adapters.feishu.client import (
    ActionHandler,
    FeishuResponse,
    MessageHandler,
)


@dataclass
class _CreateCall:
    chat_id: str
    post_content: dict[str, Any]
    reply_to_message_id: str | None


@dataclass
class _PatchCall:
    message_id: str
    post_content: dict[str, Any]


@dataclass
class _CreateCardCall:
    chat_id: str
    card_content: dict[str, Any]
    reply_to_message_id: str | None


@dataclass
class _PatchCardCall:
    message_id: str
    card_content: dict[str, Any]


@dataclass
class _DeleteCall:
    message_id: str


@dataclass
class _ProbeCall:
    chat_id: str


@dataclass
class _DownloadCall:
    message_id: str
    file_key: str
    resource_type: str


@dataclass
class _UploadImageCall:
    image_data: bytes


class FakeFeishuClient:
    """In-memory `FeishuClient` for tests.

    Recorded calls live on instance attributes (`created`,
    `patched`, `deleted`, `probed`). Per-method response injection
    via `code_next` / `data_next` / `fail_next` dicts; consumed
    on the next call to that method.
    """

    def __init__(self) -> None:
        self.started: bool = False
        self.stopped: bool = False
        self.created: list[_CreateCall] = []
        self.patched: list[_PatchCall] = []
        self.created_cards: list[_CreateCardCall] = []
        self.patched_cards: list[_PatchCardCall] = []
        self.deleted: list[_DeleteCall] = []
        self.probed: list[_ProbeCall] = []
        self.downloaded: list[_DownloadCall] = []
        self.uploaded_images: list[_UploadImageCall] = []
        # file_key → bytes returned on download. Tests seed.
        self.download_bytes_next: dict[str, bytes] = {}
        # Per-method one-shot response injection.
        self.code_next: dict[str, int] = {}
        self.data_next: dict[str, dict[str, Any]] = {}
        self.fail_next: dict[str, Exception] = {}
        # Pre-marked probe responses keyed by chat_id.
        self.dead_chats: dict[str, int] = {}
        # Bot identity for get_bot_info() responses.
        self.bot_open_id_next: str = ""
        self.bot_info_calls: int = 0
        self._next_msg_id: int = 0
        self._next_image_id: int = 0
        self._msg_handlers: list[MessageHandler] = []
        self._action_handlers: list[ActionHandler] = []

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    # ── outbound ─────────────────────────────────────────────────

    async def create_text_message(
        self,
        *,
        chat_id: str,
        post_content: dict[str, Any],
        reply_to_message_id: str | None = None,
    ) -> FeishuResponse:
        self.created.append(
            _CreateCall(
                chat_id=chat_id,
                post_content=dict(post_content),
                reply_to_message_id=reply_to_message_id,
            )
        )
        return self._respond(
            "create_text_message",
            default_data=lambda: {"message_id": self._next_message_id()},
        )

    async def patch_text_message(
        self,
        *,
        message_id: str,
        post_content: dict[str, Any],
    ) -> FeishuResponse:
        self.patched.append(_PatchCall(message_id=message_id, post_content=dict(post_content)))
        return self._respond("patch_text_message")

    async def create_card_message(
        self,
        *,
        chat_id: str,
        card_content: dict[str, Any],
        reply_to_message_id: str | None = None,
    ) -> FeishuResponse:
        self.created_cards.append(
            _CreateCardCall(
                chat_id=chat_id,
                card_content=dict(card_content),
                reply_to_message_id=reply_to_message_id,
            )
        )
        return self._respond(
            "create_card_message",
            default_data=lambda: {"message_id": self._next_message_id()},
        )

    async def patch_card_message(
        self,
        *,
        message_id: str,
        card_content: dict[str, Any],
    ) -> FeishuResponse:
        self.patched_cards.append(
            _PatchCardCall(message_id=message_id, card_content=dict(card_content))
        )
        return self._respond("patch_card_message")

    async def delete_message(self, *, message_id: str) -> FeishuResponse:
        self.deleted.append(_DeleteCall(message_id=message_id))
        return self._respond("delete_message")

    async def get_chat(self, *, chat_id: str) -> FeishuResponse:
        self.probed.append(_ProbeCall(chat_id=chat_id))
        if chat_id in self.dead_chats:
            return FeishuResponse(code=self.dead_chats[chat_id])
        return self._respond("get_chat")

    async def get_bot_info(self) -> FeishuResponse:
        """Defaults to returning `bot_open_id_next` (when seeded) or
        an empty bot dict. Tests that exercise the mention-filter
        path seed `bot_open_id_next = "ou_paige_bot"` and read out
        the `get_bot_info` count via `bot_info_calls`."""
        self.bot_info_calls += 1
        if "get_bot_info" in self.fail_next:
            raise self.fail_next.pop("get_bot_info")
        if "get_bot_info" in self.code_next:
            return FeishuResponse(code=self.code_next.pop("get_bot_info"))
        bot_open_id = self.bot_open_id_next
        return FeishuResponse(code=0, data={"bot": {"open_id": bot_open_id} if bot_open_id else {}})

    async def upload_image(self, *, image_data: bytes) -> FeishuResponse:
        self.uploaded_images.append(_UploadImageCall(image_data=image_data))
        return self._respond(
            "upload_image",
            default_data=lambda: {"image_key": self._next_image_key()},
        )

    async def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
    ) -> bytes:
        self.downloaded.append(
            _DownloadCall(
                message_id=message_id,
                file_key=file_key,
                resource_type=resource_type,
            )
        )
        if "download_resource" in self.fail_next:
            raise self.fail_next.pop("download_resource")
        return self.download_bytes_next.pop(file_key, b"")

    # ── inbound registration ─────────────────────────────────────

    def register_message_handler(self, handler: MessageHandler) -> None:
        self._msg_handlers.append(handler)

    def register_action_handler(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    # ── seed helpers (test-only) ────────────────────────────────

    async def deliver_message(self, event: dict[str, Any]) -> None:
        """Push a synthesized message event to all registered
        handlers; await all of them before returning so test
        assertions can run synchronously after delivery."""
        for h in list(self._msg_handlers):
            await h(event)

    async def deliver_action(self, event: dict[str, Any]) -> list[dict[str, Any] | None]:
        """Push an action event; collect each handler's return
        (used in 15c for the inline-card-refresh response shape)."""
        out: list[dict[str, Any] | None] = []
        for h in list(self._action_handlers):
            out.append(await h(event))
        return out

    # ── internals ────────────────────────────────────────────────

    def _next_message_id(self) -> str:
        self._next_msg_id += 1
        return f"om_fake_{self._next_msg_id}"

    def _next_image_key(self) -> str:
        self._next_image_id += 1
        return f"img_fake_{self._next_image_id}"

    def _respond(
        self,
        method: str,
        *,
        default_data: Callable[[], dict[str, Any]] | None = None,
    ) -> FeishuResponse:
        if method in self.fail_next:
            raise self.fail_next.pop(method)
        code = self.code_next.pop(method, 0)
        if method in self.data_next:
            data = self.data_next.pop(method)
        elif default_data is not None and code == 0:
            data = default_data()
        else:
            data = {}
        return FeishuResponse(code=code, msg="", data=data)


__all__ = ["FakeFeishuClient"]
