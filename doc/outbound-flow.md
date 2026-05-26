# paige — outbound flow (Claude → IM)

How a transcript line written by Claude becomes a card in the user's
chat thread, end-to-end. Companion to [`architecture.md`](architecture.md)
(design intent) and [`message-seq-arch.md`](message-seq-arch.md)
(the debug seq footer that rides on top of this pipeline).

The IM-side (user → Claude) is straightforward — Channel inbound,
allow-list gate, RunRegistry lookup, EchoDedup record, Multiplexer
send_keys. The path that warrants a diagram is the other direction:
JSONL parsing, dispatcher routing, fan-out across bindings, and the
Outbox/Channel write. Most "messages went missing" or "messages came
in the wrong order" reports localize somewhere along this trunk.

## The picture

```
─── Discovery (every 10s) ─────────────────────────────────────────

   tmux pane.pid ──► proc_scan.discover_run
                        ProcPrimitives.get_open_task_uuids(pid)
                        Linux: walk /proc/<pid>/fd  ┐
                        macOS: lsof -d ^txt -p PID  ┘ → list of uuids
                                                   │
                                          (recurse one level of children
                                           when pane's foreground is the
                                           shell, not claude itself)
                                                   │
                                          tie-break by  <project>/<uuid>.jsonl
                                          mtime — handles claude --resume
                                          + /clear-leak multi-uuid cases
                                                   │
                                                   ▼
                                          (run_id, cwd, jsonl_path)
                                                   │
                                                   ▼
                            RunRegistry  ─── pane_id ↔ (run_id, cwd)
                                         ─── pane_id ↔ Bindings (Person, Conversation)
                                                   │
                                                   ▼
                            Watcher.track(run_id, jsonl_path)


─── Outbound trunk (transcript JSONL → IM card) ───────────────────

   ~/.claude/projects/<encoded-cwd>/<run_id>.jsonl
   (claude appends one line per event, then closes the fd)
                                                   │
                                                   ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ JsonlWatcher  (single task, poll_lock, every 2.0s)           │ ① ordering: per-run_id,
   │  for run_id, path in tracked:                                │    events emit in file order
   │     first read after track():                                │    (single poll task, one
   │       seed-scan [0, attach_size] for unanswered tool_use,    │     run_id at a time, lock
   │       then jump cursor past it                               │     held during whole pass)
   │     else read [offset, EOF]                                  │
   │     → JsonlParser.parse() → list[TranscriptEvent]            │
   │     → for ev: emit (run_id, ev) to handlers                  │
   │     persist offset to ~/.paige/jsonl_watcher_state.json      │
   └──────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼ (run_id, TranscriptEvent)
   ┌──────────────────────────────────────────────────────────────┐
   │ Dispatcher._handle_transcript_event                          │
   │   bindings = registry.find_bindings_for_run(run_id)          │ ★ if [] here, the whole
   │   if []: DROP                                                │   event is silently dropped
   │   for block in ev.blocks:           # in JSONL order         │   (this is the failure
   │     text = render_block(block)                               │    mode the proc_scan fix
   │     if role==USER and all panes echo'd: DROP                 │    was masking)
   │     for binding in bindings:        # serial, not gathered   │ ② ordering: blocks per
   │       _dispatch_block(...)                                   │    event, bindings per
   │                                                              │    block, all serial
   └──────────────────────────────────────────────────────────────┘
            │                  │                           │
            │ TOOL_RESULT       │ TOOL_USE & ask_user       │ TEXT / TOOL_USE / THINKING
            │ + tool_id         │  (slice 26)                │  (generic)
            ▼                   ▼                           ▼
   ┌──────────────────┐  ┌─────────────────────┐   ┌───────────────────────────┐
   │ pop tool_anchors │  │ parse_questions     │   │ header = "🔧 <tool_name>" │
   │ await its Future │  │ build_ask_user_card │   │   or None for TEXT        │
   │   (← waits for   │  │  (one button/option)│   │ apply verbosity           │
   │    matching      │  │ header = "❓ <q>"   │   │ CardContent(card)         │
   │    tool_use send │  │ enqueue_send →      │   │ enqueue_send → Future     │
   │    anchor)       │  │   stash Future      │   │ if TOOL_USE:              │
   │ header = "🔧 …"  │  │   by tool_id        │   │   stash Future by tool_id │
   │   or "❓ Answered"│  └─────────────────────┘   └───────────────────────────┘
   │ enqueue_edit     │           │                            │
   │   (anchor)       │           ▼                            ▼
   └──────────────────┘
            │                                                  ③ ordering: tool_use → tool_result
            ▼                                                     stays paired even when other sends
                                                                  interleave, because the edit awaits
                                                                  the Future from its own send.

   ┌──────────────────────────────────────────────────────────────┐
   │ Outbox  (per-Person asyncio.Queue + worker)                  │ ④ ordering: per-Person FIFO,
   │  optional MessageSeqService.stamp_send/_edit (off by default)│    max_in_flight==1.
   │  worker awaits one Channel call at a time                    │    IUIService overlay sends +
   │  resolves Future with returned Anchor                        │    StatusCarrierService badge
   │  on send: if seq stamped, record_send_anchor(anchor, seq)    │    PATCHes share this queue,
   │  fires on_send_complete handlers (StatusCarrierService)      │    so they interleave with
   └──────────────────────────────────────────────────────────────┘    content cards. Order across
                                                   │                   queues is per-Person — a
                                                   ▼                   2nd user is unblocked.
   ┌──────────────────────────────────────────────────────────────┐
   │ FeishuChannel                                                │ ⑤ inline-refresh hazard:
   │   send   → POST /messages/{thread}/reply  (interactive card) │    when an edit lands during a
   │   edit   → PATCH /messages/{anchor}                          │    button-click handler whose
   │     OR   ↳ click-response wrapper (if anchor == clicked card,│    anchor matches, the edit is
   │             via inline-refresh ContextVar)                   │    redirected from PATCH into
   │   delete → DELETE /messages/{anchor}  (leaves 撤回 tombstone)│    the click reply. Both go
   └──────────────────────────────────────────────────────────────┘    through stamp_edit, but only
                                                                        the PATCH variant mutates
                                                                        the message at the API.

─── Side paths (NOT through JSONL — pane scrape) ───────────────────

   StatusService           every 1.0s   tmux capture-pane → parse_status
                                        broadcasts (binding, status_text)
                                        events to registered handlers; no
                                        cards of its own.

   StatusCarrierService    subscriber   on StatusService event: PATCH the
                                        current carrier's `⏱ Worked Ns`
                                        badge. on Outbox.on_send_complete:
                                        strip badge from prior carrier
                                        (PATCH) + promote new anchor +
                                        stamp badge. No DELETEs anywhere.

   EndTurnPanelService     subscriber   on ReadinessService transitions:
                                        send the 4-input pick-or-type
                                        panel on READY; morph to receipt
                                        on user submit / tmux-typed text;
                                        morph header to "🟡 Working…" on
                                        tool_use NOT_READY. All edits via
                                        Outbox; the carrier service adds
                                        the live status badge on top.

   InteractiveUIService    every 1.0s   tmux capture-pane → detect overlay
                                        (Bash approval, Permission prompt,
                                         Exit plan mode, Restore checkpoint,
                                         Settings)
                                        send numbered-option or 3×3 arrow card
                                        yields to JSONL AskUserQuestion when
                                        both fire (richer data wins)
                                        feeds the SAME per-Person Outbox
```

## Tool-specific routing in `_dispatch_block`

`_dispatch_block` is the fan-out point (slice ② above). Most blocks
take the generic path — one card per block, with TOOL_USE→TOOL_RESULT
paired 1:1 (slice ③). Four tool families are special-cased *before*
the generic path, each firewalled in its own service so the 1:1 path
stays untouched for everything else:

- **`AskUserQuestion`** → `ask_user` buttoned card (existing).
- **`Agent` / `Task`** (subagent fan-out) → `AgentBatchService`. A
  *batch* — consecutive agent launches with no intervening non-agent
  block — coalesces into one `🤖 Agents` card; each launch appends a
  line, each agent's tool_result ticks it `⏳→✓`. A non-agent block
  closes the batch (next agent opens a fresh card); the `tool_id→line`
  map outlives the close so a late result still ticks its line.
- **`TaskCreate` / `TaskUpdate`** → `TaskTrackerService`. Rebuilds the
  task list from the ops (id from the TaskCreate *result*, status from
  the TaskUpdate *input*) into one `📋 Tasks` card per *group*. A new
  group (fresh card) opens when a create lands after the current
  group started executing (an update arrived); updates route to the
  group owning the task id. TaskUpdate results are swallowed.
- Everything else → generic per-block card.

Both coalescing services patch via the Outbox (out-of-band PATCH) —
transcript-driven, not click-driven, so they don't need the inline-
refresh slot (⑤). Card bodies are built through
`infrastructure.markdown_safe` so truncation never severs a code
fence, raw tool args/output render verbatim, and ATX headings in
prose are demoted; fenced chunks get the newline margin Lark needs in
`cards._body_elements` (else a `#` inside `bash` renders as a heading).

## Outbox bypass paths

Most outbound writes go Outbox → Channel (item ④). Three paths in
application code reach `Channel` directly, by design — if you're
adding a new send/edit, prefer the Outbox unless your case matches
one of these:

- **Inline-refresh edits during a click handler.** The handler must
  return its reply inside Feishu's callback window, so the edit
  can't queue behind unrelated traffic. Targets the clicked card's
  anchor and is redirected channel-side into the click-response API
  (item ⑤). Sites: `_sessions_context.py:101`, `ask_user.py:308`,
  `screenshot.py:139,167`, `interactive_ui.py:393` (overlay morph
  on pick), `live_pane.py:320,375` (key-press / text-submit
  auto-refresh — comment at `live_pane.py:307` calls out the slot
  explicitly).
- **Tight-poll edits from a live-streaming service.**
  `LivePaneService`'s poll loop edits the live buffer card on every
  capture; queuing through the per-Person Outbox would block behind
  unrelated traffic and let the buffer drift behind the pane. The
  loop owns ordering internally. Sites: `live_pane.py:262` (poll
  tick), `live_pane.py:275` (finalize on stop).
- **Acks** (`channel.ack`, ~100 sites). The click-reply toast, not
  a message edit. Fire-and-forget; not ordered against any send.

Direct `channel.send/edit/delete` from anywhere else creates a
hidden ordering bug between your write and whatever the Outbox is
draining for the same Person.

## Where disorder can sneak in

Ranked from "trust this invariant" to "watch out":

1. **Highest trust** — JSONL line order → Watcher emit order → Dispatcher
   block iteration → Outbox enqueue. All single-task or per-Person FIFO.
   If you ever see two text messages from one assistant turn arrive
   reversed, that's a real bug.

2. **Trusted but subtle** — tool_use ↔ tool_result pairing. The
   Future-await pattern guarantees the edit waits for the send anchor.
   *But* if the edit is enqueued before the send's Future resolves,
   the edit sits behind a `.await future` inside the dispatcher
   coroutine — and if another transcript event comes in during that
   wait, *its* `_dispatch_block` calls run in a different task. The
   dispatcher's per-event loop is serial across blocks; the Outbox is
   the only thing serializing across events. Worth knowing if you
   suspect ordering of concurrent tool exchanges.

3. **Mixed** — StatusCarrierService + EndTurnPanelService + IUIService
   all share the per-Person Outbox with the Dispatcher. Badge
   migrations (strip-old / stamp-new PATCH pairs) interleave with
   content sends. By design (one queue, FIFO), so order is
   whatever they enqueued in. The carrier service uses
   `suppress_hooks=True` on its own PATCHes so its own
   on_send_complete handler doesn't recurse on the migrations.

4. **The trap** — inline-refresh ContextVar (Feishu only). On a click
   handler, an edit to the clicked anchor is redirected from the PATCH
   API to the click-response API. Both work, but only the PATCH one
   mutates the persisted card — if a click handler kicks off a chain
   of edits, only the *first* one rides the click reply; the rest go
   PATCH. If something looks wrong right after a click, that's the
   place.

5. **The "only Status & AskUser" failure mode (resolved twice).**
   `bindings == []` at step ★ is *silent*; pane-scrape flows
   (StatusCarrierService badge migration, InteractiveUIService
   permission prompts) keep working without `find_bindings_for_run`,
   so the symptom is "I see the spinner badge and approval cards
   but no content / tool output." First occurrence: the original fd
   walk picked the lowest-numbered task-dir fd; `claude --resume`
   and `/clear` leaks left stale uuids in lower fd slots; fix was
   to collect every uuid and tie-break by JSONL mtime. Second
   occurrence: the fd walks were briefly dropped in favour of
   `~/.claude/sessions/<pid>.json` as the sole signal; Claude Code
   2.1.126 freezes that file's `sessionId` field at process start
   and never rewrites it, so any post-`/clear` discovery returned
   the dead uuid. Permanent fix: tasks-dir fd walk is the *only*
   discovery signal again, regression-tested via
   `tests/e2e/test_full_loop.py::test_run_discovery_survives_clear`
   (mock_claude grew a `__CLEAR__` sentinel that rotates the way
   real Claude does).

## How to localize a "missing message" report

Use the seq stamping toggle (`/session → ⚙ Prefs`, see
[`message-seq-arch.md`](message-seq-arch.md) for the chain semantics)
to get a footer on every send/edit. Then:

| Symptom | Likely step |
|---|---|
| No content cards, only Status / IUI overlays | step ★ — bindings=[] (run_id rollover, registry stale) |
| Card appears, then a tool_use card after it has the wrong tool name | step ② — block ordering inside one event |
| tool_result body shows up but doesn't replace the tool_use card in place | step ③ — Future not awaited, or anchor not registered |
| Cards from two different topics arrive interleaved | step ④ across-Person — expected; same-Person is the bug |
| Edit-after-click drops on the floor | step ⑤ — inline-refresh redirected the edit |
| Seq gaps (`#7` skipped) | usually `/screenshot` `DocumentContent` consuming a seq it can't render — see message-seq-arch.md |
