"""Transcriber — speech-to-text port.

A `Transcriber` turns audio bytes into a text transcript. paige's
voice flow plugs this in for backends where audio arrives as raw
bytes. Backends that pre-transcribe on the client (Feishu) skip
the port entirely — the text is already on `Inbound.text`.

The port stays narrow: one method, no streaming, no language
hinting. Callers pass MIME type as a hint; adapters that don't need
it ignore it. Errors propagate as exceptions; the application-side
handler decides whether to echo the failure to the user.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transcriber(Protocol):
    """Speech-to-text adapter."""

    async def transcribe(self, audio: bytes, *, mime: str = "") -> str:
        """Transcribe `audio` to plain text.

        Raises:
            ValueError: API returned an empty transcript.
            Exception: transport / authentication / quota errors —
                       caller logs + reports.
        """
        ...
