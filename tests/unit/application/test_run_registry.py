"""RunRegistry — bindings + run pointers + persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from paige.application.run_registry import RunPointer, RunRegistry
from paige.domain.conversation import Conversation
from paige.domain.pane import Binding
from paige.domain.person import Person
from paige.testing.fakes import FakeStorage

ALICE = Person(user_id="u-alice", display_name="Alice")
BOB = Person(user_id="u-bob", display_name="Bob")
CONV_A = Conversation(chat_id="-100", thread_id="42")
CONV_B = Conversation(chat_id="-100", thread_id="43")
CONV_DM = Conversation(chat_id="oc-1")


@pytest.fixture
async def registry() -> RunRegistry:
    storage = FakeStorage()
    r = RunRegistry(storage)
    await r.load()  # empty state
    return r


# ── bindings ─────────────────────────────────────────────────────


async def test_bind_then_get_pane(registry: RunRegistry) -> None:
    await registry.bind(ALICE, CONV_A, "@1")
    assert registry.get_pane(ALICE, CONV_A) == "@1"


async def test_get_pane_returns_none_when_unbound(registry: RunRegistry) -> None:
    assert registry.get_pane(ALICE, CONV_A) is None


async def test_unbind_removes_binding(registry: RunRegistry) -> None:
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.unbind(ALICE, CONV_A)
    assert registry.get_pane(ALICE, CONV_A) is None


async def test_unbind_missing_is_noop(registry: RunRegistry) -> None:
    await registry.unbind(ALICE, CONV_A)  # never bound
    # No exception.


async def test_rebind_overwrites_pane(registry: RunRegistry) -> None:
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(ALICE, CONV_A, "@2")  # rebind same triple
    assert registry.get_pane(ALICE, CONV_A) == "@2"


async def test_dm_conversation_no_thread(registry: RunRegistry) -> None:
    """thread_id=None is a valid distinct binding key."""
    await registry.bind(ALICE, CONV_DM, "@5")
    assert registry.get_pane(ALICE, CONV_DM) == "@5"


async def test_different_persons_can_bind_same_conversation(
    registry: RunRegistry,
) -> None:
    """Two users observing the same chat thread bind independently."""
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(BOB, CONV_A, "@2")
    assert registry.get_pane(ALICE, CONV_A) == "@1"
    assert registry.get_pane(BOB, CONV_A) == "@2"


async def test_topic_id_disambiguates_bindings_in_same_group(
    registry: RunRegistry,
) -> None:
    """Two Lark topics in the same shared group share `chat_id` but
    differ in `topic_id` — each must bind to its own pane."""
    topic_one = Conversation(chat_id="oc_group", thread_id="om_root_a", topic_id="omt_topic_a")
    topic_two = Conversation(chat_id="oc_group", thread_id="om_root_b", topic_id="omt_topic_b")
    await registry.bind(ALICE, topic_one, "@1")
    await registry.bind(ALICE, topic_two, "@2")
    assert registry.get_pane(ALICE, topic_one) == "@1"
    assert registry.get_pane(ALICE, topic_two) == "@2"


async def test_dm_unbind_is_chat_scoped(registry: RunRegistry) -> None:
    """In a chat without a Lark topic (P2P DM or group main-chat),
    /unbind drops every non-topic binding the requester owns under
    this chat_id — even when the unbind message lands in a different
    reply chain than the original bind. Matches the operator's
    intent of "unbind this whole DM."
    """
    chain_a = Conversation(chat_id="oc_dm", thread_id="om_card1")
    chain_b = Conversation(chat_id="oc_dm", thread_id="om_card2")
    await registry.bind(ALICE, chain_a, "@1")
    await registry.bind(ALICE, chain_b, "@2")
    # User types /unbind as a fresh message — its chain root won't
    # match either of the bind keys.
    fresh = Conversation(chat_id="oc_dm", thread_id="om_unbind_msg")
    await registry.unbind(ALICE, fresh)
    assert registry.get_pane(ALICE, chain_a) is None
    assert registry.get_pane(ALICE, chain_b) is None


async def test_dm_unbind_leaves_topic_bindings_untouched(
    registry: RunRegistry,
) -> None:
    """Chat-scoped unbind only drops non-topic bindings. Topic
    bindings in the same chat (rare — only possible in a topic-mode
    group's main chat) stay bound."""
    main = Conversation(chat_id="oc_group", thread_id="om_main")
    topic = Conversation(chat_id="oc_group", thread_id="om_root", topic_id="omt_x")
    await registry.bind(ALICE, main, "@1")
    await registry.bind(ALICE, topic, "@2")
    await registry.unbind(ALICE, Conversation(chat_id="oc_group", thread_id="om_other"))
    assert registry.get_pane(ALICE, main) is None
    assert registry.get_pane(ALICE, topic) == "@2"


async def test_topic_unbind_is_key_precise(registry: RunRegistry) -> None:
    """Inside a Lark topic, /unbind affects only that topic. Other
    topics in the same group keep their bindings."""
    topic_one = Conversation(chat_id="oc_group", thread_id="om_a", topic_id="omt_a")
    topic_two = Conversation(chat_id="oc_group", thread_id="om_b", topic_id="omt_b")
    await registry.bind(ALICE, topic_one, "@1")
    await registry.bind(ALICE, topic_two, "@2")
    await registry.unbind(ALICE, topic_one)
    assert registry.get_pane(ALICE, topic_one) is None
    assert registry.get_pane(ALICE, topic_two) == "@2"


async def test_topic_id_persists_across_load(registry: RunRegistry) -> None:
    """The topic_id round-trips through Storage so a restart
    rehydrates topic-scoped bindings correctly."""
    storage = registry._storage  # noqa: SLF001
    conv = Conversation(chat_id="oc_group", thread_id="om_root", topic_id="omt_topic")
    await registry.bind(ALICE, conv, "@1")
    fresh = RunRegistry(storage)
    await fresh.load()
    binding = fresh.get_binding(ALICE, conv)
    assert binding is not None
    assert binding.conversation.topic_id == "omt_topic"
    assert binding.pane_id == "@1"


# ── runs ─────────────────────────────────────────────────────────


async def test_register_run_then_get_pointer(registry: RunRegistry) -> None:
    await registry.register_run("@1", "sid-abc", Path("/proj"))
    ptr = registry.get_run_pointer("@1")
    assert ptr == RunPointer(run_id="sid-abc", cwd=Path("/proj"))


async def test_get_run_pointer_returns_none_when_unset(
    registry: RunRegistry,
) -> None:
    assert registry.get_run_pointer("@99") is None


async def test_register_run_overwrites_after_clear() -> None:
    """Simulates `/clear` rotating the session_id while keeping pane."""
    storage = FakeStorage()
    r = RunRegistry(storage)
    await r.load()
    await r.register_run("@1", "sid-old", Path("/proj"))
    await r.register_run("@1", "sid-new", Path("/proj"))
    ptr = r.get_run_pointer("@1")
    assert ptr is not None and ptr.run_id == "sid-new"


async def test_clear_run_removes_pointer(registry: RunRegistry) -> None:
    await registry.register_run("@1", "sid-a", Path("/p"))
    await registry.clear_run("@1")
    assert registry.get_run_pointer("@1") is None


async def test_clear_run_keeps_bindings(registry: RunRegistry) -> None:
    """`/clear` rotates the session but the topic stays bound to the
    pane."""
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.register_run("@1", "sid-a", Path("/p"))
    await registry.clear_run("@1")
    assert registry.get_pane(ALICE, CONV_A) == "@1"


# ── reverse lookups ──────────────────────────────────────────────


async def test_find_bindings_for_run(registry: RunRegistry) -> None:
    """When a TranscriptEvent for run X arrives, the registry tells
    us which (person, conversation) pairs should hear it."""
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(BOB, CONV_B, "@1")  # both watching the same pane
    await registry.bind(ALICE, CONV_DM, "@2")  # different pane
    await registry.register_run("@1", "sid-abc", Path("/p"))
    await registry.register_run("@2", "sid-other", Path("/p"))

    bindings = registry.find_bindings_for_run("sid-abc")
    persons = {(b.person.user_id, b.conversation.thread_id) for b in bindings}
    assert persons == {("u-alice", "42"), ("u-bob", "43")}


async def test_find_bindings_for_run_with_no_pane_yields_empty(
    registry: RunRegistry,
) -> None:
    """A run that's not registered against any pane has no bindings."""
    await registry.bind(ALICE, CONV_A, "@1")
    # No register_run call.
    assert registry.find_bindings_for_run("sid-abc") == []


async def test_find_bindings_for_pane(registry: RunRegistry) -> None:
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(BOB, CONV_B, "@1")
    await registry.bind(ALICE, CONV_DM, "@2")
    bindings = registry.find_bindings_for_pane("@1")
    persons = {b.person.user_id for b in bindings}
    assert persons == {"u-alice", "u-bob"}


async def test_list_bindings_for_person(registry: RunRegistry) -> None:
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(ALICE, CONV_B, "@1")
    await registry.bind(BOB, CONV_A, "@2")
    out = registry.list_bindings_for(ALICE)
    threads = {b.conversation.thread_id for b in out}
    assert threads == {"42", "43"}


async def test_list_panes(registry: RunRegistry) -> None:
    await registry.register_run("@1", "sid-a", Path("/p"))
    await registry.register_run("@5", "sid-b", Path("/p"))
    assert set(registry.list_panes()) == {"@1", "@5"}


# ── pane removal cascades ───────────────────────────────────────


async def test_remove_pane_drops_run_and_bindings(
    registry: RunRegistry,
) -> None:
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(BOB, CONV_B, "@1")
    await registry.register_run("@1", "sid-x", Path("/p"))
    await registry.remove_pane("@1")
    assert registry.get_pane(ALICE, CONV_A) is None
    assert registry.get_pane(BOB, CONV_B) is None
    assert registry.get_run_pointer("@1") is None


async def test_remove_pane_keeps_unrelated_bindings(
    registry: RunRegistry,
) -> None:
    await registry.bind(ALICE, CONV_A, "@1")
    await registry.bind(ALICE, CONV_B, "@2")
    await registry.remove_pane("@1")
    assert registry.get_pane(ALICE, CONV_B) == "@2"


# ── persistence ─────────────────────────────────────────────────


async def test_save_then_load_roundtrip() -> None:
    """A second registry instance against the same storage sees the
    same bindings + runs."""
    storage = FakeStorage()
    r1 = RunRegistry(storage)
    await r1.load()
    await r1.bind(ALICE, CONV_A, "@1")
    await r1.bind(BOB, CONV_DM, "@2")
    await r1.register_run("@1", "sid-a", Path("/proj-a"))
    await r1.register_run("@2", "sid-b", Path("/proj-b"))

    r2 = RunRegistry(storage)
    await r2.load()
    assert r2.get_pane(ALICE, CONV_A) == "@1"
    assert r2.get_pane(BOB, CONV_DM) == "@2"
    ptr = r2.get_run_pointer("@1")
    assert ptr == RunPointer(run_id="sid-a", cwd=Path("/proj-a"))


async def test_load_with_empty_storage_is_noop() -> None:
    storage = FakeStorage()
    r = RunRegistry(storage)
    await r.load()
    assert r.list_panes() == []
    assert r.list_bindings_for(ALICE) == []


async def test_load_skips_corrupt_entries() -> None:
    """A malformed persisted entry shouldn't poison the whole load."""
    storage = FakeStorage()
    await storage.save(
        "run_registry",
        {
            "bindings": [
                {"chat_id": "x", "pane_id": "@1"},  # missing person_id
                {
                    "person_id": "u-alice",
                    "chat_id": "-100",
                    "thread_id": "42",
                    "pane_id": "@9",
                    "display_name": "Alice",
                },
            ],
            "runs": [
                {"pane_id": "@9"},  # missing run_id
                {
                    "pane_id": "@9",
                    "run_id": "sid-x",
                    "cwd": "/p",
                },
            ],
        },
    )
    r = RunRegistry(storage)
    await r.load()
    assert r.get_pane(ALICE, CONV_A) == "@9"
    ptr = r.get_run_pointer("@9")
    assert ptr == RunPointer(run_id="sid-x", cwd=Path("/p"))


async def test_persisted_binding_round_trips_display_name() -> None:
    storage = FakeStorage()
    r1 = RunRegistry(storage)
    await r1.load()
    await r1.bind(Person(user_id="u1", display_name="Carol"), CONV_A, "@3")

    r2 = RunRegistry(storage)
    await r2.load()
    [binding] = r2.list_bindings_for(Person(user_id="u1"))
    assert binding == Binding(
        person=Person(user_id="u1", display_name="Carol"),
        conversation=CONV_A,
        pane_id="@3",
    )


async def test_persisted_dm_round_trips_thread_none() -> None:
    storage = FakeStorage()
    r1 = RunRegistry(storage)
    await r1.load()
    await r1.bind(ALICE, CONV_DM, "@5")  # CONV_DM.thread_id is None

    r2 = RunRegistry(storage)
    await r2.load()
    assert r2.get_pane(ALICE, CONV_DM) == "@5"
    [binding] = r2.list_bindings_for(ALICE)
    assert binding.conversation.thread_id is None


# ── host_id (multi-host scaffolding) ─────────────────────────────


async def test_default_bind_uses_local_host(registry: RunRegistry) -> None:
    """`bind(...)` without `host_id` defaults to "local" so single-
    host call sites don't need to know about the parameter."""
    await registry.bind(ALICE, CONV_A, "@1")
    assert registry.get_host(ALICE, CONV_A) == "local"
    binding = registry.get_binding(ALICE, CONV_A)
    assert binding is not None
    assert binding.host_id == "local"
    assert binding.pane_id == "@1"


async def test_bind_with_explicit_host_id(registry: RunRegistry) -> None:
    await registry.bind(ALICE, CONV_A, "@1", host_id="dev-1")
    assert registry.get_host(ALICE, CONV_A) == "dev-1"
    binding = registry.get_binding(ALICE, CONV_A)
    assert binding is not None
    assert binding.host_id == "dev-1"


async def test_get_host_returns_none_when_unbound(registry: RunRegistry) -> None:
    assert registry.get_host(ALICE, CONV_A) is None
    assert registry.get_binding(ALICE, CONV_A) is None


async def test_register_run_with_host_id(registry: RunRegistry) -> None:
    await registry.register_run("@1", "run-abc", Path("/p"), host_id="dev-1")
    ptr = registry.get_run_pointer("@1")
    assert ptr == RunPointer(run_id="run-abc", cwd=Path("/p"), host_id="dev-1")


async def test_register_run_default_host_is_local(registry: RunRegistry) -> None:
    await registry.register_run("@1", "run-abc", Path("/p"))
    ptr = registry.get_run_pointer("@1")
    assert ptr is not None
    assert ptr.host_id == "local"


async def test_persisted_binding_round_trips_host_id() -> None:
    """Save then load preserves host_id. Confirms the on-disk schema
    survives a paige restart for non-local hosts (in scope when the
    SSH adapter slice lands)."""
    storage = FakeStorage()
    r1 = RunRegistry(storage)
    await r1.load()
    await r1.bind(ALICE, CONV_A, "@7", host_id="dev-1")
    await r1.register_run("@7", "run-7", Path("/repo"), host_id="dev-1")

    r2 = RunRegistry(storage)
    await r2.load()
    binding = r2.get_binding(ALICE, CONV_A)
    assert binding is not None
    assert binding.host_id == "dev-1"
    ptr = r2.get_run_pointer("@7")
    assert ptr is not None
    assert ptr.host_id == "dev-1"


async def test_persisted_state_without_host_id_loads_as_local() -> None:
    """Pre-multi-host paige saved bindings without `host_id`. On
    load they must materialise as local-host bindings — otherwise
    every existing user would lose their bindings on the upgrade
    that ships this refactor."""
    storage = FakeStorage()
    # Manually craft a pre-refactor snapshot — no `host_id` keys.
    await storage.save(
        "run_registry",
        {
            "bindings": [
                {
                    "person_id": "u-alice",
                    "display_name": "Alice",
                    "chat_id": "-100",
                    "thread_id": "42",
                    "pane_id": "@1",
                }
            ],
            "runs": [
                {"pane_id": "@1", "run_id": "old-run", "cwd": "/repo"},
            ],
        },
    )
    r = RunRegistry(storage)
    await r.load()
    binding = r.get_binding(ALICE, CONV_A)
    assert binding is not None
    assert binding.host_id == "local"
    ptr = r.get_run_pointer("@1")
    assert ptr is not None
    assert ptr.host_id == "local"
