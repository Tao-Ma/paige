"""Inbound + Attachment — what a user sends us."""

from paige.domain.conversation import Conversation
from paige.domain.inbound import Attachment, AttachmentKind, Inbound
from paige.domain.person import Person


def _person() -> Person:
    return Person(user_id="ou_abc", display_name="Alice")


def _conv() -> Conversation:
    return Conversation(chat_id="oc_xyz", thread_id="om_root")


def test_inbound_minimal() -> None:
    msg = Inbound(
        sender=_person(),
        conversation=_conv(),
        text="hi",
        message_id="om_1",
    )
    assert msg.text == "hi"
    assert msg.attachments == ()
    assert msg.mentions == ()
    assert msg.timestamp_ms == 0


def test_inbound_with_attachment() -> None:
    att = Attachment(kind=AttachmentKind.IMAGE, fetch_id="img_key_x")
    msg = Inbound(
        sender=_person(),
        conversation=_conv(),
        text="check this",
        message_id="om_2",
        attachments=(att,),
    )
    assert len(msg.attachments) == 1
    assert msg.attachments[0].kind == AttachmentKind.IMAGE


def test_attachment_kinds() -> None:
    """All three attachment kinds are exposed."""
    assert AttachmentKind.IMAGE.value == "image"
    assert AttachmentKind.AUDIO.value == "audio"
    assert AttachmentKind.FILE.value == "file"


def test_attachment_containing_message_id_is_optional() -> None:
    """Adapters that fetch by attachment id alone leave
    `containing_message_id` None. Feishu populates it from the
    enclosing message."""
    a = Attachment(kind=AttachmentKind.AUDIO, fetch_id="vc_123")
    assert a.containing_message_id is None
    b = Attachment(
        kind=AttachmentKind.AUDIO,
        fetch_id="vc_123",
        containing_message_id="om_xxx",
    )
    assert b.containing_message_id == "om_xxx"
