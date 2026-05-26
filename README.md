# paige

Drive Claude Code from your chat app.

paige bridges a running `claude` CLI session (inside a `tmux` pane on
some host) to an IM thread. You read Claude's stream on your phone,
tap to answer permission prompts and option-pick questions, and
type replies that land in the pane as keystrokes. The pane keeps
running when your laptop is asleep — paige catches up the next
time you open the chat.

Feishu / Lark is the supported IM today. The `Channel` port keeps
the interface backend-agnostic, so other IMs can be added without
touching the rest of the codebase.

## What you get

- **Live streaming** of Claude's assistant text, tool calls, and
  tool results into the thread as they happen.
- **Coalesced fan-out cards** — a parallel agent launch renders as
  one `🤖 Agents` card that ticks off as each subagent finishes, and
  a burst of task-tool calls (`TaskCreate` / `TaskUpdate`) collapses
  into one live `📋 Tasks` checklist — instead of a card per call.
- **Inline option pickers** — when Claude asks you to pick from a
  list (`AskUserQuestion`), the options render as tappable
  buttons; tap one and it goes back as a keystroke.
- **Pane-scrape interactive UI** — Bash-permission prompts,
  edit/file permissions, `ExitPlanMode`, restore-checkpoint,
  Settings overlays — all rendered as buttoned cards even though
  Claude never lands them in JSONL.
- **Slash commands** — `/start` (pick a project and bind a fresh
  pane), `/sessions` (active / resume / archive / new chooser, with
  archive-and-restore and per-session transcript view), `/history`
  (paginated transcript), `/screenshot` (pane → image),
  `/usage` (token / spend), `/server` (admin overview), plus
  `/esc`, `/unbind`, `/help`.
- **Voice transcription** via OpenAI (optional; only needed for IM
  backends that don't pre-transcribe).
- **Topic-mode group routing** (optional) — instead of one direct
  message per pane, route each pane into its own Lark topic inside
  a shared topic-mode group. Cleaner when driving several panes
  at once.

## Requirements

- Linux or macOS host
- Python ≥ 3.12
- `tmux` (any recent version)
- [Claude Code CLI](https://github.com/anthropics/claude-code)
  installed and runnable as `claude`
- A Feishu / Lark workspace with a custom app (App ID + Secret)

## Quickstart

paige is in early release (0.1.0). Install is via a release wheel
plus a small lifecycle script.

```bash
# 1. Clone the repo
git clone https://github.com/Tao-Ma/paige
cd paige

# 2. Build the dev image (one-time)
./do.sh build

# 3. Build the wheel (runs inside the dev container)
./do.sh artifact --export ~/.paige/wheels/

# 4. First-time install on the host
cp env.example ~/.paige/.env
$EDITOR ~/.paige/.env   # fill in PAIGE_FEISHU_APP_ID + PAIGE_FEISHU_APP_SECRET
./scripts/prod.sh upgrade ~/.paige/wheels/paige-*-py3-none-any.whl

# 5. Confirm
./scripts/prod.sh status
./scripts/prod.sh logs -f
```

Full step-by-step (prerequisites, env reference, day-to-day ops,
rollback) lives in [`INSTALL.md`](INSTALL.md). The lifecycle script
auto-rolls back if a new wheel fails its health check.

### Topic-mode group (optional)

Run pane-per-topic in one shared Lark group instead of one DM per
pane. Two one-shot helpers seed the setup; both source
`~/.paige/.env` for credentials.

```bash
# Create the topic-mode group ("话题模式群") + add yourself.
# Prints PAIGE_FEISHU_GROUP_ID=oc_… for ~/.paige/.env.
~/.paige/venv/bin/python scripts/create_topic_group.py --name paige

# Add the printed line, then seed a permanent `general` topic.
# Idempotent — re-running is a no-op once seeded.
~/.paige/venv/bin/python scripts/seed_general_topic.py

# Restart so paige picks up the new env.
./scripts/prod.sh restart
```

Inside that group, the `@bot` mention requirement is dropped (the
group exists for bot interaction). New `/start` and `/sessions`
flows used from within a topic create topic-scoped bindings; old
DM bindings keep working. Full design notes —
mode-switching, key disambiguation, routing semantics, known Lark
quirks — live in [`doc/topic-mode.md`](doc/topic-mode.md).

## Status

Functional and live-tested against real Feishu + real Claude Code.
Pre-1.0 — the public surface (slash commands, ports, env keys) may
still shift before v1. See [`CHANGELOG.md`](CHANGELOG.md) for the
0.1.0 feature list.

## How it works

```
   Claude Code (in tmux pane)
        │ JSONL transcript          tmux key events
        ▼                                 ▲
   JsonlWatcher ─► Dispatcher ─► Outbox   │
                       │                  │
                       ▼                  │
                  FeishuChannel ◄── Inbound (your reply)
                       │                  ▲
                       ▼                  │
                    Lark / Feishu ────────┘
```

The architecture is layered (`domain` / `ports` / `adapters` /
`application` / `infrastructure` / `entrypoint`) with the layer
graph enforced by `import-linter`. Design decisions and rationale
live in [`doc/architecture.md`](doc/architecture.md) — read that
before adding code.

## Development

```bash
./do.sh build           # build the dev image (one-time)
./do.sh ci              # ruff + pyright + import-linter + pytest
./do.sh test            # pytest tests/unit (fast)
./do.sh test-all        # unit + integration + e2e
./do.sh shell           # interactive shell inside the dev container
```

Everything runs inside the `paige-dev` container — no host pip
install needed. Contribution guidelines live in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Layout

```
src/paige/
  domain/         pure data + rules; no I/O.
  ports/          Protocol interfaces.
  adapters/       concrete implementations of ports.
  application/    use cases that compose ports.
  infrastructure/ config, logging, lifecycle.
  entrypoint/     composition root + CLI.
tests/
  unit/           fast, no I/O. Default `pytest` runs only these.
  integration/    real fs / libtmux. Slow, opt-in.
  e2e/            full pipeline with mock_claude. Slow, opt-in.
```

## Credits

Thanks to [**six-ddc/ccbot**](https://github.com/six-ddc/ccbot) for
the core idea. The dual-channel pattern — reading Claude Code's
JSONL transcript for structured events alongside `tmux capture-pane`
for state Claude doesn't write to JSONL — was ccbot's first;
paige's `JsonlWatcher` + pane-scrape services are direct
descendants.

## License

MIT — see [`LICENSE`](LICENSE).
