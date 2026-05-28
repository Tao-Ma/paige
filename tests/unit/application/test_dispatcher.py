"""Dispatcher — Watcher events ↔ Outbox; Channel inbound → Multiplexer."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.access import AllowList
from paige.application.dispatcher import (
    UNBOUND_HINT,
    Dispatcher,
    render_block,
)
from paige.application.echo_dedup import EchoDedup
from paige.application.outbox import Outbox
from paige.application.run_registry import RunRegistry
from paige.application.verbosity import ContentKind, Verbosity, VerbosityService
from paige.domain.conversation import Conversation
from paige.domain.inbound import Inbound
from paige.domain.outbound import CardContent, TextContent
from paige.domain.person import Person
from paige.domain.transcript import Block, BlockKind, Role, TranscriptEvent
from paige.testing.fakes import (
    FakeChannel,
    FakeMultiplexer,
    FakeStorage,
    FakeWatcher,
)

ALICE = Person(user_id="u-alice", display_name="Alice")
BOB = Person(user_id="u-bob", display_name="Bob")
CONV_A = Conversation(chat_id="-100", thread_id="42")
CONV_B = Conversation(chat_id="-100", thread_id="43")


@pytest.fixture
async def harness():  # type: ignore[no-untyped-def]
    """Wire up a full test rig: Channel, Watcher, Multiplexer,
    Storage, RunRegistry, Outbox, EchoDedup, Dispatcher."""
    channel = FakeChannel()
    watcher = FakeWatcher()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    echo = EchoDedup()
    # Default to FULL so most tests can ignore truncation; tests that
    # care set it explicitly via h.verbosity.set(...).
    verbosity = VerbosityService(default=Verbosity.FULL)
    dispatcher = Dispatcher(
        channel=channel,
        watcher=watcher,
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        echo_dedup=echo,
        verbosity=verbosity,
        allow_list=AllowList(),  # open
    )
    dispatcher.install()

    class Harness:
        pass

    h = Harness()
    h.channel = channel  # type: ignore[attr-defined]
    h.watcher = watcher  # type: ignore[attr-defined]
    h.mux = mux  # type: ignore[attr-defined]
    h.registry = registry  # type: ignore[attr-defined]
    h.outbox = outbox  # type: ignore[attr-defined]
    h.echo = echo  # type: ignore[attr-defined]
    h.verbosity = verbosity  # type: ignore[attr-defined]
    h.dispatcher = dispatcher  # type: ignore[attr-defined]
    yield h
    await outbox.stop()


# ── render_block ─────────────────────────────────────────────────


def test_render_text_block() -> None:
    assert render_block(Block(kind=BlockKind.TEXT, text="hi")) == "hi"


def test_render_empty_text_block_is_none() -> None:
    assert render_block(Block(kind=BlockKind.TEXT, text="")) is None


def test_render_thinking_wraps_in_italics() -> None:
    out = render_block(Block(kind=BlockKind.THINKING, text="hmm"))
    assert out == "💭 _hmm_"


def test_render_tool_use_dispatches_to_per_tool_renderer() -> None:
    """render_block delegates to `tool_renderers.render_tool_use` for
    TOOL_USE blocks. The card header already carries `🔧 {tool_name}`
    so the body is the per-tool body (here, a fenced bash block for
    `Bash`). Detailed per-tool behavior is exercised in
    `test_tool_renderers.py`."""
    block = Block(
        kind=BlockKind.TOOL_USE,
        text='{"command": "ls"}',
        tool_id="t1",
        tool_name="Bash",
    )
    out = render_block(block)
    assert out is not None
    # No more redundant `🔧 *Bash*(...)` wrapper — header carries it.
    assert "🔧" not in out
    assert "ls" in out
    assert "```bash" in out


def test_render_tool_use_without_input_falls_through_to_generic() -> None:
    """Empty input dict renders as `_(no input)_` via the generic
    fallback — not as the legacy `🔧 *Read*` wrapper."""
    block = Block(kind=BlockKind.TOOL_USE, text="", tool_id="t1", tool_name="Read")
    assert render_block(block) == "_(no input)_"


def test_render_tool_result() -> None:
    assert (
        render_block(Block(kind=BlockKind.TOOL_RESULT, text="output line", tool_id="t1"))
        == "output line"
    )


# ── inbound flow ─────────────────────────────────────────────────


async def test_inbound_to_bound_pane_calls_send_keys(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    inbound = Inbound(sender=ALICE, conversation=CONV_A, text="hello", message_id="m1")
    await h.channel.deliver_inbound(inbound)
    assert len(h.mux.send_keys_calls) == 1
    call = h.mux.send_keys_calls[0]
    assert call.pane_id == "@1"
    assert call.text == "hello"
    assert call.enter is True
    assert call.literal is True


async def test_inbound_records_echo(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    inbound = Inbound(sender=ALICE, conversation=CONV_A, text="echo me", message_id="m1")
    await h.channel.deliver_inbound(inbound)
    # Echo entry exists; the next is_echo for this pane+text returns True.
    assert h.echo.is_echo("@1", "echo me") is True


async def test_inbound_unbound_conversation_sends_hint(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    inbound = Inbound(sender=ALICE, conversation=CONV_A, text="hi", message_id="m1")
    await h.channel.deliver_inbound(inbound)
    # The hint is sent through the Outbox → FakeChannel.sent.
    # Wait for the future-completion via stop() drain.
    await h.outbox.stop()
    assert len(h.channel.sent) == 1
    out = h.channel.sent[0]
    assert isinstance(out.content, TextContent)
    assert out.content.text == UNBOUND_HINT
    assert h.mux.send_keys_calls == []


async def test_inbound_empty_text_is_ignored(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "proj", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    inbound = Inbound(sender=ALICE, conversation=CONV_A, text="   \n  ", message_id="m1")
    await h.channel.deliver_inbound(inbound)
    assert h.mux.send_keys_calls == []


# ── outbound flow: text/thinking ─────────────────────────────────


async def test_text_event_enqueues_outbound_for_each_binding(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.bind(BOB, CONV_B, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))

    h.watcher.track("sid", Path("/p/sid.jsonl"))
    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text="hi"),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()  # drain

    assert len(h.channel.sent) == 2
    convs = {o.conversation for o in h.channel.sent}
    assert convs == {CONV_A, CONV_B}


async def test_text_block_opts_out_of_collapse(harness) -> None:  # type: ignore[no-untyped-def]
    """Claude's prose must render flat — folding a reply behind a
    tap-to-expand header hurts readability. Only bulky tool output
    should auto-collapse."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text="a long reply"),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()

    sent = h.channel.sent[0]
    assert isinstance(sent.content, CardContent)
    assert sent.content.card.force_no_collapse is True


async def test_tool_use_args_stay_collapsible(harness) -> None:  # type: ignore[no-untyped-def]
    """Tool_use args (e.g. a big Write file body) can be bulky, so
    they keep the collapse behaviour — the opt-out is text-only."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            Block(
                kind=BlockKind.TOOL_USE,
                text='{"command": "ls"}',
                tool_id="t1",
                tool_name="Bash",
            ),
        ),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()

    sent = h.channel.sent[0]
    assert isinstance(sent.content, CardContent)
    assert sent.content.card.force_no_collapse is False


async def test_no_bindings_means_nothing_enqueued(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.watcher.track("sid", Path("/p/sid.jsonl"))
    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text="ignored"),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()
    assert h.channel.sent == []


async def test_event_with_multiple_blocks_emits_one_send_per_block(  # type: ignore[no-untyped-def]
    harness,
) -> None:
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            Block(kind=BlockKind.THINKING, text="planning"),
            Block(kind=BlockKind.TEXT, text="ok"),
        ),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()
    assert len(h.channel.sent) == 2
    texts = [o.content.card.text for o in h.channel.sent if isinstance(o.content, CardContent)]
    assert any("planning" in t for t in texts)
    assert "ok" in texts


# ── echo suppression ────────────────────────────────────────────


async def test_user_event_matching_recent_send_keys_is_dropped(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    # Simulate the user typing in chat → bot forwarded → JSONL bounces back.
    inbound = Inbound(sender=ALICE, conversation=CONV_A, text="hello", message_id="m1")
    await h.channel.deliver_inbound(inbound)
    # Now Claude's transcript records the user message.
    event = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TEXT, text="hello"),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()
    # Channel only has whatever the inbound flow produced (none — bound).
    assert h.channel.sent == []


async def test_user_event_without_recent_send_is_forwarded(harness) -> None:  # type: ignore[no-untyped-def]
    """No recent send_keys means the user typed at the laptop; forward
    the message to chat."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    event = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TEXT, text="typed at laptop"),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()
    assert len(h.channel.sent) == 1


# ── tool_use ↔ tool_result pairing ──────────────────────────────


async def test_tool_use_then_tool_result_edits_in_place(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    use_event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            Block(
                kind=BlockKind.TOOL_USE,
                text='{"command": "ls"}',
                tool_id="t1",
                tool_name="Bash",
            ),
        ),
    )
    await h.watcher.feed("sid", use_event)

    result_event = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TOOL_RESULT, text="file1\nfile2", tool_id="t1"),),
    )
    await h.watcher.feed("sid", result_event)

    await h.outbox.stop()
    # tool_use → one send (no second send for tool_result).
    assert len(h.channel.sent) == 1
    sent_outbound = h.channel.sent[0]
    assert isinstance(sent_outbound.content, CardContent)
    # Tool name is in the header now; the body is the per-tool render
    # (fenced bash block for Bash). See `test_tool_renderers.py`.
    assert sent_outbound.content.card.header_title == "🔧 Bash"
    assert "ls" in sent_outbound.content.card.text
    # tool_result → one edit in place, against the tool_use's anchor.
    assert len(h.channel.edits) == 1
    edited_anchor, edited_outbound = h.channel.edits[0]
    # FakeChannel hands out sequential message_ids starting at 1001.
    assert edited_anchor.message_id == "1001"
    assert isinstance(edited_outbound.content, CardContent)
    assert "file1" in edited_outbound.content.card.text


async def test_tool_result_without_prior_tool_use_falls_back_to_send(  # type: ignore[no-untyped-def]
    harness,
) -> None:
    """If we never saw the tool_use (e.g. it landed before paige started),
    deliver the tool_result as a fresh message rather than dropping it."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    result_event = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TOOL_RESULT, text="orphan output", tool_id="missing"),),
    )
    await h.watcher.feed("sid", result_event)
    await h.outbox.stop()
    assert len(h.channel.sent) == 1
    assert h.channel.edits == []
    assert isinstance(h.channel.sent[0].content, CardContent)
    assert h.channel.sent[0].content.card.text == "orphan output"


async def test_agent_fanout_coalesces_into_one_card(harness) -> None:  # type: ignore[no-untyped-def]
    """A turn that launches several agents produces ONE batch card,
    not one card per agent."""
    import json

    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    def agent(tid: str, sub: str, desc: str) -> Block:
        return Block(
            kind=BlockKind.TOOL_USE,
            tool_id=tid,
            tool_name="Agent",
            text=json.dumps({"subagent_type": sub, "description": desc, "prompt": "go"}),
        )

    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            agent("a1", "Explore", "find auth"),
            agent("a2", "Plan", "design"),
            agent("a3", "general-purpose", "refactor"),
        ),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()

    # One card for three agents.
    assert len(h.channel.sent) == 1
    # Result for one agent ticks its line in place (an edit, not a card).
    result = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TOOL_RESULT, text="ok", tool_id="a2"),),
    )
    await h.watcher.feed("sid", result)
    assert len(h.channel.sent) == 1  # still one card
    assert h.channel.edits  # patched in place


async def test_agent_groups_split_by_text_get_separate_cards(  # type: ignore[no-untyped-def]
    harness,
) -> None:
    import json

    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    def agent(tid: str) -> Block:
        return Block(
            kind=BlockKind.TOOL_USE,
            tool_id=tid,
            tool_name="Agent",
            text=json.dumps({"subagent_type": "Explore", "description": "x", "prompt": "go"}),
        )

    # agent, then narration text, then another agent → two batches.
    await h.watcher.feed("sid", TranscriptEvent(role=Role.ASSISTANT, blocks=(agent("a1"),)))
    await h.watcher.feed(
        "sid",
        TranscriptEvent(role=Role.ASSISTANT, blocks=(Block(kind=BlockKind.TEXT, text="now…"),)),
    )
    await h.watcher.feed("sid", TranscriptEvent(role=Role.ASSISTANT, blocks=(agent("a2"),)))
    await h.outbox.stop()

    # Two agent cards + one text card.
    assert len(h.channel.sent) == 3


async def test_task_ops_coalesce_into_one_card(harness) -> None:  # type: ignore[no-untyped-def]
    """A TaskCreate + its update render as one task card; the
    TaskUpdate result is swallowed (no orphan fresh-send)."""
    import json

    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    await h.watcher.feed(
        "sid",
        TranscriptEvent(
            role=Role.ASSISTANT,
            blocks=(
                Block(
                    kind=BlockKind.TOOL_USE,
                    tool_id="c1",
                    tool_name="TaskCreate",
                    text=json.dumps({"subject": "Do the thing", "description": "d"}),
                ),
            ),
        ),
    )
    await h.watcher.feed(
        "sid",
        TranscriptEvent(
            role=Role.USER,
            blocks=(Block(kind=BlockKind.TOOL_RESULT, tool_id="c1", text="Task #1 created"),),
        ),
    )
    # Update + its (bare) result.
    await h.watcher.feed(
        "sid",
        TranscriptEvent(
            role=Role.ASSISTANT,
            blocks=(
                Block(
                    kind=BlockKind.TOOL_USE,
                    tool_id="u1",
                    tool_name="TaskUpdate",
                    text=json.dumps({"taskId": "1", "status": "completed"}),
                ),
            ),
        ),
    )
    await h.watcher.feed(
        "sid",
        TranscriptEvent(
            role=Role.USER,
            blocks=(
                Block(kind=BlockKind.TOOL_RESULT, tool_id="u1", text="Updated task #1 status"),
            ),
        ),
    )
    await h.outbox.stop()

    # Exactly one task card; the update patched it; no orphan card for
    # the TaskUpdate result.
    assert len(h.channel.sent) == 1
    assert isinstance(h.channel.sent[0].content, CardContent)
    assert h.channel.sent[0].content.card.header_title.startswith("📋 Tasks")
    final = h.channel.edits[-1][1]
    assert isinstance(final.content, CardContent)
    assert "✓ #1 Do the thing" in final.content.card.text


async def test_inbound_disallowed_sender_is_dropped() -> None:
    """When the AllowList is closed, a non-listed sender's inbound
    should not reach send_keys, not produce an UNBOUND hint, and
    not record an echo entry. Total silence."""
    channel = FakeChannel()
    watcher = FakeWatcher()
    mux = FakeMultiplexer()
    storage = FakeStorage()
    registry = RunRegistry(storage)
    await registry.load()
    outbox = Outbox(channel)
    echo = EchoDedup()
    verbosity = VerbosityService(default=Verbosity.FULL)

    dispatcher = Dispatcher(
        channel=channel,
        watcher=watcher,
        registry=registry,
        multiplexer=mux,
        outbox=outbox,
        echo_dedup=echo,
        verbosity=verbosity,
        allow_list=AllowList(["u-only-alice"]),  # Bob isn't listed
    )
    dispatcher.install()

    mux.add_pane("@1", "proj", Path("/p"))
    bob = Person(user_id="u-bob")
    await registry.bind(bob, CONV_A, "@1")  # Bob has a binding...

    inbound = Inbound(sender=bob, conversation=CONV_A, text="hello", message_id="m1")
    await channel.deliver_inbound(inbound)
    await outbox.stop()

    # ...but the gate drops his inbound silently.
    assert mux.send_keys_calls == []
    assert channel.sent == []
    assert echo.is_echo("@1", "hello") is False


async def test_tool_use_pairing_per_binding(harness) -> None:  # type: ignore[no-untyped-def]
    """Each binding tracks its own tool_use anchor — so when two
    persons both watch the same run, both get the in-place edit on
    tool_result."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.bind(BOB, CONV_B, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    use_event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            Block(
                kind=BlockKind.TOOL_USE,
                text='{"command": "ls"}',
                tool_id="t1",
                tool_name="Bash",
            ),
        ),
    )
    await h.watcher.feed("sid", use_event)

    result_event = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TOOL_RESULT, text="ok", tool_id="t1"),),
    )
    await h.watcher.feed("sid", result_event)

    await h.outbox.stop()
    assert len(h.channel.sent) == 2  # one tool_use per binding
    assert len(h.channel.edits) == 2  # one edit per binding


# ── verbosity integration ───────────────────────────────────────


async def test_brief_truncates_long_text(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    h.verbosity.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.BRIEF)
    long_text = "x" * 500
    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text=long_text),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()

    assert len(h.channel.sent) == 1
    sent = h.channel.sent[0]
    assert isinstance(sent.content, CardContent)
    assert len(sent.content.card.text) < 500
    assert "truncated" in sent.content.card.text


async def test_full_passes_long_text_through(harness) -> None:  # type: ignore[no-untyped-def]
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    h.verbosity.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.FULL)
    long_text = "y" * 500
    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text=long_text),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()
    assert isinstance(h.channel.sent[0].content, CardContent)
    assert h.channel.sent[0].content.card.text == long_text


async def test_per_binding_verbosity_diverges(harness) -> None:  # type: ignore[no-untyped-def]
    """Two persons watching the same run can have different verbosity
    settings — one gets BRIEF, the other FULL."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.bind(BOB, CONV_B, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    h.verbosity.set(ALICE, CONV_A, ContentKind.TEXT, Verbosity.BRIEF)
    h.verbosity.set(BOB, CONV_B, ContentKind.TEXT, Verbosity.FULL)

    long_text = "z" * 400
    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(Block(kind=BlockKind.TEXT, text=long_text),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()

    by_conv = {o.conversation: o for o in h.channel.sent}
    alice_content = by_conv[CONV_A].content
    bob_content = by_conv[CONV_B].content
    assert isinstance(alice_content, CardContent) and isinstance(bob_content, CardContent)
    assert "truncated" in alice_content.card.text
    assert bob_content.card.text == long_text


async def test_brief_truncates_tool_result_too(harness) -> None:  # type: ignore[no-untyped-def]
    """tool_result text should be subject to BRIEF when configured —
    bash output of 1000 lines doesn't earn the screen real estate."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    h.verbosity.set(ALICE, CONV_A, ContentKind.TOOL_RESULT, Verbosity.BRIEF)
    long_output = "line\n" * 200
    # Need a prior tool_use to pair against; fall through to fresh send
    # is fine for this assertion.
    event = TranscriptEvent(
        role=Role.USER,
        blocks=(Block(kind=BlockKind.TOOL_RESULT, text=long_output, tool_id="orphan"),),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()
    assert isinstance(h.channel.sent[0].content, CardContent)
    assert "truncated" in h.channel.sent[0].content.card.text


# ── AskUserQuestion special-case ─────────────────────────────────


async def test_ask_user_tool_use_renders_buttoned_card(harness) -> None:  # type: ignore[no-untyped-def]
    """tool_use named AskUserQuestion bypasses the generic JSON-blob
    render: the dispatcher emits a card with one button per option."""
    import json as _json

    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    payload = _json.dumps(
        {
            "questions": [
                {
                    "question": "Confirm the project details?",
                    "header": "Project",
                    "options": [
                        {"label": "Yes — keep"},
                        {"label": "Different path"},
                    ],
                }
            ]
        }
    )
    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            Block(
                kind=BlockKind.TOOL_USE,
                text=payload,
                tool_id="toolu_X",
                tool_name="AskUserQuestion",
            ),
        ),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    card = sent.content.card
    assert "Confirm the project details?" in card.text
    # Two options → two button rows.
    assert len(card.rows) == 2
    [b1, b2] = [row[0] for row in card.rows]
    assert b1.label == "Yes — keep"
    assert b1.action_id == "askq:pick"
    assert b1.value["tool_id"] == "toolu_X"
    assert b1.value["idx"] == "0"
    assert b2.value["idx"] == "1"


async def test_ask_user_tool_result_edits_card_in_place(harness) -> None:  # type: ignore[no-untyped-def]
    """The buttoned card preserves the tool_use anchor pairing, so
    the user-role tool_result edits it in place once Claude Code
    writes the answer to JSONL."""
    import json as _json

    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    payload = _json.dumps({"questions": [{"question": "Q?", "options": [{"label": "A"}]}]})
    use_event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            Block(
                kind=BlockKind.TOOL_USE,
                text=payload,
                tool_id="toolu_Y",
                tool_name="AskUserQuestion",
            ),
        ),
    )
    await h.watcher.feed("sid", use_event)

    result_event = TranscriptEvent(
        role=Role.USER,
        blocks=(
            Block(
                kind=BlockKind.TOOL_RESULT,
                text="User has answered: A",
                tool_id="toolu_Y",
            ),
        ),
    )
    await h.watcher.feed("sid", result_event)

    await h.outbox.stop()
    assert len(h.channel.sent) == 1  # the question card
    assert len(h.channel.edits) == 1  # in-place edit with the answer
    _anchor, edited = h.channel.edits[0]
    assert isinstance(edited.content, CardContent)
    assert "User has answered: A" in edited.content.card.text


async def test_ask_user_falls_back_on_unparseable_input(harness) -> None:  # type: ignore[no-untyped-def]
    """If the input shape doesn't match what we expect, the
    dispatcher falls through to the generic tool_use render so the
    event isn't silently dropped."""
    h = harness
    h.mux.add_pane("@1", "p", Path("/p"))
    await h.registry.bind(ALICE, CONV_A, "@1")
    await h.registry.register_run("@1", "sid", Path("/p"))
    h.watcher.track("sid", Path("/p/sid.jsonl"))

    event = TranscriptEvent(
        role=Role.ASSISTANT,
        blocks=(
            Block(
                kind=BlockKind.TOOL_USE,
                text="{not parseable",
                tool_id="toolu_Z",
                tool_name="AskUserQuestion",
            ),
        ),
    )
    await h.watcher.feed("sid", event)
    await h.outbox.stop()

    [sent] = h.channel.sent
    assert isinstance(sent.content, CardContent)
    # Generic render — no buttons, the body is the per-tool fallback
    # (here `_(no input)_` because the input wasn't parseable). The
    # tool name lives in the header, not the body.
    assert sent.content.card.header_title == "🔧 AskUserQuestion"
    assert sent.content.card.rows == ()
    assert sent.content.card.text == "_(no input)_"
