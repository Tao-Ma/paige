"""Channel Protocol — compliance + minimal stub."""

from __future__ import annotations

from paige.domain.card import ActionEvent
from paige.domain.conversation import Anchor, Conversation
from paige.domain.inbound import Attachment, Inbound
from paige.domain.outbound import Outbound, TextContent, TypingContent
from paige.ports.channel import (
    ActionHandler,
    Channel,
    CommandHandler,
    InboundHandler,
)


class _StubChannel:
    """Minimal Channel impl — verifies the Protocol surface."""

    def __init__(self) -> None:
        self.sent: list[Outbound] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, outbound: Outbound) -> Anchor | None:
        self.sent.append(outbound)
        if isinstance(outbound.content, TypingContent):
            return None
        return Anchor(conversation=outbound.conversation, message_id="om_1")

    async def edit(self, anchor: Anchor, outbound: Outbound) -> Anchor | None:
        return None

    async def delete(self, anchor: Anchor) -> None:
        return None

    async def download(self, attachment: Attachment) -> bytes:
        return b""

    async def ack(self, event: ActionEvent, text: str | None = None) -> None:
        return None

    async def probe(self, conversation: Conversation) -> bool:
        return True

    def on_inbound(self, handler: InboundHandler) -> None: ...
    def on_command(self, name: str, handler: CommandHandler) -> None: ...
    def on_action(self, handler: ActionHandler) -> None: ...

    async def dispatch_command(self, inbound: Inbound, name: str, arg: str) -> bool:
        return False


def test_stub_satisfies_channel_protocol() -> None:
    """Runtime isinstance check: a minimal stub conforms to the
    Channel Protocol surface."""
    stub = _StubChannel()
    assert isinstance(stub, Channel)


async def test_send_returns_anchor() -> None:
    stub: Channel = _StubChannel()
    conv = Conversation(chat_id="oc", thread_id="om")
    out = Outbound(conversation=conv, content=TextContent(text="hi"))
    anchor = await stub.send(out)
    assert anchor is not None
    assert anchor.conversation is conv
    assert anchor.message_id == "om_1"


async def test_send_typing_returns_none() -> None:
    """Typing has no anchor — fire-and-forget."""
    stub: Channel = _StubChannel()
    conv = Conversation(chat_id="oc")
    out = Outbound(conversation=conv, content=TypingContent())
    anchor = await stub.send(out)
    assert anchor is None


async def test_edit_returns_optional_anchor() -> None:
    """edit() returning None means "patched in place"; a non-None
    return signals the cross-type fallback resent the message."""
    stub: Channel = _StubChannel()
    conv = Conversation(chat_id="oc")
    anchor = Anchor(conversation=conv, message_id="om_x")
    out = Outbound(conversation=conv, content=TextContent(text="new"))
    result = await stub.edit(anchor, out)
    assert result is None
