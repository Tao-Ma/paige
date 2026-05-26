"""VoiceService — handle inbound audio attachments.

Plumbs the speech-to-text loop:

  1. Inbound arrives with an AUDIO attachment.
  2. If `inbound.text` is non-empty, the backend already
     transcribed (Feishu does this client-side) — silent skip;
     the regular text dispatcher handles it.
  3. Otherwise: ensure a binding, download the bytes via Channel,
     transcribe via Transcriber, send the text to the bound pane,
     echo `🎤 "<text>"` back to the user.

Design notes:
- VoiceService is OPTIONAL. When no Transcriber is available
  (no `OPENAI_API_KEY` set), no instance is built and audio messages
  get a one-line "voice transcription not configured" hint instead.
- Multiple `on_inbound` handlers are supported by Channel — Voice
  and Dispatcher run independently. Voice short-circuits on text;
  Dispatcher short-circuits on no-text. Disjoint paths.
- Failures echo to the user but never raise — the dispatcher's
  text path keeps working even if transcription is broken.
"""

from __future__ import annotations

import logging

from ..domain.inbound import Attachment, AttachmentKind, Inbound
from ..domain.outbound import Outbound, TextContent
from ..ports.channel import Channel
from ..ports.multiplexer import Multiplexer
from ..ports.transcriber import Transcriber
from .access import AllowList
from .outbox import Outbox
from .run_registry import RunRegistry

logger = logging.getLogger(__name__)

NOT_CONFIGURED_HINT = "Voice transcription isn't configured. Set `PAIGE_OPENAI_API_KEY` to enable."
UNBOUND_HINT = (
    "No session bound to this conversation. Use /sessions to pick one or /start a new one."
)
TRANSCRIBE_FAILED_TMPL = "⚠ Transcription failed: {error}"


class VoiceService:
    """Inbound audio → transcript → bound pane + echo."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        multiplexer: Multiplexer,
        outbox: Outbox,
        transcriber: Transcriber | None,
        allow_list: AllowList,
    ) -> None:
        self._registry = registry
        self._multiplexer = multiplexer
        self._outbox = outbox
        self._transcriber = transcriber
        self._allow_list = allow_list
        self._channel: Channel | None = None  # captured at install for download()

    def install(self, channel: Channel) -> None:
        self._channel = channel
        channel.on_inbound(self._allow_list.guard_inbound(self._handle))

    async def _handle(self, inbound: Inbound) -> None:
        # Skip when not relevant — keep both early-exits cheap so the
        # 99% text-only case isn't taxed.
        audio = _first_audio(inbound)
        if audio is None:
            return
        if inbound.text.strip():
            # Pre-transcribed by the backend — Dispatcher's text path
            # picks it up.
            return

        if self._transcriber is None:
            self._reply(inbound, NOT_CONFIGURED_HINT)
            return

        pane_id = self._registry.get_pane(inbound.sender, inbound.conversation)
        if pane_id is None:
            self._reply(inbound, UNBOUND_HINT)
            return

        if self._channel is None:
            logger.error("VoiceService not installed but received an inbound — drop")
            return

        try:
            payload = await self._channel.download(audio)
        except Exception as e:
            logger.warning("voice download failed: %s", e)
            self._reply(inbound, TRANSCRIBE_FAILED_TMPL.format(error=e))
            return

        try:
            text = await self._transcriber.transcribe(payload, mime=audio.mime_type)
        except Exception as e:
            logger.warning("voice transcribe failed: %s", e)
            self._reply(inbound, TRANSCRIBE_FAILED_TMPL.format(error=e))
            return

        ok = await self._multiplexer.send_keys(pane_id, text, enter=True, literal=True)
        if not ok:
            logger.warning("voice send_keys failed for pane %s", pane_id)
            return
        self._reply(inbound, f'🎤 "{text}"')

    def _reply(self, inbound: Inbound, text: str) -> None:
        self._outbox.enqueue_send(
            inbound.sender,
            Outbound(
                conversation=inbound.conversation,
                content=TextContent(text),
            ),
        )


def _first_audio(inbound: Inbound) -> Attachment | None:
    for att in inbound.attachments:
        if att.kind is AttachmentKind.AUDIO:
            return att
    return None


__all__ = [
    "NOT_CONFIGURED_HINT",
    "TRANSCRIBE_FAILED_TMPL",
    "UNBOUND_HINT",
    "VoiceService",
]
