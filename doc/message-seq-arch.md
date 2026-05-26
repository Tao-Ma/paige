# Message processing arch + where seq stamping fits

Working notes for diagnosing sequence glitches on outgoing messages.
Distilled from `application/dispatcher.py`, `application/outbox.py`,
`application/message_seq.py`, `adapters/jsonl_watcher.py`,
`application/run_registry.py`, `adapters/feishu/channel.py`,
`entrypoint/app.py`.

## Big picture

Three flows meet at the `Outbox`:

- **Inbound** (user → Claude): IM event → Channel → command split or
  free-text dispatch → `Multiplexer.send_keys` to the bound tmux pane.
- **Claude change detection**: `JsonlWatcher` polls the transcript
  file every 2 s, parses new bytes into `TranscriptEvent`s, and emits
  them tagged with `run_id`.
- **Outbound** (Claude → user): `Dispatcher._handle_transcript_event`
  resolves bindings for the run, renders blocks, applies verbosity
  + echo filtering + tool_use/tool_result pairing, then enqueues
  `Outbound`s on the per-Person `Outbox`. The `Outbox` worker calls
  `Channel.send / edit / delete`.

`MessageSeqService` is hooked **inside the Outbox enqueue path**, so
every send/edit gets stamped at submission time and the worker sees
a pre-stamped Outbound.

## ASCII diagram

```
                            INBOUND  (user → Claude)
 ┌────────┐  IM event   ┌─────────────┐  Inbound   ┌─────────────────┐
 │  User  │────────────▶│   Channel   │───────────▶│ AllowList.guard │
 │ (Lark) │             │ (FeishuCh.) │            └────────┬────────┘
 └────────┘             └──────┬──────┘                     │
                               │ /cmd (split_command)       │ free text
                               │                            ▼
                               │                   Dispatcher._handle_inbound
                               │                            │
                               ▼                            │  RunRegistry.get_pane
                       command handlers                     │
                       (sessions, ask_user,                 ▼
                        screenshot, history,         EchoDedup.record
                        directory, voice,            Multiplexer.send_keys
                        usage, server, livepane)     (tmux pane, →Claude)


                            CLAUDE CHANGE DETECTION
                                       │
                              Claude writes JSONL
                                       │
                                       ▼
                       ┌─────────────────────────────┐
                       │       JsonlWatcher          │
                       │ poll every 2s, byte cursor, │
                       │ truncation reset, /clear    │
                       │ replay of unanswered tools  │
                       └────────────┬────────────────┘
                                    │ (run_id, TranscriptEvent)
                                    ▼
                            OUTBOUND  (Claude → user)
                       ┌─────────────────────────────┐
                       │ Dispatcher._handle_         │
                       │   transcript_event          │
                       │   ├ bindings = registry.    │
                       │   │    find_bindings_for_run│
                       │   ├ render_block            │
                       │   ├ verbosity.maybe_truncate│
                       │   ├ USER-echo drop          │
                       │   │   (vs EchoDedup)        │
                       │   └ tool_use ↔ tool_result  │
                       │       Future pairing        │
                       └────────────┬────────────────┘
                                    │ Outbound (TextContent / CardContent / Doc)
                                    ▼
                       ┌─────────────────────────────┐
                       │           Outbox            │
                       │  ┌── enqueue_send ─────┐    │
                       │  │  seq.stamp_send ←───┼────┼── MessageSeqService
                       │  │  → footer appended  │    │   (counter per-conv,
                       │  └── enqueue_edit ─────┤    │    chain by msg_id,
                       │     seq.stamp_edit  ──▶│    │    toggle per-person)
                       │  per-Person FIFO queue │    │
                       │     + worker task      │    │
                       └────────────┬───────────┘    │
                                    │  Channel.send / edit / delete
                                    ▼                          ▲
                       ┌─────────────────────────────┐         │
                       │   Channel (FeishuChannel)   │         │
                       │  – inline-refresh for click │         │
                       │  – patch_card_message       │         │
                       │  – upload_image for docs    │         │
                       └────────────┬────────────────┘         │
                                    │ message_id               │
                                    ▼                          │
                              IM (Lark/Feishu)                 │
                                    │                          │
                                    └─ anchor.message_id ──────┘
                                       seq.record_send_anchor
                                       (binds seq → message_id
                                        so future edits chain)
```

## Seq lifecycle in the call sequence

```
enqueue_send(p, outbound)
    └─ seq.stamp_send → (outbound', seq=N)              # counter++ now
    └─ queue.put(_SendTask(outbound', future, seq=N))
                                                        # worker may
                                                        # be far behind
worker._process(_SendTask)
    └─ channel.send(outbound')  ──▶ anchor (message_id)
    └─ seq.record_send_anchor(anchor, N)                # binds N → msg_id
    └─ future.set_result(anchor)

later: enqueue_edit(p, anchor, outbound2)
    └─ seq.stamp_edit reads chain[anchor.message_id]    # ← only finds N
                                                        #   if record_send
                                                        #   already ran
    └─ chain.append(M); footer = compact(chain)
```

The footer collapses consecutive runs into ranges to save space: a
chain that's one unbroken run renders `_seq #3–#6_` (no bracket), and
a gapped chain renders `_seq #9 [#3–#5 → #9]_` (ranges joined by `→`,
en-dash inside a range). Gaps come from a `DocumentContent` that
consumed a seq it couldn't stamp. See `message_seq._format_footer`.

Counter scope: per-conversation `(chat_id, thread_id_or_empty)`.
Toggle scope: per `(person, conversation)`. State is in-memory only,
resets on restart.

## Likely sources of "sequence glitches"

Three structural ones to confirm before patching:

### 1. Stamp time ≠ wire time

`stamp_send` allocates the seq at *enqueue*; `channel.send` happens
later in the per-Person worker. Within one conversation, seqs stay
monotonic (stamp is sync, allocation is FIFO with enqueue order),
and the per-Person queue drains FIFO — so wire order matches stamp
order **for that Person**.

But: counters are per-conversation, while queues are per-Person. A
Person bound to two conversations has interleaved drains and two
independent counters; #5-in-chat-A and #5-in-chat-B are different
messages. Not a bug, just the namespace.

### 2. Edit-before-send-completes

`record_send_anchor` only runs **after** `channel.send` returns. If
a tool_use send is enqueued and the matching tool_result edit
follows fast, `stamp_edit` could execute before the chain root has
been recorded.

The dispatcher gates this: `_dispatch_tool_result` does
`await self._await_anchor(future)` before `enqueue_edit`, so by the
time the edit is stamped the send has finished and the anchor is
registered. Verified by reading dispatcher.py:254.

Failure mode if that gating is ever bypassed: `stamp_edit` finds no
chain in `_chains[anchor.message_id]`, silently roots a new chain at
the edit's seq, and the user sees a single-entry footer instead of
`#N → #M`. The bug then *hides itself* in the footer.

### 3. Fan-out + image gaps

One TranscriptEvent → N bindings → N `enqueue_send` calls → N stamps.
Each binding has its own conversation key, so each gets its own
counter — fine.

`DocumentContent` (screenshots) consumes a seq that
`_with_footer` can't render (no text slot on an image card), so the
counter advances but no footer is visible. Visibly looks like
"message #7 went missing." Documented as known limitation in
`message_seq.py:30`, but worth re-stating: gaps in displayed seqs
are expected when a `/screenshot` lands between text messages.

## Other observations worth knowing

- **Echo filter is conservative**: `_all_echos` requires *every*
  binding's pane to have a recent matching send_keys. If even one
  binding doesn't, the message is forwarded to all. Trade: one
  duplicate echo in some panes, vs. risk of swallowing a real
  tmux-typed prompt.
- **Inline-refresh** in `FeishuChannel.edit` redirects an edit into
  the click response when the edited card is the one just clicked —
  important to remember when reasoning about "did this edit hit the
  PATCH path or the click-ack path?" Both end up calling
  `enqueue_edit`, both go through `stamp_edit`, but only the PATCH
  path actually mutates the message at Feishu.
- **`_chains` leak** on `delete`: no `clear_anchor` hook today, so
  deleted-card chain entries stay until process exit. Bounded
  growth, but unbounded over time.

## Quick instrumentation idea

If a real glitch needs localizing, add `logger.info` lines in
`MessageSeqService` at three points:

```
stamp_send:           (chat, thread, seq=N, kind=send)
record_send_anchor:   (msg_id, seq=N, action=bind)
stamp_edit:           (chat, thread, msg_id, chain_before, seq=M, chain_after)
```

That gives a per-msg ledger to diff against what actually rendered
in Lark — fastest way to localize the symptom (gap, wrong chain,
out-of-order #).
