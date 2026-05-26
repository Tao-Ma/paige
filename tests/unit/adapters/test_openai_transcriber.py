"""OpenAITranscriber — Transcriber port impl over httpx.

We don't hit the network; we monkeypatch `httpx.AsyncClient.post` to
record the request shape and return a synthetic response. The
adapter is the boundary — its behavior under valid / empty / error
responses is what we lock in.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from paige.adapters.openai_transcriber import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    OpenAITranscriber,
    _filename_for,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status
        self._body: dict[str, Any] = body if body is not None else {}

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=cast("httpx.Request", None),
                response=cast("httpx.Response", self),
            )


class _FakeClient:
    """Stub for httpx.AsyncClient — records the last call only."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.is_closed = False
        self.last_url: str = ""
        self.last_headers: dict[str, str] = {}
        self.last_files: Any = None
        self.last_data: dict[str, str] = {}
        self.posts: int = 0

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        files: Any,
        data: dict[str, str],
    ) -> _FakeResponse:
        self.posts += 1
        self.last_url = url
        self.last_headers = headers
        self.last_files = files
        self.last_data = data
        return self._response

    async def aclose(self) -> None:
        self.is_closed = True


def _patched(transcriber: OpenAITranscriber, response: _FakeResponse) -> _FakeClient:
    """Replace the lazily-initialized httpx client with a _FakeClient."""
    client = _FakeClient(response)
    # Bypass `_get_client` by setting the slot directly.
    transcriber._client = cast("httpx.AsyncClient", client)  # noqa: SLF001
    return client


# ── construction ─────────────────────────────────────────────────


def test_construct_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenAITranscriber("")


def test_default_base_url_and_model() -> None:
    t = OpenAITranscriber("k")
    assert t._base_url == DEFAULT_BASE_URL  # noqa: SLF001
    assert t._model == DEFAULT_MODEL  # noqa: SLF001


def test_custom_base_url_strips_trailing_slash() -> None:
    t = OpenAITranscriber("k", base_url="https://proxy.example/v1/")
    assert t._base_url == "https://proxy.example/v1"  # noqa: SLF001


# ── transcribe happy path ────────────────────────────────────────


async def test_transcribe_returns_text() -> None:
    t = OpenAITranscriber("k")
    fake = _patched(t, _FakeResponse(body={"text": "hello world"}))

    out = await t.transcribe(b"opus-bytes", mime="audio/ogg")

    assert out == "hello world"
    assert fake.posts == 1
    assert fake.last_url == f"{DEFAULT_BASE_URL}/audio/transcriptions"
    assert fake.last_headers == {"Authorization": "Bearer k"}
    assert fake.last_data == {"model": DEFAULT_MODEL}
    # Multipart form structure: ("filename", bytes, content_type)
    assert fake.last_files["file"][0].endswith(".ogg")
    assert fake.last_files["file"][1] == b"opus-bytes"
    assert fake.last_files["file"][2] == "audio/ogg"


async def test_transcribe_strips_whitespace() -> None:
    t = OpenAITranscriber("k")
    _patched(t, _FakeResponse(body={"text": "  hi there \n"}))

    assert await t.transcribe(b"x", mime="audio/ogg") == "hi there"


# ── failure paths ────────────────────────────────────────────────


async def test_transcribe_empty_audio_raises() -> None:
    t = OpenAITranscriber("k")
    with pytest.raises(ValueError, match="empty audio"):
        await t.transcribe(b"")


async def test_transcribe_empty_response_raises() -> None:
    t = OpenAITranscriber("k")
    _patched(t, _FakeResponse(body={"text": "   "}))
    with pytest.raises(ValueError, match="Empty transcription"):
        await t.transcribe(b"x")


async def test_transcribe_missing_text_field_raises() -> None:
    t = OpenAITranscriber("k")
    _patched(t, _FakeResponse(body={}))  # no "text" key
    with pytest.raises(ValueError, match="Empty transcription"):
        await t.transcribe(b"x")


async def test_transcribe_http_error_propagates() -> None:
    t = OpenAITranscriber("k")
    _patched(t, _FakeResponse(status=429, body={}))
    with pytest.raises(httpx.HTTPStatusError):
        await t.transcribe(b"x")


# ── filename mapping ─────────────────────────────────────────────


def test_filename_for_default() -> None:
    assert _filename_for("") == ("voice.ogg", "audio/ogg")


def test_filename_for_audio_mp3() -> None:
    assert _filename_for("audio/mp3") == ("voice.mp3", "audio/mp3")


def test_filename_for_audio_with_codecs_suffix() -> None:
    fn, mime = _filename_for("audio/ogg;codecs=opus")
    assert fn == "voice.ogg"
    assert mime == "audio/ogg;codecs=opus"


def test_filename_for_non_audio_falls_back_to_ogg() -> None:
    assert _filename_for("application/octet-stream") == ("voice.ogg", "audio/ogg")


# ── lifecycle ────────────────────────────────────────────────────


async def test_aclose_releases_client() -> None:
    t = OpenAITranscriber("k")
    fake = _patched(t, _FakeResponse(body={"text": "x"}))
    await t.aclose()
    assert fake.is_closed is True
    assert t._client is None  # noqa: SLF001


async def test_aclose_when_no_client_is_noop() -> None:
    t = OpenAITranscriber("k")
    # No transcribe call → no client built.
    await t.aclose()
    assert t._client is None  # noqa: SLF001
