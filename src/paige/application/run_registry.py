"""RunRegistry — identity/binding tracker for runs and conversations.

A "binding" connects (Person, Conversation) → pane_id: the user has
bound a chat/thread to a tmux pane that's running a Claude session.
A "run pointer" connects pane_id → (run_id, cwd): the pane is
running this Claude transcript on disk.

Together these answer:
  - "Where do messages from THIS user/thread go?"     (get_pane)
  - "Whose conversations should hear THIS run's events?"
                                                       (find_bindings_for_run)
  - "What's running in pane X?"                        (get_run_pointer)
  - "Pane X just died — who was bound to it?"          (find_bindings_for_pane)

All state is persisted via the `Storage` port under one key. Mutations
save synchronously; loads happen via `load()` after construction.

Run "pointers" are intentionally minimal — just `(run_id, cwd)`.
Rich derived state (transcript, summary, message_count, total_tokens)
is computed on demand by other services that read JSONL. Storing only
identity here keeps the registry trivially correct after `/clear` (a
single `clear_run` + `register_run` swap, no derived fields to
invalidate).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..domain.conversation import Conversation
from ..domain.host import LOCAL_HOST_ID
from ..domain.pane import Binding
from ..domain.person import Person
from ..ports.storage import Storage

_STATE_KEY = "run_registry"


@dataclass(frozen=True)
class RunPointer:
    """The minimum identity needed to find a run on disk.

    `transcript_path` is intentionally absent — derived from
    `run_id` + `cwd` + project-encoding rules at use time, not
    stored here.

    `host_id` defaults to `"local"` so single-host deployments and
    pre-multi-host persisted state stay valid; the SSH slice
    populates it per binding/run. See `doc/multi-host.md`.
    """

    run_id: str
    cwd: Path
    host_id: str = LOCAL_HOST_ID


class RunRegistry:
    """Maps (Person, Conversation) ↔ pane_id ↔ (run_id, cwd)."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        # (person.user_id, chat_id, discriminator) → (host_id, pane_id, thread_id, topic_id).
        # `discriminator` is `topic_id` when set (Lark topic-mode
        # group → one binding per topic), else falls back to
        # `thread_id` (reply-chain root → one binding per chain).
        # Storing both alongside the value keeps the original
        # Conversation fields recoverable on load. `host_id`
        # defaults to `"local"` for entries persisted before the
        # multi-host refactor.
        self._bindings: dict[tuple[str, str, str], tuple[str, str, str | None, str | None]] = {}
        # pane_id → RunPointer.
        self._runs: dict[str, RunPointer] = {}
        # Display-name cache for Persons we've seen, since we store
        # only user_id in the binding key. Lets list APIs return
        # `Binding(person=Person(user_id, display_name=…))` even
        # though display_name isn't part of the key.
        self._person_names: dict[str, str] = {}

    # ── lifecycle ────────────────────────────────────────────────

    async def load(self) -> None:
        """Load persisted state. Call once after construction."""
        state = await self._storage.load(_STATE_KEY)
        if state is None:
            return
        raw_bindings = state.get("bindings")
        if isinstance(raw_bindings, list):
            for raw in cast("list[Any]", raw_bindings):
                if not isinstance(raw, dict):
                    continue
                entry = cast("dict[str, Any]", raw)
                user_id = str(entry.get("person_id", ""))
                chat_id = str(entry.get("chat_id", ""))
                thread_id_raw = entry.get("thread_id")
                thread_id = None if thread_id_raw is None else str(thread_id_raw)
                topic_id_raw = entry.get("topic_id")
                topic_id = None if topic_id_raw is None else str(topic_id_raw)
                pane_id = str(entry.get("pane_id", ""))
                # `host_id` was added in the multi-host refactor;
                # earlier persisted entries don't have it. Treat
                # missing / empty as the synthetic local host.
                host_id = str(entry.get("host_id", "")) or LOCAL_HOST_ID
                display_name = str(entry.get("display_name", ""))
                if not user_id or not chat_id or not pane_id:
                    continue
                discriminator = topic_id or thread_id or ""
                self._bindings[(user_id, chat_id, discriminator)] = (
                    host_id,
                    pane_id,
                    thread_id,
                    topic_id,
                )
                if display_name:
                    self._person_names[user_id] = display_name
        raw_runs = state.get("runs")
        if isinstance(raw_runs, list):
            for raw in cast("list[Any]", raw_runs):
                if not isinstance(raw, dict):
                    continue
                entry = cast("dict[str, Any]", raw)
                pane_id = str(entry.get("pane_id", ""))
                run_id = str(entry.get("run_id", ""))
                cwd_raw = entry.get("cwd")
                host_id = str(entry.get("host_id", "")) or LOCAL_HOST_ID
                if not pane_id or not run_id or not isinstance(cwd_raw, str):
                    continue
                self._runs[pane_id] = RunPointer(run_id=run_id, cwd=Path(cwd_raw), host_id=host_id)

    # ── bindings ─────────────────────────────────────────────────

    async def bind(
        self,
        person: Person,
        conversation: Conversation,
        pane_id: str,
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> None:
        """Bind a (person, conversation) to a (host_id, pane_id).
        Idempotent — re-binding the same triple is a no-op (still
        saves). `host_id` defaults to `"local"` so single-host
        callers don't need to pass it."""
        key = self._key(person, conversation)
        self._bindings[key] = (
            host_id,
            pane_id,
            conversation.thread_id,
            conversation.topic_id,
        )
        if person.display_name:
            self._person_names[person.user_id] = person.display_name
        await self._save()

    async def unbind(self, person: Person, conversation: Conversation) -> None:
        """Drop the binding for (person, conversation).

        Two regimes, selected by whether `topic_id` is present:

        - **In a Lark topic** (`topic_id` set): exact-key precision.
          Drops only the binding for this specific topic. Other
          topics in the same group are untouched.
        - **Outside a topic** (`topic_id is None`): chat-scoped.
          Drops every binding the requester owns under this
          `chat_id`. Covers P2P DMs and group main-chats. Lets
          users `/unbind` a whole DM in one shot regardless of
          which reply chain the unbind message happens to land in
          — the chain-root key would otherwise miss whenever the
          unbind isn't a reply to the original bind card.

        No-op if nothing matches.
        """
        if conversation.topic_id is not None:
            self._bindings.pop(self._key(person, conversation), None)
        else:
            stale = [
                k
                for k, (_, _, _, topic_id) in self._bindings.items()
                if k[0] == person.user_id and k[1] == conversation.chat_id and topic_id is None
            ]
            for k in stale:
                self._bindings.pop(k, None)
        await self._save()

    def get_pane(self, person: Person, conversation: Conversation) -> str | None:
        """Return the bound pane_id, or None. For host-aware callers
        use `get_binding` to also see `host_id`."""
        entry = self._lookup(person, conversation)
        return entry[1] if entry is not None else None

    def get_host(self, person: Person, conversation: Conversation) -> str | None:
        """Return the bound host_id, or None when the topic isn't
        bound. Useful for routing inbound forwards to the right
        multiplexer once the SSH adapter lands."""
        entry = self._lookup(person, conversation)
        return entry[0] if entry is not None else None

    def get_binding(self, person: Person, conversation: Conversation) -> Binding | None:
        """Full Binding for the topic, or None when unbound. Returns
        a fresh Binding object — callers can read `host_id`,
        `pane_id`, and the original Person / Conversation off it."""
        entry = self._lookup(person, conversation)
        if entry is None:
            return None
        host_id, pane_id, _, _ = entry
        return Binding(
            person=Person(
                user_id=person.user_id,
                display_name=self._person_names.get(person.user_id, person.display_name),
            ),
            conversation=conversation,
            pane_id=pane_id,
            host_id=host_id,
        )

    # ── runs ─────────────────────────────────────────────────────

    async def register_run(
        self,
        pane_id: str,
        run_id: str,
        cwd: Path,
        *,
        host_id: str = LOCAL_HOST_ID,
    ) -> None:
        """Set the run pointer for `pane_id`. Overwrites any prior
        pointer (e.g. after `/clear`). `host_id` defaults to `"local"`
        for single-host call sites."""
        self._runs[pane_id] = RunPointer(run_id=run_id, cwd=cwd, host_id=host_id)
        await self._save()

    async def clear_run(self, pane_id: str) -> None:
        """Drop the run pointer for `pane_id`. Bindings to this pane
        survive — they may be re-pointed at a fresh run shortly."""
        self._runs.pop(pane_id, None)
        await self._save()

    def get_run_pointer(self, pane_id: str) -> RunPointer | None:
        return self._runs.get(pane_id)

    # ── pane lifecycle ───────────────────────────────────────────

    async def remove_pane(self, pane_id: str) -> None:
        """Pane is gone — drop its run pointer AND any bindings that
        targeted it. Cascade unbind. Matches by `pane_id` only;
        scoping to a host is the SSH-slice concern (panes ids are
        unique within a host but not across hosts; today everything
        is local so the existing semantics hold)."""
        self._runs.pop(pane_id, None)
        stale_keys = [k for k, (_, pid, _, _) in self._bindings.items() if pid == pane_id]
        for k in stale_keys:
            self._bindings.pop(k, None)
        await self._save()

    # ── reverse lookups ──────────────────────────────────────────

    def find_bindings_for_run(self, run_id: str) -> list[Binding]:
        """All bindings whose pane is currently running `run_id`."""
        panes = {pid for pid, ptr in self._runs.items() if ptr.run_id == run_id}
        return [b for b in self._all_bindings() if b.pane_id in panes]

    def find_bindings_for_pane(self, pane_id: str) -> list[Binding]:
        """All bindings pointing at `pane_id` (one pane can serve
        multiple conversations, e.g. an observer + the primary user).
        Matches by `pane_id` only — adequate while everything is
        local; the SSH slice will scope this by host."""
        return [b for b in self._all_bindings() if b.pane_id == pane_id]

    def list_bindings_for(self, person: Person) -> list[Binding]:
        """All bindings owned by `person`."""
        return [b for b in self._all_bindings() if b.person.user_id == person.user_id]

    def list_panes(self) -> list[str]:
        """Distinct pane_ids that have registered runs."""
        return list(self._runs.keys())

    # ── internals ────────────────────────────────────────────────

    @staticmethod
    def _key(person: Person, conversation: Conversation) -> tuple[str, str, str]:
        # Prefer `topic_id` (Lark omt_*) as the discriminator when set,
        # so each topic in a topic-mode group gets its own binding.
        # Falls back to `thread_id` (reply-chain root) for chats not
        # in topic mode — same key shape as pre-topic-mode bindings.
        discriminator = conversation.topic_id or conversation.thread_id or ""
        return (
            person.user_id,
            conversation.chat_id,
            discriminator,
        )

    def _lookup(
        self, person: Person, conversation: Conversation
    ) -> tuple[str, str, str | None, str | None] | None:
        """Find the binding entry for `(person, conversation)`.

        Tries the primary key first (`topic_id` discriminator when
        set, else `thread_id`). If that misses AND `topic_id` is set,
        falls back to keying on `thread_id` — covers the migration
        case where a binding was persisted with `topic_id=null`
        before the inbound parser learned to extract `omt_*` but
        post-fix inbounds now carry the topic id. Bindings created
        after the parser fix go through the primary key path.
        """
        entry = self._bindings.get(self._key(person, conversation))
        if entry is not None:
            return entry
        if conversation.topic_id and conversation.thread_id:
            legacy_key = (person.user_id, conversation.chat_id, conversation.thread_id)
            return self._bindings.get(legacy_key)
        return None

    def _all_bindings(self) -> list[Binding]:
        out: list[Binding] = []
        for (uid, chat_id, _disc), (
            host_id,
            pane_id,
            thread_id,
            topic_id,
        ) in self._bindings.items():
            person = Person(
                user_id=uid,
                display_name=self._person_names.get(uid, ""),
            )
            conversation = Conversation(
                chat_id=chat_id,
                thread_id=thread_id,
                topic_id=topic_id,
            )
            out.append(
                Binding(
                    person=person,
                    conversation=conversation,
                    pane_id=pane_id,
                    host_id=host_id,
                )
            )
        return out

    async def _save(self) -> None:
        snapshot: dict[str, Any] = {
            "bindings": [
                {
                    "person_id": uid,
                    "display_name": self._person_names.get(uid, ""),
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "topic_id": topic_id,
                    "pane_id": pane_id,
                    "host_id": host_id,
                }
                for (uid, chat_id, _disc), (
                    host_id,
                    pane_id,
                    thread_id,
                    topic_id,
                ) in self._bindings.items()
            ],
            "runs": [
                {
                    "pane_id": pane_id,
                    "run_id": ptr.run_id,
                    "cwd": str(ptr.cwd),
                    "host_id": ptr.host_id,
                }
                for pane_id, ptr in self._runs.items()
            ],
        }
        await self._storage.save(_STATE_KEY, snapshot)
