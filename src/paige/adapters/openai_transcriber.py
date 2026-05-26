"""OpenAITranscriber — `paige.ports.transcriber.Transcriber` impl.

Calls OpenAI's `/audio/transcriptions` endpoint via httpx
multipart upload with model `gpt-4o-transcribe`. Feishu voice
arrives pre-transcribed and never reaches this adapter, so this
exists for any future backend that delivers raw audio bytes.

`base_url` lets you point at a self-hosted OpenAI-compatible
endpoint or the Anthropic-only flavor of the API. Default targets
OpenAI's prod host.

httpx is imported lazily inside `transcribe` so installs without
the `[voice]` extra don't crash at module-import time. Same pattern
as the screenshot renderer.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-transcribe"
DEFAULT_TIMEOUT_SEC = 30.0


class OpenAITranscriber:
    """Transcribe audio via OpenAI's audio API.

    One client is held for the adapter's lifetime; `aclose()`
    releases it. Construct lazily so the httpx dep stays optional.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAITranscriber requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def transcribe(self, audio: bytes, *, mime: str = "") -> str:
        if not audio:
            raise ValueError("transcribe: empty audio payload")
        client = self._get_client()
        url = f"{self._base_url}/audio/transcriptions"
        filename, content_type = _filename_for(mime)
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            files={"file": (filename, audio, content_type)},
            data={"model": self._model},
        )
        response.raise_for_status()
        body: Any = response.json()
        text = ""
        if isinstance(body, dict):
            raw = cast("dict[str, Any]", body).get("text")
            if isinstance(raw, str):
                text = raw.strip()
        if not text:
            raise ValueError("Empty transcription returned by API")
        return text

    async def aclose(self) -> None:
        client = self._client
        if client is not None and not client.is_closed:
            await client.aclose()
        self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        import httpx

        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client


def _filename_for(mime: str) -> tuple[str, str]:
    """Pick a filename + content-type the API will accept.

    OpenAI's docs accept ogg/mp3/m4a/wav/webm/flac. Feishu voice
    arrives pre-transcribed so this path isn't hit for the live
    backend. Default to .ogg / audio/ogg when the MIME is empty or
    unrecognized — the server uses the bytes, not the extension,
    but a plausible filename keeps the multipart form
    valid.
    """
    if not mime:
        return "voice.ogg", "audio/ogg"
    if mime.startswith("audio/"):
        ext = mime.split("/", 1)[1]
        # Strip any +codecs suffix.
        ext = ext.split(";", 1)[0].strip()
        return f"voice.{ext}", mime
    return "voice.ogg", "audio/ogg"


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SEC",
    "OpenAITranscriber",
]
