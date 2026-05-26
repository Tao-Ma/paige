# pyright: reportMissingTypeStubs=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownLambdaType=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportAttributeAccessIssue=false
"""LarkClientWrapper — production `FeishuClient` over lark-oapi.

The shape paige's channel works against is `FeishuClient`
(client.py). This module wires that surface to the real
`lark-oapi` SDK:

  - outbound calls go through `lark.Client.im.v1.message.*` async
    methods, wrapped in a small retry loop
  - inbound WS bridges `lark.ws.Client.start()` (sync, blocks) via
    `asyncio.to_thread`, normalizes lark's typed events into the
    plain dicts paige's `inbound.py` consumes, and forwards to
    registered handlers via `run_coroutine_threadsafe`

`lark-oapi` is imported lazily inside method bodies so importing
this module without the `[feishu]` extra installed doesn't crash —
production deploys that pick `IM_BACKEND=feishu` need the extra;
tests use `FakeFeishuClient` and never touch this module.

Live behavior is the test: there are no unit tests against a
mocked lark-oapi (the type tree is too deep to mock faithfully).
The structural contract — that LarkClientWrapper satisfies the
FeishuClient Protocol — is checked at runtime.

Retry policy:
  - 3 attempts with 0.5 / 1.0 / 2.0 s exponential backoff on
    `(ConnectionError, TimeoutError, OSError)`
  - 1 extra retry on Feishu code 99991663 (stale tenant token —
    lark-oapi refreshes its cached token on the next call)
  - everything else propagates immediately (and the channel logs +
    drops, mapping non-zero codes to FeishuResponse semantics)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ...infrastructure.token_bucket import TokenBucket
from .client import ActionHandler, FeishuResponse, MessageHandler

logger = logging.getLogger(__name__)

_RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)
_FEISHU_ERR_INVALID_TENANT_TOKEN = 99991663
_RETRY_DELAYS_SEC = (0.5, 1.0, 2.0)
# Feishu's app-wide outbound limit. The bucket starts full so a
# bot that's been idle can fire a burst without waiting.
_DEFAULT_RATE_PER_SEC = 50.0
_DEFAULT_BURST = 50.0


class LarkClientWrapper:
    """Production FeishuClient backed by lark-oapi.

    Constructed via `LarkClientWrapper.build(app_id, app_secret,
    domain=None)` — domain defaults to https://open.feishu.cn (CN);
    pass https://open.larksuite.com for international.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        domain: str | None = None,
        rate_per_sec: float = _DEFAULT_RATE_PER_SEC,
        burst: float = _DEFAULT_BURST,
    ) -> None:
        # Defer SDK import so just `import paige.adapters.feishu...`
        # without [feishu] installed doesn't crash. main.py only
        # constructs this when IM_BACKEND=feishu.
        import lark_oapi as lark

        builder = lark.Client.builder().app_id(app_id).app_secret(app_secret)
        if domain:
            builder = builder.domain(domain)
        self._client = builder.build()
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain

        # Each retry attempt spends one token; retries can't bust
        # Feishu's app-wide cap.
        self._rate_limiter = TokenBucket(rate_per_sec, burst)

        self._msg_handlers: list[MessageHandler] = []
        self._action_handlers: list[ActionHandler] = []
        # WS bookkeeping — set on start().
        self._ws_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: Any | None = None  # lark.ws.Client

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the WS connection. lark.ws.Client.start() is sync
        and blocks forever; we run it in an asyncio.to_thread worker
        so the main loop stays responsive."""
        if self._ws_task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._ws_client = self._build_ws_client()
        self._ws_task = asyncio.create_task(
            asyncio.to_thread(self._ws_client.start),
            name="lark-ws",
        )

    async def stop(self) -> None:
        """Stop the WS. lark exposes no public stop(); we reach into
        its internal asyncio loop and orchestrate a clean teardown:
        cancel every pending task (the receive loop, the ping loop,
        the websockets keepalive), give the cancellations a beat to
        propagate so SSL / WS protocols flush their close frames,
        then stop the loop so the start() thread's
        `run_until_complete` returns.

        Without the cancel-first step, the start() thread exits with
        three tasks still pending and asyncio dumps `Task was
        destroyed but it is pending!` warnings plus a `Fatal error
        on SSL protocol` traceback on every `prod.sh restart`."""
        if self._ws_task is None:
            return
        try:
            import lark_oapi.ws.client as lark_ws_module

            ws_loop = getattr(lark_ws_module, "loop", None)
            if ws_loop is not None and ws_loop.is_running():
                # Drain coroutine runs on the WS loop's own thread;
                # `run_coroutine_threadsafe` returns a
                # `concurrent.futures.Future` we wait on with
                # `asyncio.wrap_future` so the main loop stays
                # responsive while the WS thread tears down.
                drain_fut = asyncio.run_coroutine_threadsafe(_drain_async(ws_loop), ws_loop)
                try:
                    await asyncio.wait_for(asyncio.wrap_future(drain_fut), timeout=3.0)
                except (asyncio.CancelledError, TimeoutError):
                    drain_fut.cancel()
                except Exception as e:
                    logger.debug("WS drain raised: %s", e)
        except Exception as e:
            logger.debug("WS stop hook failed: %s", e)
        try:
            await asyncio.wait_for(self._ws_task, timeout=5.0)
        except (asyncio.CancelledError, TimeoutError):
            self._ws_task.cancel()
        except Exception as e:
            logger.debug("WS task exit raised: %s", e)
        finally:
            self._ws_task = None
            self._ws_client = None

    # ── outbound: text (post) ───────────────────────────────────

    async def create_text_message(
        self,
        *,
        chat_id: str,
        post_content: dict[str, Any],
        reply_to_message_id: str | None = None,
    ) -> FeishuResponse:
        return await self._send_message(
            chat_id=chat_id,
            msg_type="post",
            content_json=json.dumps(post_content, ensure_ascii=False),
            reply_to_message_id=reply_to_message_id,
        )

    async def patch_text_message(
        self,
        *,
        message_id: str,
        post_content: dict[str, Any],
    ) -> FeishuResponse:
        return await self._patch_message(
            message_id=message_id,
            content_json=json.dumps(post_content, ensure_ascii=False),
        )

    # ── outbound: cards (interactive) ───────────────────────────

    async def create_card_message(
        self,
        *,
        chat_id: str,
        card_content: dict[str, Any],
        reply_to_message_id: str | None = None,
    ) -> FeishuResponse:
        return await self._send_message(
            chat_id=chat_id,
            msg_type="interactive",
            content_json=json.dumps(card_content, ensure_ascii=False),
            reply_to_message_id=reply_to_message_id,
        )

    async def patch_card_message(
        self,
        *,
        message_id: str,
        card_content: dict[str, Any],
    ) -> FeishuResponse:
        return await self._patch_message(
            message_id=message_id,
            content_json=json.dumps(card_content, ensure_ascii=False),
        )

    # ── outbound: image upload ──────────────────────────────────

    async def upload_image(self, *, image_data: bytes) -> FeishuResponse:
        import io as _io

        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
        )

        # Feishu's CreateImageRequest takes a file-like under
        # `image`. The `image_type="message"` enum scopes the upload
        # to message context (vs. avatar).
        req = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder()
                .image_type("message")
                .image(_io.BytesIO(image_data))
                .build()
            )
            .build()
        )
        return await self._call(
            "upload_image",
            lambda: self._client.im.v1.image.acreate(req),
        )

    # ── outbound: delete + probe ────────────────────────────────

    async def delete_message(self, *, message_id: str) -> FeishuResponse:
        from lark_oapi.api.im.v1 import DeleteMessageRequest

        req = DeleteMessageRequest.builder().message_id(message_id).build()
        return await self._call("delete_message", lambda: self._client.im.v1.message.adelete(req))

    async def get_chat(self, *, chat_id: str) -> FeishuResponse:
        from lark_oapi.api.im.v1 import GetChatRequest

        req = GetChatRequest.builder().chat_id(chat_id).build()
        return await self._call("get_chat", lambda: self._client.im.v1.chat.aget(req))

    async def get_bot_info(self) -> FeishuResponse:
        """Calls `/open-apis/bot/v3/info` via the low-level request
        path — lark-oapi's Python SDK doesn't expose it as a typed
        service call. Returns a FeishuResponse where
        `data["bot"]["open_id"]` carries the bot's identity when the
        scope `im:bot:readonly` is granted."""
        import lark_oapi as lark

        req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        # `arequest()` returns a `BaseResponse` whose HTTP body lives
        # on `.raw.content`, not on its typed `.data`. The bot-info
        # endpoint puts `code`/`msg`/`bot.open_id` at the top level
        # of the JSON body — parse + repackage as a FeishuResponse so
        # callers see the same shape as every other client method.
        base = await self._retry_transient(
            "get_bot_info",
            lambda: self._client.arequest(req),
        )
        body_bytes = b""
        inner_raw = getattr(base, "raw", None)
        if inner_raw is not None:
            body_bytes = getattr(inner_raw, "content", b"") or b""
        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
        except (TypeError, ValueError, AttributeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return FeishuResponse(
            code=int(parsed.get("code", 0)),
            msg=str(parsed.get("msg", "")),
            data=parsed,
        )

    async def download_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
    ) -> bytes:
        """Fetch attachment bytes via im.v1.message.resource.get.

        Returns the raw response body. Non-zero codes raise so the
        caller can decide what to do (the channel surfaces failure
        to the application). lark-oapi's `aget` returns a typed
        response with `.file` (a stream) on success.
        """
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        native = await self._retry_transient(
            "download_resource",
            lambda: self._client.im.v1.message.resource.aget(req),
        )
        code = getattr(native, "code", 0) or 0
        if code != 0:
            raise RuntimeError(
                f"download_resource failed code={code} msg={getattr(native, 'msg', '')!r}"
            )
        # lark wraps the stream in `.file` (a BytesIO-like). Read all.
        file_obj = getattr(native, "file", None)
        if file_obj is None:
            return b""
        try:
            return file_obj.read()
        except AttributeError:
            return bytes(file_obj)

    # ── inbound registration ────────────────────────────────────

    def register_message_handler(self, handler: MessageHandler) -> None:
        self._msg_handlers.append(handler)

    def register_action_handler(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    # ── internals ───────────────────────────────────────────────

    async def _send_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        content_json: str,
        reply_to_message_id: str | None,
    ) -> FeishuResponse:
        """Dispatch between im.v1.message.acreate (top-level) and
        im.v1.message.areply (in-thread). Reply_to_message_id !=
        None means we want the message to land inside that
        message's reply chain."""
        if reply_to_message_id is None:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            req = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type(msg_type)
                    .content(content_json)
                    .build()
                )
                .build()
            )
            return await self._call(
                "create_message",
                lambda: self._client.im.v1.message.acreate(req),
            )

        from lark_oapi.api.im.v1 import (
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        req = (
            ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content_json)
                .reply_in_thread(True)
                .build()
            )
            .build()
        )
        return await self._call("reply_message", lambda: self._client.im.v1.message.areply(req))

    async def _patch_message(
        self,
        *,
        message_id: str,
        content_json: str,
    ) -> FeishuResponse:
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(content_json).build())
            .build()
        )
        return await self._call("patch_message", lambda: self._client.im.v1.message.apatch(req))

    async def _call(self, op_name: str, attempt_fn: Any) -> FeishuResponse:
        """Run an async lark API call with retry policy + Feishu
        response normalization.

        Two retry layers:
          - transient exceptions: 3 attempts with backoff
          - stale-token (99991663): one extra attempt
        """
        native = await self._retry_transient(op_name, attempt_fn)
        code = getattr(native, "code", 0) or 0
        if code == _FEISHU_ERR_INVALID_TENANT_TOKEN:
            logger.debug("%s: stale tenant token (99991663); retrying once", op_name)
            native = await self._retry_transient(op_name, attempt_fn)
        return _to_feishu_response(native)

    async def _retry_transient(self, op_name: str, attempt_fn: Any) -> Any:
        """3-attempt retry on transient connection errors. Non-
        retryable errors propagate. Each attempt — including
        retries — acquires one token from the rate limiter so a
        retry storm can't exceed Feishu's app-wide cap."""
        last_exc: BaseException | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS_SEC):
            await self._rate_limiter.acquire()
            try:
                return await attempt_fn()
            except _RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt == len(_RETRY_DELAYS_SEC) - 1:
                    raise
                logger.debug(
                    "%s attempt %d: %s; retrying in %.1fs",
                    op_name,
                    attempt + 1,
                    type(e).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
        # Defensive — loop always returns or raises.
        if last_exc is not None:  # pragma: no cover
            raise last_exc
        raise RuntimeError("unreachable")

    # ── WS handler bridge ───────────────────────────────────────

    def _build_ws_client(self) -> Any:
        """Construct a lark.ws.Client wired to dispatch
        `im.message.receive_v1` and `card.action.trigger_v1` events
        into our normalized handlers.

        Lark's WS callbacks run in a worker thread, so we hop back
        to the asyncio loop via `run_coroutine_threadsafe` to fire
        the registered async handlers.

        `lark_oapi.ws.client` binds a module-level event loop at
        import time via `asyncio.get_event_loop()`. paige imports
        lark lazily — inside a coroutine, with the main asyncio
        loop already running — so `get_event_loop()` returns *our*
        main loop. Then lark's `Client.start()` later calls
        `loop.run_until_complete(_select())` on it from a worker
        thread, which raises "This event loop is already running"
        because the main thread is still driving that loop.

        Fix: replace the module-level loop with a fresh,
        not-yet-running loop. Lark's `Client.start()` will own and
        drive it inside the asyncio.to_thread worker, completely
        decoupled from paige's main loop.
        """
        import lark_oapi as lark
        import lark_oapi.ws.client as lark_ws_module

        if lark_ws_module.loop is asyncio.get_event_loop():
            lark_ws_module.loop = asyncio.new_event_loop()

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_lark_message)
            .register_p2_card_action_trigger(self._on_lark_action)
            # Subscribed in the Feishu app config but not actionable for
            # paige; register no-ops so the SDK doesn't log every
            # occurrence at ERROR with "processor not found".
            .register_p2_im_message_message_read_v1(_noop_event_handler)
            .register_p2_im_message_recalled_v1(_noop_event_handler)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_noop_event_handler)
            .build()
        )
        return lark.ws.Client(self._app_id, self._app_secret, event_handler=dispatcher)

    def _on_lark_message(self, event: Any) -> None:
        """lark P2MessageReceiveV1 → normalized dict → registered
        async handlers. Lark calls this in a worker thread."""
        payload = _normalize_message_event(event)
        if payload is None:
            return
        self._dispatch_async(self._fan_out_message(payload))

    def _on_lark_action(self, event: Any) -> Any:
        """lark P2CardActionTriggerV1 → normalized dict → handlers.
        Returns the response object lark wants for the click reply.
        Synchronous from lark's POV — we use run_coroutine_threadsafe
        + .result() to wait for the async handler chain to produce
        the response."""
        payload = _normalize_action_event(event)
        if payload is None:
            logger.warning("lark card action normalization returned None")
            return None
        try:
            response_dict = self._dispatch_async_sync(self._fan_out_action(payload))
        except Exception as e:
            logger.exception("card action dispatch failed: %s", e)
            return None
        return _to_lark_action_response(response_dict)

    async def _fan_out_message(self, payload: dict[str, Any]) -> None:
        for h in list(self._msg_handlers):
            try:
                await h(payload)
            except Exception as e:
                logger.exception("message handler failed: %s", e)

    async def _fan_out_action(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        # Multiple handlers — first non-None response wins. (paige
        # currently registers exactly one action handler from the
        # FeishuChannel; this pattern leaves room for more.)
        out: dict[str, Any] | None = None
        for h in list(self._action_handlers):
            try:
                result = await h(payload)
                if result is not None and out is None:
                    out = result
            except Exception as e:
                logger.exception("action handler failed: %s", e)
        return out

    def _dispatch_async(self, coro: Any) -> None:
        """Schedule a coroutine on the main loop from the WS worker
        thread; fire-and-forget."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _dispatch_async_sync(self, coro: Any) -> dict[str, Any] | None:
        """Schedule + wait for the result. Used for action dispatch
        where lark expects a response."""
        if self._loop is None:
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=5.0)


async def _drain_async(ws_loop: asyncio.AbstractEventLoop) -> None:
    """Coroutine that runs on the lark WS thread's loop. Cancels
    every other pending task and awaits their termination so SSL
    close-frames and WebSocket close handshakes flush cleanly,
    then stops the loop so the start() thread's
    `run_until_complete` returns.

    The fixed-delay-then-stop variant (`call_later(0.1, stop)`)
    isn't reliable — SSL teardown can take 100s of ms when the
    connection is alive and writing, and stopping mid-flush leaves
    "Fatal error on SSL protocol" and "Task was destroyed but it
    is pending!" warnings in the log on every restart."""
    me = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks(loop=ws_loop) if t is not me and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        # CancelledError is the expected outcome; swallow everything
        # so a misbehaving task doesn't block the drain.
        await asyncio.gather(*tasks, return_exceptions=True)
    ws_loop.stop()


def _noop_event_handler(_event: Any) -> None:
    """Lark dispatcher handler for events paige subscribes to but
    intentionally ignores (e.g. message-read, chat-entered). Without
    a registered handler the SDK logs every occurrence at ERROR with
    "processor not found"; this drains that noise from the log."""
    return None


# ── lark → paige normalizers ────────────────────────────────────


def _normalize_message_event(event: Any) -> dict[str, Any] | None:
    """Convert a lark-oapi P2MessageReceiveV1 to the dict shape
    `paige.adapters.feishu.inbound.to_inbound` expects.

    Tolerant of missing fields — returns None if the core message
    structure isn't there.
    """
    inner = getattr(event, "event", None)
    if inner is None:
        return None
    msg = getattr(inner, "message", None)
    sender = getattr(inner, "sender", None)
    if msg is None or sender is None:
        return None

    sender_id = getattr(sender, "sender_id", None)
    open_id = getattr(sender_id, "open_id", "") if sender_id is not None else ""

    # Mentions: a list of typed objects each carrying an `id` with
    # open_id/user_id/union_id. Project to a flat list of open_ids
    # so the channel-side filter only has to do a membership check.
    mention_open_ids: list[str] = []
    raw_mentions = getattr(msg, "mentions", None) or []
    for m in raw_mentions:
        m_id = getattr(m, "id", None)
        m_open_id = getattr(m_id, "open_id", "") if m_id is not None else ""
        if m_open_id:
            mention_open_ids.append(m_open_id)

    return {
        "message": {
            "message_id": getattr(msg, "message_id", "") or "",
            "root_id": getattr(msg, "root_id", None),
            # Lark's topic id (`omt_xxx`) in topic-mode groups —
            # absent on the message object outside topic mode. The
            # inbound parser uses this (not `root_id`) to populate
            # `Conversation.topic_id`, so bindings can scope per topic.
            "thread_id": getattr(msg, "thread_id", None),
            "chat_id": getattr(msg, "chat_id", "") or "",
            "chat_type": getattr(msg, "chat_type", "") or "",
            "message_type": getattr(msg, "message_type", "") or "",
            "content": getattr(msg, "content", "") or "",
            "create_time": getattr(msg, "create_time", "") or "",
            "mentions": mention_open_ids,
        },
        "sender": {
            "sender_id": {"open_id": open_id or ""},
            "name": getattr(sender, "user_name", "") or "",
        },
    }


def _normalize_action_event(event: Any) -> dict[str, Any] | None:
    """Convert lark P2CardActionTriggerV1 to the dict shape
    `paige.adapters.feishu.inbound.to_action_event` expects."""
    inner = getattr(event, "event", None)
    if inner is None:
        return None
    operator = getattr(inner, "operator", None)
    action = getattr(inner, "action", None)
    context = getattr(inner, "context", None)
    if operator is None or action is None or context is None:
        return None

    open_id = getattr(operator, "open_id", "") or ""
    user_name = getattr(operator, "user_name", "") or ""
    raw_value = getattr(action, "value", None)
    value: dict[str, Any] = dict(raw_value) if isinstance(raw_value, dict) else {}

    # Element-specific fields beyond the static `value` payload.
    # `input_value` carries the user's typed text on `input` element
    # submissions (lark-oapi's CallBackAction model). `option` is
    # the older select-style equivalent we also try in the inbound
    # parser. We surface BOTH so `inbound.to_action_event` can lift
    # whichever the runtime delivered into `value['_input']`.
    action_payload: dict[str, Any] = {
        "tag": getattr(action, "tag", "button") or "button",
        "value": value,
    }
    input_text = getattr(action, "input_value", None)
    if isinstance(input_text, str):
        action_payload["input_value"] = input_text
    option_text = getattr(action, "option", None)
    if isinstance(option_text, str):
        action_payload["option"] = option_text

    return {
        "operator": {"open_id": open_id, "user_name": user_name},
        "action": action_payload,
        "context": {
            "open_message_id": getattr(context, "open_message_id", "") or "",
            "open_chat_id": getattr(context, "open_chat_id", "") or "",
            "thread_id": getattr(context, "thread_id", None),
        },
        "token": getattr(event, "token", "") or "",
    }


def _to_feishu_response(native: Any) -> FeishuResponse:
    """Pull `code`, `msg`, `data` off a lark-oapi typed response.
    Falls back to defaults if shape doesn't match (e.g. RawResponse)."""
    code = getattr(native, "code", 0) or 0
    msg = getattr(native, "msg", "") or ""
    data_obj = getattr(native, "data", None)
    if data_obj is None:
        return FeishuResponse(code=code, msg=msg, data={})
    # Lark typed responses' `.data` are class instances; serialize
    # via lark.JSON.marshal for a shallow dict.
    try:
        import lark_oapi as lark

        data_json = lark.JSON.marshal(data_obj) or "{}"
        data: dict[str, Any] = json.loads(data_json)
    except Exception:
        data = {}
    return FeishuResponse(code=code, msg=msg, data=data)


def _to_lark_action_response(
    response_dict: dict[str, Any] | None,
) -> Any:
    """Pass paige's response dict straight through. None means "no
    response body" — lark sends an empty 200.

    We deliberately do NOT wrap in `P2CardActionTriggerResponse`:
    its constructor walks the dict via `lark_oapi.core.construct.init`
    and only copies fields it recognizes, while `CallBackCard` knows
    only `type` and `data`. A bare card JSON has neither key, so the
    typed wrapper silently turned every `card` payload into
    `{type: None, data: None}` — toast survived, repaint didn't.
    `JSON.marshal` (used by the dispatcher) serializes plain dicts
    via the default encoder, so passing the dict through works.
    """
    return response_dict


__all__ = ["LarkClientWrapper"]
