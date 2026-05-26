# Topic-mode group routing

Paige can route per-pane traffic into individual Lark **topics** inside
a shared **topic-mode group**, instead of using one direct-message
chat per pane.

The wire-level concepts:

- **Topic-mode group** (Lark "话题模式群") — a group chat created with
  `chat_mode=group` + `group_message_type=thread`. Supports both
  ordinary group messages and message-rooted topics; users spawn a
  topic by long-pressing a message and choosing **Create topic**.
- **Topic id** (`omt_…`) — the durable Lark identifier for a topic.
  Lives on `Conversation.topic_id` and is the binding-key
  discriminator when set, so each topic gets its own binding.
- **Reply-chain root** (`om_…`) — the message id of the first
  message inside a topic. Lives on `Conversation.thread_id` and is
  what paige uses as `reply_to_message_id` so subsequent outbound
  messages stay threaded under the same topic. Independent of
  `topic_id`.

## Enabling

1. Create the group + capture its `chat_id`:
   ```
   ~/.paige/venv/bin/python scripts/create_topic_group.py --name paige
   ```
   The script calls `im.v1.chat.create` with the right knobs and adds
   the first user in `PAIGE_ALLOWED_USERS` as a member.

2. Add the printed id to `~/.paige/.env`:
   ```
   PAIGE_FEISHU_GROUP_ID=oc_…
   ```
   Paige logs this at startup so the intent is discoverable.

3. *(Optional but recommended)* Seed a `general` topic so the
   group has a permanent place for non-pane chitchat:
   ```
   ~/.paige/venv/bin/python scripts/seed_general_topic.py
   ```
   The script is idempotent — it skips if `~/.paige/topic_seed.json`
   already exists.

4. Restart paige to pick up the new env: `./scripts/prod.sh restart`.

## Routing

- **Inbounds in the configured group bypass the @mention filter.**
  The whole group exists for bot interaction, so every message is a
  candidate. Other groups continue to require an `@bot` mention.
- **`Conversation.topic_id` keys the binding** when present. New
  bindings created from inside a topic key on `topic_id` directly.
- **Legacy bindings still work.** `RunRegistry._lookup` falls back to
  `thread_id` when the primary `topic_id` key misses — so bindings
  persisted before topic-mode support keep matching post-upgrade.
- **Cards round-trip `_topic_id` alongside `_thread_id`** through
  every button / input value. Card-click events recover both
  identifiers reliably, regardless of where Lark's
  `context.thread_id` lands.

## Mixed-mode operation

A single paige process can hold both shapes of binding side-by-side:

- Old DM bindings (one per pane, in a private chat) — `topic_id=None`,
  keyed on chain root.
- New topic-mode bindings (one per topic, in the shared group) —
  `topic_id=omt_…`, keyed on topic id.

The flag flip is forward-only. Setting `PAIGE_FEISHU_GROUP_ID`
doesn't migrate or invalidate existing DM bindings; it just makes
new bindings created from within the group use topic-mode keying.

## Known Lark behaviors

- **Brief event delivery pause around chat metadata changes**
  (rename, member edits). Inbounds resume after ~30s. Not a paige
  bug — eventual consistency on Lark's side.
- **`im.chat.updated_v1` / `im.chat.member.*` events** arrive on
  the WS but paige doesn't subscribe to them. The `processor not
  found` lines in the log are cosmetic.
- **Streaming-card root pinning** (see openclaw#28273) — streaming
  cards inside a topic may spawn a new topic if `root_id` isn't
  pinned per chunk. Not yet observed end-to-end in paige; revisit
  if streaming cards leak out of a topic.
