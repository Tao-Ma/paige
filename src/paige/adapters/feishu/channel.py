"""FeishuChannel — `Channel` adapter on top of a `FeishuClient`.

Send/edit covers `TextContent`, `CardContent`, and the image flavor
of `DocumentContent` (the only flavor paige produces today, via
`/screenshot`). Image messages can't carry buttons on Feishu, so
the channel uploads the bytes via `upload_image` and wraps the
resulting key in an interactive card with `img + action` elements.

The channel does NOT import lark-oapi directly. It works against
the `FeishuClient` Protocol; production binds `LarkClientWrapper`
and tests bind `FakeFeishuClient`. This keeps the Feishu specifics
(codes, retry, WS lifecycle) localized.

Threading: `Conversation.thread_id` carries the reply-chain
`root_id`. Outbound sends pass `reply_to_message_id=thread_id`
so messages anchor to the same chain — same as v1's
`reply_in_thread=True` pattern.

Inbound dispatch:
  - text messages whose body starts with `/<cmd>` route to
    `on_command(name)` if registered
  - everything else fans out to `on_inbound` handlers
  - non-text, non-command, non-action events are dropped

Probe maps Feishu's known "chat is dead" codes (230002 disbanded,
230003 bot-not-in-chat, 230020 chat-not-found) to False. Anything
else (network errors, transient backend issues) fails open
(returns True) so a hiccup doesn't nuke a live binding.

Action handling (cards): clicks fan out to registered handlers,
and edits to the clicked card during dispatch ride the click
response (the inline-refresh slot) so the swap is atomic.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from ...domain.card import ActionEvent
from ...domain.conversation import Anchor, Conversation
from ...domain.inbound import Attachment, AttachmentKind, Inbound
from ...domain.outbound import (
    CardContent,
    DocumentContent,
    Outbound,
    TextContent,
)
from ...ports.channel import (
    ActionHandler as PaigeActionHandler,
)
from ...ports.channel import (
    CommandHandler as PaigeCommandHandler,
)
from ...ports.channel import (
    InboundHandler as PaigeInboundHandler,
)
from .cards import image_card, to_card
from .client import FeishuClient, FeishuResponse
from .inbound import split_command, to_action_event, to_inbound
from .post import to_post

logger = logging.getLogger(__name__)

# Feishu codes that mean "this chat is gone." Mapped to probe=False.
_DEAD_CHAT_CODES: frozenset[int] = frozenset({230002, 230003, 230020})


@dataclass
class _InlineRefreshSlot:
    """Per-click context for the inline-card-refresh trick.

    `clicked_anchor` identifies the card the user just tapped.
    `replacement_card` is the new card JSON to return via
    P2CardActionTriggerResponse.card — populated by `edit()` when
    the edit target matches the clicked card.

    Lives in a ContextVar so concurrent click dispatch on different
    cards doesn't share state.
    """

    clicked_anchor: Anchor
    replacement_card: dict[str, Any] | None = None


_inline_refresh: ContextVar[_InlineRefreshSlot | None] = ContextVar(
    "_feishu_inline_refresh", default=None
)


class FeishuChannel:
    """`Channel` implementation backed by `FeishuClient`."""

    def __init__(self, client: FeishuClient, *, paige_group_id: str = "") -> None:
        self._client = client
        # Per-call collapse threshold lives on `Outbound` itself —
        # the Outbox stamps it from `CollapsePrefService` at enqueue
        # time, the channel reads it at render time. Removed the
        # constructor param so the channel stays stateless w.r.t.
        # per-(person, conversation) prefs.
        self._inbound_handlers: list[PaigeInboundHandler] = []
        self._command_handlers: dict[str, PaigeCommandHandler] = {}
        self._action_handlers: list[PaigeActionHandler] = []
        # Toast text the channel should return on the response of the
        # next click event. Keyed by ack_token so a slow handler
        # processing one click can't bleed its toast onto another.
        self._pending_acks: dict[str, str] = {}
        # The bot's own open_id, learned at start() via
        # `client.get_bot_info`. Group-chat mention filtering uses
        # it to drop messages that don't @-mention this bot. Empty
        # string means "couldn't resolve" — filter fails open.
        self._bot_open_id: str = ""
        # Operator-declared paige-dedicated group (PAIGE_FEISHU_GROUP_ID).
        # Inside this chat, the mention filter is bypassed — the
        # whole purpose of the group is bot interaction, so every
        # message routes through. Empty string = no dedicated group;
        # all groups remain mention-filtered.
        self._paige_group_id = paige_group_id
        # Hook our normalizing dispatchers into the client. Done at
        # construction so handler registration order doesn't matter.
        client.register_message_handler(self._dispatch_message)
        client.register_action_handler(self._dispatch_action)

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        await self._client.start()
        await self._fetch_bot_open_id()

    async def stop(self) -> None:
        await self._client.stop()

    async def _fetch_bot_open_id(self) -> None:
        """Learn self open_id at startup so group-chat mention
        filtering can run. Fails open with a warning when the bot
        lacks the `im:bot:readonly` scope (or any other reason
        get_bot_info returns nothing) — better to over-respond in
        groups than to go silent."""
        try:
            resp = await self._client.get_bot_info()
        except Exception as e:
            logger.warning(
                "Feishu get_bot_info failed; group mention filtering disabled: %s",
                e,
            )
            return
        if not resp.ok:
            logger.warning(
                "Feishu get_bot_info returned code=%d msg=%s; mention filtering disabled",
                resp.code,
                resp.msg,
            )
            return
        bot_raw = resp.data.get("bot")
        if isinstance(bot_raw, dict):
            bot = cast("dict[str, Any]", bot_raw)
            self._bot_open_id = str(bot.get("open_id", "") or "")
        if self._bot_open_id:
            logger.info("Feishu bot open_id=%s", self._bot_open_id)
        else:
            logger.warning("Feishu get_bot_info returned no open_id; mention filtering disabled")

    # ── outbound ─────────────────────────────────────────────────

    async def send(self, outbound: Outbound) -> Anchor | None:
        if isinstance(outbound.content, TextContent):
            return await self._send_text(outbound, outbound.content.text)
        if isinstance(outbound.content, CardContent):
            return await self._send_card(outbound, outbound.content)
        if isinstance(outbound.content, DocumentContent):
            return await self._send_document(outbound, outbound.content)
        # TypingContent — Feishu has no typing-indicator API; drop
        # silently. Caller (Outbox) treats None as "fire-and-forget."
        return None

    async def edit(self, anchor: Anchor, outbound: Outbound) -> Anchor | None:
        if isinstance(outbound.content, TextContent):
            response = await self._client.patch_text_message(
                message_id=anchor.message_id,
                post_content=to_post(outbound.content.text),
            )
            if not response.ok:
                logger.debug(
                    "Feishu patch failed code=%d msg=%s",
                    response.code,
                    response.msg,
                )
            return None
        if isinstance(outbound.content, CardContent):
            card_json = to_card(
                outbound.content.card,
                thread_id=outbound.conversation.thread_id,
                topic_id=outbound.conversation.topic_id,
                collapse_threshold_lines=outbound.collapse_threshold_lines,
            )
            # Inline-refresh: if we're inside a click handler and the
            # user is editing the very card they just tapped, write
            # the new card into the click response instead of
            # PATCHing. Feishu repaints atomically with the click ack
            # — patch's repaint behavior on the clicker is unreliable.
            slot = _inline_refresh.get()
            if slot is not None and slot.clicked_anchor.message_id == anchor.message_id:
                slot.replacement_card = card_json
                return None
            response = await self._client.patch_card_message(
                message_id=anchor.message_id,
                card_content=card_json,
            )
            if not response.ok:
                logger.debug(
                    "Feishu card patch failed code=%d msg=%s",
                    response.code,
                    response.msg,
                )
            return None
        if isinstance(outbound.content, DocumentContent):
            card_json = await self._document_card_json(
                outbound.content,
                outbound.conversation.thread_id,
                outbound.conversation.topic_id,
            )
            if card_json is None:
                return None
            slot = _inline_refresh.get()
            if slot is not None and slot.clicked_anchor.message_id == anchor.message_id:
                slot.replacement_card = card_json
                return None
            response = await self._client.patch_card_message(
                message_id=anchor.message_id,
                card_content=card_json,
            )
            if not response.ok:
                logger.debug(
                    "Feishu document edit failed code=%d msg=%s",
                    response.code,
                    response.msg,
                )
            return None
        raise NotImplementedError(f"edit unsupported for {type(outbound.content).__name__}")

    async def delete(self, anchor: Anchor) -> None:
        response = await self._client.delete_message(message_id=anchor.message_id)
        if not response.ok:
            logger.debug(
                "Feishu delete failed code=%d msg=%s",
                response.code,
                response.msg,
            )

    # ── inbound media ────────────────────────────────────────────

    async def download(self, attachment: Attachment) -> bytes:
        """Fetch attachment bytes via the FeishuClient.

        Feishu's resource endpoint takes (message_id, file_key,
        type). `containing_message_id` carries the source message
        id paige's inbound parser stamped on the Attachment when
        it was emitted.
        """
        if not attachment.containing_message_id:
            raise ValueError("Feishu downloads require Attachment.containing_message_id")
        return await self._client.download_resource(
            message_id=attachment.containing_message_id,
            file_key=attachment.fetch_id,
            resource_type=_feishu_resource_type(attachment.kind),
        )

    # ── action handling ──────────────────────────────────────────

    async def ack(self, event: ActionEvent, text: str | None = None) -> None:
        """Stash a toast string keyed by ack_token. The dispatcher
        reads it back when assembling the click response so Feishu
        renders the toast on the tapper's screen.

        Feishu's card-action ack is request/response: the toast
        must ride home in the click response body. paige decouples
        this with a small per-token slot so application code can
        `await channel.ack(event, "Bound!")` synchronously inside
        the handler without knowing about Feishu's reply mechanism.
        """
        if not event.ack_token or text is None:
            return
        self._pending_acks[event.ack_token] = text

    # ── liveness ─────────────────────────────────────────────────

    async def probe(self, conversation: Conversation) -> bool:
        response = await self._client.get_chat(chat_id=conversation.chat_id)
        if response.ok:
            return True
        if response.code in _DEAD_CHAT_CODES:
            return False
        # Unknown error — assume the chat is alive so a transient
        # backend hiccup doesn't kill bindings.
        logger.debug(
            "Feishu probe non-zero code=%d msg=%s — failing open",
            response.code,
            response.msg,
        )
        return True

    # ── handler registration ─────────────────────────────────────

    def on_inbound(self, handler: PaigeInboundHandler) -> None:
        self._inbound_handlers.append(handler)

    def on_command(self, name: str, handler: PaigeCommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_action(self, handler: PaigeActionHandler) -> None:
        self._action_handlers.append(handler)

    async def dispatch_command(self, inbound: Inbound, name: str, arg: str) -> bool:
        handler = self._command_handlers.get(name)
        if handler is None:
            return False
        try:
            await handler(inbound, arg)
        except Exception as e:
            logger.exception("/%s synthetic dispatch failed: %s", name, e)
        return True

    # ── inbound dispatch ─────────────────────────────────────────

    async def _send_text(self, outbound: Outbound, text: str) -> Anchor | None:
        response = await self._client.create_text_message(
            chat_id=outbound.conversation.chat_id,
            post_content=to_post(text),
            reply_to_message_id=_thread_anchor(outbound),
        )
        return self._anchor_from_response(outbound, response, "send")

    async def _send_card(self, outbound: Outbound, content: CardContent) -> Anchor | None:
        response = await self._client.create_card_message(
            chat_id=outbound.conversation.chat_id,
            card_content=to_card(
                content.card,
                thread_id=outbound.conversation.thread_id,
                topic_id=outbound.conversation.topic_id,
                collapse_threshold_lines=outbound.collapse_threshold_lines,
            ),
            reply_to_message_id=_thread_anchor(outbound),
        )
        return self._anchor_from_response(outbound, response, "card send")

    async def _send_document(self, outbound: Outbound, content: DocumentContent) -> Anchor | None:
        card_json = await self._document_card_json(
            content, outbound.conversation.thread_id, outbound.conversation.topic_id
        )
        if card_json is None:
            return None
        response = await self._client.create_card_message(
            chat_id=outbound.conversation.chat_id,
            card_content=card_json,
            reply_to_message_id=_thread_anchor(outbound),
        )
        return self._anchor_from_response(outbound, response, "document send")

    async def _document_card_json(
        self,
        content: DocumentContent,
        thread_id: str | None,
        topic_id: str | None,
    ) -> dict[str, Any] | None:
        """Upload `content.data` to Feishu's image store and wrap
        the resulting image_key in an interactive card alongside any
        `content.rows` of buttons. Returns None on upload failure
        (caller logs + drops). Non-image (file) DocumentContent
        isn't yet supported on Feishu — paige's only DocumentContent
        producer today is `/screenshot`."""
        if not content.as_image:
            raise NotImplementedError(
                "Feishu file upload (DocumentContent as_image=False) not yet implemented"
            )
        upload = await self._client.upload_image(image_data=content.data)
        if not upload.ok:
            logger.warning(
                "Feishu image upload failed code=%d msg=%s",
                upload.code,
                upload.msg,
            )
            return None
        image_key = str(upload.data.get("image_key", ""))
        if not image_key:
            logger.warning("Feishu image upload returned no image_key")
            return None
        return image_card(
            image_key=image_key,
            rows=content.rows,
            thread_id=thread_id,
            topic_id=topic_id,
            alt=content.filename,
        )

    @staticmethod
    def _anchor_from_response(
        outbound: Outbound,
        response: FeishuResponse,
        what: str,
    ) -> Anchor | None:
        if not response.ok:
            logger.warning(
                "Feishu %s failed code=%d msg=%s",
                what,
                response.code,
                response.msg,
            )
            return None
        message_id = str(response.data.get("message_id", ""))
        if not message_id:
            return None
        return Anchor(
            conversation=outbound.conversation,
            message_id=message_id,
        )

    def _should_drop_group_message(self, event: dict[str, Any]) -> bool:
        """Drop group-chat messages that don't @-mention this bot.
        p2p (DM) messages always pass through. The operator-declared
        paige group (PAIGE_FEISHU_GROUP_ID) also passes through
        unfiltered — it exists for bot interaction, so every message
        is a candidate. Fails open when our own open_id is unknown.
        """
        if not self._bot_open_id:
            return False
        msg_raw = event.get("message")
        if not isinstance(msg_raw, dict):
            return False
        msg = cast("dict[str, Any]", msg_raw)
        if msg.get("chat_type") != "group":
            return False
        chat_id = str(msg.get("chat_id", ""))
        if self._paige_group_id and chat_id == self._paige_group_id:
            return False
        mentions_raw: Any = msg.get("mentions") or []
        if not isinstance(mentions_raw, list):
            return False
        mentions = cast("list[Any]", mentions_raw)
        if self._bot_open_id in mentions:
            return False
        logger.info(
            "feishu inbound dropped — group msg without bot mention (chat_id=%s)",
            chat_id,
        )
        return True

    async def _dispatch_message(self, event: dict[str, Any]) -> None:
        """Single normalizing entry: convert dict → Inbound, then
        route to the matching paige handler."""
        if self._should_drop_group_message(event):
            return
        inbound = to_inbound(event)
        if inbound is None:
            logger.info("feishu inbound dropped (to_inbound returned None)")
            return
        # Temporary debug: trace text + thread_id + which routing
        # bucket fires. Helpful while live-tuning slash commands and
        # bind/unbind cases. Drop once /sessions + /help confirmed
        # working live.
        logger.info(
            "feishu inbound text=%r thread_id=%s msg_id=%s",
            inbound.text,
            inbound.conversation.thread_id,
            inbound.message_id,
        )
        cmd_split = split_command(inbound.text)
        if cmd_split is not None:
            name, arg = cmd_split
            handler = self._command_handlers.get(name)
            if handler is None:
                logger.warning(
                    "feishu inbound /%s — no handler registered (known: %s)",
                    name,
                    sorted(self._command_handlers),
                )
                return
            logger.info("feishu inbound /%s arg=%r → handler", name, arg)
            try:
                await handler(inbound, arg)
            except Exception as e:
                logger.exception("/%s handler failed: %s", name, e)
            return
        for h in list(self._inbound_handlers):
            try:
                await h(inbound)
            except Exception as e:
                logger.exception("on_inbound handler failed: %s", e)

    async def _dispatch_action(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Convert a Feishu card-action trigger event to ActionEvent,
        fire registered handlers, then return the response body
        Feishu expects.

        Response shape (paige's normalized version, the production
        client wraps it into Feishu's full P2CardActionTriggerResponse
        envelope):

            {"toast": {"type": "info", "content": "..."}}

        The toast comes from `_pending_acks`, populated by
        `ack(event, text)` calls inside handlers. None when no ack
        was issued.

        Inline card refresh (`P2CardActionTriggerResponse.card`) is
        deferred — patch_card_message is reliable enough for non-
        clicker repaint, which covers the bulk of the UX.
        """
        ev = to_action_event(event)
        if ev is None:
            logger.warning("feishu action event dropped (to_action_event returned None)")
            return None
        # Temporary diagnostic: trace every click that arrives so we
        # can tell when a button silently fails to fire a callback
        # vs. when the handler chain does fire but produces no visible
        # effect. Drop once AskUserQuestion live-test settles.
        logger.info(
            "feishu action %s value=%s thread_id=%s anchor=%s",
            ev.action_id,
            ev.value,
            ev.conversation.thread_id,
            ev.card_anchor.message_id,
        )

        # Set the inline-refresh slot for the duration of handler
        # dispatch. Any channel.edit() targeting ev.card_anchor
        # during this window writes its card JSON to the slot
        # instead of PATCHing.
        slot = _InlineRefreshSlot(clicked_anchor=ev.card_anchor)
        token = _inline_refresh.set(slot)
        try:
            for h in list(self._action_handlers):
                try:
                    await h(ev)
                except Exception as e:
                    logger.exception("on_action handler failed: %s", e)
        finally:
            _inline_refresh.reset(token)

        toast = self._pending_acks.pop(ev.ack_token, None)
        return _build_action_response(toast, slot.replacement_card)


def _build_action_response(toast: str | None, card: dict[str, Any] | None) -> dict[str, Any] | None:
    """Combine optional toast + optional card-refresh into the
    response shape Feishu's click trigger expects. Returns None
    when neither is set so the dispatcher sends an empty 200.

    Feishu's `card` field on the click response wraps the new card
    JSON: `{"type": "raw", "data": <card_json>}`. Without the
    wrapper lark-oapi's `CallBackCard` silently drops the bare card
    payload (its only known fields are `type` and `data`) — the
    toast still fires, but no repaint happens. So every inline-
    refresh path needs the wrapper, image card or otherwise.
    """
    out: dict[str, Any] = {}
    if toast is not None:
        out["toast"] = {"type": "info", "content": toast}
    if card is not None:
        out["card"] = {"type": "raw", "data": card}
    return out or None


def _feishu_resource_type(kind: AttachmentKind) -> str:
    """Map paige's AttachmentKind onto Feishu's resource type enum.

    Feishu's `im.v1.message.resource.get` accepts only `image` for
    image_key keys; everything else (audio, voice, file) goes
    through `file`. Unknown kinds default to `file` defensively.
    """
    return "image" if kind is AttachmentKind.IMAGE else "file"


def _thread_anchor(outbound: Outbound) -> str | None:
    """Outbound + thread → `reply_to_message_id` to keep the message
    in the same Feishu reply chain. If `reply_to` is set explicitly
    (e.g. quoting a specific message), prefer that; otherwise fall
    back to the conversation's thread_id (the chain root)."""
    if outbound.reply_to is not None:
        return outbound.reply_to.message_id
    return outbound.conversation.thread_id


__all__ = ["FeishuChannel"]
