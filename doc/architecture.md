# Paige — architecture decisions

Reference for why the layout looks the way it does. Read this before
adding code.

## Anti-patterns this design avoids

These are pitfalls every long-lived bot codebase grows into unless
fenced off from the start. The architecture below is shaped against
them.

1. **God modules.** A single multi-thousand-line file mixing session
   lifecycle, callback dispatch, command handlers, content
   forwarding, IM business logic, lifecycle hooks. Anything that
   needs to happen ends up there.

2. **Module-level mutable state.** Globals like `_user_queues`,
   `_status_msg_info`, `_pane_state`, `_interactive_msgs`,
   `_modes`, `_keyboards`, plus singleton `config` / adapter
   registries. Tests have to remember to reset each one between
   runs.

3. **Implicit dependencies.** Functions reach for `session_manager`,
   `tmux_manager`, `config`, `get_adapter()` from anywhere. No DI —
   wherever you are, you can summon any service. Layering becomes
   impossible.

4. **Incoherent domain.** When a "session" can mean a JSONL file, a
   tmux window, a topic binding, OR an active conversation —
   depending on the function — every refactor breaks something.

5. **Tests entangled with real I/O.** Unit tests that need a real
   tmux server, real fs, real adapter fakes set via a global
   registry. State leaks between tests; `_reset_for_tests()`
   functions scattered everywhere.

6. **Protocols that grew organically.** Methods added one at a time
   for one backend's quirk leak through to every backend. The port
   stops being an abstraction.

7. **Multiple async loops sharing state, no synchronization.**
   Most subtle bugs trace back to "two loops both touched dict X
   without a lock." Fixes come as additional state machines, not
   as the missing primitive.

8. **Wire-order ≠ render-order.** The bot has no signal for what
   the user actually sees. Sequencing assumptions made at the wire
   level break on slow clients. Better to design surfaces that are
   order-independent than to layer fixes onto a fragile sequence.

## Lessons from IM development

Things worth baking in from the start.

- **Cards over posts.** Patches are visible; deletes leave
  tombstones. Plan for in-place mutation.
- **But cards stay where they were sent.** Patching never
  relocates. Position-at-bottom requires delete + re-create.
- **Heartbeats mask render lag.** A single patch on a slow client
  loses races; periodic re-assertion eventually settles.
- **Pane parsing is fragile.** TUI versions change. Multiple
  observations + a state machine + asymmetric thresholds beat
  any single-frame decision.
- **IM adapters have edge-case quirks.** Feishu dedups identical
  patches, has 24 h edit windows, can't patch image messages,
  rejects cross-type patches. *Those quirks must stay inside the
  adapter — the Channel port stays backend-agnostic.*
- **Multi-loop coordination needs explicit primitives.**
  `asyncio.Queue` for FIFO, explicit synchronization points
  (`queue.join()`, `flush()`), no hidden shared state.
- **Self-echo dedup is unavoidable.** Forwarding a user prompt
  through a backend that re-emits it via JSONL/transcript means
  you'll see it twice unless you mark + drop.
- **Test against a mock claude.** Most coordination bugs aren't
  unit-testable; they need an end-to-end fixture that produces
  realistic JSONL streams.
- **Two-environment discipline.** Code env (where we work) ≠ dev
  env (where we test) ≠ prod (where it runs). Tests never touch
  production credentials.

## Design decisions

How paige addresses each.

### A. No module-level mutable state

State is held by service objects, dependency-injected from the
composition root. A test instantiates fresh services with fakes;
no `_reset_for_tests` functions, no `set_x_for_tests` registries.

### B. Strict layering, enforced by `import-linter`

```
entrypoint           composition root, only place that crosses layers
   ↓
application + adapters       (siblings, neither imports the other)
   ↓
ports               Protocol interfaces — the only contract between app and adapter
   ↓
infrastructure | domain      both leaves, neither imports up
```

Lower never imports higher. `application` MUST NOT import
`adapters` directly — only via `ports`. Enforced in CI.

### C. Async coordination through explicit primitives

Each service owns its own loop / task. Cross-service work goes
through:
- `asyncio.Queue` for FIFO, with `task_done()` discipline.
- `asyncio.Event` for "wait until ready" handshakes.
- Explicit `on_change` callbacks registered at the composition root
  (e.g. `status_service.on_change(status_carrier.on_status_change)`)
  for one-to-one fan-out.

No shared mutable dicts. If two services need to look at the same
data, they hold the same `State` object via DI — and that object
has its own locking.

### D. Explicit lifecycle

Services implement `start() / stop()`. The composition root starts
in dependency order, stops in reverse. No background tasks
spawned in module `__init__`.

### E. Content-first model

Everything that flows is a `Message`. Inbound from a user, outbound
to a user, status, content from Claude — all are `Message`
subtypes. Messages flow through pipelines: ingest → filter →
render → deliver. Status is just a different kind of `Message`,
not a separate worker.

### F. Adapter quirks isolated

Feishu's render-lag mitigation and edit-window expiry are
handled inside the adapter, never exposed to application code.

### G. Concurrency model

One process. One asyncio loop. Per-loop tasks for:
- `Watcher` — JSONL transcript watcher.
- `Multiplexer` — tmux pane operations.
- `Channel` — IM adapter (one per backend).
- `Dispatcher` — the application service that ties them together.

Cross-task communication is `asyncio.Queue` or DI'd state objects
with their own locking. NEVER shared mutable dicts.

## Naming

Domain types live in `paige.domain`:
- `Person` — a human user.
- `Conversation` — chat + optional thread.
- `Anchor` — a pointer to a sent message (for edits / deletes).
- `Inbound` — a received message.
- `Outbound` — a message we want to send.
- `Card` + `Action` — interactive surfaces and the buttons on them.
- `ActionEvent` — a button press.
- `Run` — one Claude conversation.
- `Pane` — one unit of multiplexing (a tmux window in libtmux terms).
- `Binding` — a (Person, Conversation) → pane_id mapping.
- `Transcript` + `TranscriptEvent` + `Block` — the JSONL stream.

Ports live in `paige.ports`:
- `Channel` — IM-side comms.
- `Multiplexer` — pane operations.
- `Watcher` — transcript file watching.
- `Storage` — atomic JSON persistence.
- `Transcriber` — speech-to-text (optional; only the OpenAI adapter today).
- `ProcPrimitives` — process inspection for run discovery (open task-dir
  fds, exit status); `psutil` adapter on Linux + macOS.

Adapter implementations get their own subpackage:
`paige.adapters.feishu.FeishuChannel`,
`paige.adapters.tmux.TmuxMultiplexer`,
`paige.adapters.jsonl_watcher.JsonlWatcher`,
`paige.adapters.openai_transcriber.OpenAITranscriber`.

## Testing strategy

- `tests/unit/` — fast, no I/O. Pure dataclasses, port fakes,
  no real fs / tmux / network. Default `pytest` runs only these.
- `tests/integration/` — real tmux + real fs in a tempdir. Slow,
  opt-in via `pytest tests/integration`.
- `tests/e2e/` — full pipeline driven by mock_claude.
  Opt-in via `pytest tests/e2e`.
- No live IM. Period. Adapters are tested against backend stubs;
  no real Feishu credentials in tests.

## Sibling design notes

- [`doc/topic-mode.md`](topic-mode.md) — pane-per-topic routing
  via Lark Topic Mode Groups.
- [`doc/interactive-ui.md`](interactive-ui.md) — how pane-scrape
  detects Claude Code's TUI overlays, and how AskUserQuestion is
  handed off from `InteractiveUIService` to `LivePaneService`.
- [`doc/multi-host.md`](multi-host.md) — host-id plumbing for
  the (deferred) SSH multiplexer.
- [`doc/outbound-flow.md`](outbound-flow.md) — message lifecycle
  through the outbox.
- [`doc/message-seq-arch.md`](message-seq-arch.md) — sequence
  numbering for debug correlation.
- [`doc/config.md`](config.md) — env-var inventory.
- [`doc/upgrade.md`](upgrade.md) — wheel build + `prod.sh upgrade`
  lifecycle.
