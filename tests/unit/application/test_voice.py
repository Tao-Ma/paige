"""VoiceService — audio attachment → transcribe → forward + echo."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.application.voice import (
    NOT_CONFIGURED_HINT,
    TRANSCRIBE_FAILED_TMPL,
    UNBOUND_HINT,
    VoiceService,
)
from paige.domain.conversation import Conversation
from paige.domain.inbound import Attachment, AttachmentKind, Inbound
from paige.domain.outbound import TextContent
from paige.domain.person import Person
from paige.testing.fakes import FakeChannel, FakeMultiplexer, FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
CONV = Conversation(chat_id="-100", thread_id="42")


class FakeTranscriber:
    """Records calls; `next_text` controls the response."""

    def __init__(self, text: str = "transcribed text") -> None:
        self.next_text = text
        self.next_exc: Exception | None = None
        self.calls: list[tuple[bytes, str]] = []

    async def transcribe(self, audio: bytes, *, mime: str = "") -> str:
        self.calls.append((audio, mime))
        if self.next_exc is not None:
            raise self.next_exc
        return self.next_text


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    transcriber = FakeTranscriber()

    service = VoiceService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        transcriber=transcriber,
        allow_list=AllowList(),
    )
    service.install(channel)

    class Harness:
        pass

    h = Harness()
    h.channel = channel  # type: ignore[attr-defined]
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    h.transcriber = transcriber  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


def _audio_inbound(text: str = "", mime: str = "audio/ogg") -> Inbound:
    return Inbound(
        sender=ALICE,
        conversation=CONV,
        text=text,
        message_id="m1",
        attachments=(
            Attachment(
                kind=AttachmentKind.AUDIO,
                fetch_id="aud-1",
                mime_type=mime,
                duration_sec=2.5,
            ),
        ),
    )


def _image_inbound() -> Inbound:
    return Inbound(
        sender=ALICE,
        conversation=CONV,
        text="",
        message_id="m1",
        attachments=(Attachment(kind=AttachmentKind.IMAGE, fetch_id="img-1"),),
    )


# ── inert paths ──────────────────────────────────────────────────


async def test_no_attachment_does_nothing(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    plain = Inbound(sender=ALICE, conversation=CONV, text="hi", message_id="m")
    await h.channel.deliver_inbound(plain)
    await h.outbox.stop()
    assert h.channel.sent == []
    assert h.channel.downloaded == []
    assert h.transcriber.calls == []


async def test_image_attachment_does_nothing(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_inbound(_image_inbound())
    await h.outbox.stop()
    assert h.channel.sent == []
    assert h.transcriber.calls == []


async def test_pre_transcribed_text_does_nothing(harness) -> None:  # type: ignore[no-untyped-def]
    """Feishu emits voice with text already filled in by client-side
    transcription. VoiceService skips so Dispatcher's text path runs."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    await h.channel.deliver_inbound(_audio_inbound(text="hi from feishu"))
    await h.outbox.stop()

    assert h.transcriber.calls == []
    assert h.channel.downloaded == []
    # No echo from VoiceService — text dispatch will handle it.
    assert h.channel.sent == []


# ── unconfigured ─────────────────────────────────────────────────


async def test_no_transcriber_sends_hint() -> None:
    """When no transcriber is wired (e.g. PAIGE_OPENAI_API_KEY unset),
    audio inbounds get a one-line config hint."""
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)

    service = VoiceService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        transcriber=None,
        allow_list=AllowList(),
    )
    service.install(channel)
    try:
        await channel.deliver_inbound(_audio_inbound())
        await outbox.stop()

        [sent] = channel.sent
        assert isinstance(sent.content, TextContent)
        assert sent.content.text == NOT_CONFIGURED_HINT
        assert channel.downloaded == []
    finally:
        pass


# ── unbound ──────────────────────────────────────────────────────


async def test_unbound_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    await h.channel.deliver_inbound(_audio_inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == UNBOUND_HINT
    assert h.channel.downloaded == []
    assert h.transcriber.calls == []


# ── happy path ───────────────────────────────────────────────────


async def test_transcribes_and_forwards_to_pane(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.channel.download_data = b"opus-bytes"
    h.transcriber.next_text = "build the thing"

    await h.channel.deliver_inbound(_audio_inbound(mime="audio/ogg"))
    await h.outbox.stop()

    # Bytes downloaded once via the channel.
    [att] = h.channel.downloaded
    assert att.kind is AttachmentKind.AUDIO
    # Transcriber received the bytes + the MIME type.
    [(payload, mime)] = h.transcriber.calls
    assert payload == b"opus-bytes"
    assert mime == "audio/ogg"
    # Transcript forwarded to the pane.
    [send] = h.mux.send_keys_calls
    assert send.text == "build the thing"
    assert send.enter is True
    assert send.literal is True
    # Echo back to the user.
    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == '🎤 "build the thing"'


# ── failure paths ────────────────────────────────────────────────


async def test_download_error_echoes_failure(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")

    # Inject a download error via a custom FakeChannel subclass —
    # easier to monkeypatch the `download` coroutine directly.
    async def boom(_att: Attachment) -> bytes:
        raise RuntimeError("network gone")

    h.channel.download = boom  # type: ignore[method-assign]

    await h.channel.deliver_inbound(_audio_inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert "network gone" in sent.content.text
    assert sent.content.text.startswith("⚠")
    assert h.transcriber.calls == []
    assert h.mux.send_keys_calls == []


async def test_transcribe_error_echoes_failure(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV, "@1")
    h.channel.download_data = b"some bytes"
    h.transcriber.next_exc = ValueError("Empty transcription returned by API")

    await h.channel.deliver_inbound(_audio_inbound())
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, TextContent)
    assert sent.content.text == TRANSCRIBE_FAILED_TMPL.format(
        error="Empty transcription returned by API"
    )
    assert h.mux.send_keys_calls == []


async def test_send_keys_failure_skips_echo(harness) -> None:  # type: ignore[no-untyped-def]
    """If the pane went away between binding and the keypress, the
    transcript shouldn't echo (would mislead the user into thinking
    Claude got it)."""
    h = harness
    # Bind to a pane that doesn't exist on the multiplexer.
    await h.registry.bind(ALICE, CONV, "@gone")
    h.transcriber.next_text = "hello"
    h.channel.download_data = b"x"

    await h.channel.deliver_inbound(_audio_inbound())
    await h.outbox.stop()

    assert h.mux.send_keys_calls == []
    # No echo, no error message — just a silent drop. The next
    # text inbound would surface the unbound state via the
    # text dispatcher.
    assert h.channel.sent == []


# ── allow-list ───────────────────────────────────────────────────


async def test_disallowed_sender_silently_dropped() -> None:
    channel = FakeChannel()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    transcriber = FakeTranscriber()

    service = VoiceService(
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        transcriber=transcriber,
        allow_list=AllowList(["u-only-alice"]),
    )
    service.install(channel)
    try:
        bob = Person(user_id="u-bob")
        bob_voice = Inbound(
            sender=bob,
            conversation=CONV,
            text="",
            message_id="m",
            attachments=(Attachment(kind=AttachmentKind.AUDIO, fetch_id="x"),),
        )
        await channel.deliver_inbound(bob_voice)
        await outbox.stop()

        assert transcriber.calls == []
        assert channel.sent == []
    finally:
        pass
