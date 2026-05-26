# paige configuration — design of record

This is the canonical inventory of every knob paige reads. Three
layers, three lifecycles, three audiences:

| Layer | Source | Scope | Reload | Audience |
|---|---|---|---|---|
| **1. Startup env** | `~/.paige/.env` → `os.environ` → `Config.from_env()` | process-wide | `prod.sh restart` | operator |
| **2. Static TOML** | `~/.paige/hosts.toml` → `load_hosts_toml()` → `HostsService` | process-wide | `prod.sh restart` (SIGHUP deferred to multi-host Step 12) | operator |
| **3. Runtime prefs** | `/session → ⚙ Prefs` IM buttons → in-memory services | per-(person, conversation) | not persisted; reset on restart | end user |

Layers 1–2 are owned by whoever runs `prod.sh`; layer 3 is owned by
each end user inside their own threads. Layer 3 deliberately does not
persist — every pref is cheap to re-toggle and a restart is rare.

```
                 ┌──────────────────────┐
   operator ───→ │  ~/.paige/.env       │ ── load via dotenv ──→ os.environ ──→ Config.from_env() ─→ Config dataclass
                 └──────────────────────┘                                                              │
                 ┌──────────────────────┐                                                              │
   operator ───→ │  ~/.paige/hosts.toml │ ── load_hosts_toml() ─────────────────────→ HostsService ←──┘
                 └──────────────────────┘
   end user ───→ /session ⚙ Prefs ─→ MessageSeqService / VerbosityService / CollapsePrefService  (in-memory)
```

## Layer 1 — startup env vars

Single source of truth for this table is `Config.from_env` in
`src/paige/entrypoint/config.py`. `env.example` at the repo root is
the install-template companion — keep the two in sync on every
addition (deliberate two-source-of-truth: doc is the inventory,
template is what users `cp` to `~/.paige/.env`).

### Feishu credentials

| Var | Default | Required | Consumer |
|---|---|---|---|
| `PAIGE_FEISHU_APP_ID` | — | yes | `LarkClientWrapper` |
| `PAIGE_FEISHU_APP_SECRET` | — | yes | `LarkClientWrapper` |
| `PAIGE_FEISHU_DOMAIN` | `https://open.feishu.cn` | no | `LarkClientWrapper`. Set to `https://open.larksuite.com` for Lark International. |
| `PAIGE_FEISHU_GROUP_ID` | empty | no | Operator-declared topic-mode group `chat_id` (`oc_…`). Not enforced at runtime — bindings key on whatever `topic_id` the parser reports — but logged at startup so the intent is discoverable. Create the group with `chat_mode=group` + `group_message_type=thread` (Lark "话题模式群"). |

### Access control

| Var | Default | Required | Consumer |
|---|---|---|---|
| `PAIGE_ALLOWED_USERS` | empty (open) | no, but **strongly recommended** | `AllowList`. CSV of `user_id`s. Empty = anyone the bot can hear from. Find your `user_id` in `paige.log` after a test message. |
| `PAIGE_ADMIN_USERS` | empty | no | `AdminList`. CSV of `user_id`s allowed to run admin commands (currently `/server`). Empty = every allowed user is admin (fine for solo). |

### Filesystem

| Var | Default | Required | Consumer |
|---|---|---|---|
| `PAIGE_DIR` | `~/.paige` | no | state directory — see "PAIGE_DIR layout" below |
| `PAIGE_PROJECTS_ROOT` | `~/projects` | no | `DirectoryService` + `/sessions → 🆕 New`. Immediate subdirs become picker entries. |

### Tmux

| Var | Default | Required | Consumer |
|---|---|---|---|
| `PAIGE_TMUX_SESSION` | `paige` | no | `TmuxMultiplexer.default_session`. The session new panes spawn into. |

### Polling intervals

| Var | Default | Required | Consumer |
|---|---|---|---|
| `PAIGE_STATUS_INTERVAL` | `1.0` (s) | no | `StatusService` + `InteractiveUIService`. Lower = snappier card updates, higher = less CPU. |
| `PAIGE_WATCHER_INTERVAL` | `2.0` (s) | no | `JsonlWatcher`. JSONL transcript poll interval. |

### Voice transcription (optional — Feishu transcribes client-side, so this is rarely needed)

| Var | Default | Required | Consumer |
|---|---|---|---|
| `PAIGE_OPENAI_API_KEY` | — | only when voice forwarding desired | `OpenAITranscriber`. Unset = audio inbounds get a "not configured" hint. |
| `PAIGE_OPENAI_BASE_URL` | `https://api.openai.com/v1` | no | `OpenAITranscriber`. Override for OpenAI-compatible proxies. |

## Layer 2 — `~/.paige/hosts.toml`

Multi-host registry (`doc/multi-host.md` Step 8). LOCAL is always
synthesised by `HostsService` — don't list it. The SSH adapter
(Steps 9–10) hasn't shipped yet, so remote entries surface as
"disconnected" placeholders in `/sessions` and `/server`.

```toml
[[host]]
id   = "dev-1"                  # required, non-empty, NOT "local"
name = "Dev box"                # optional display label; falls back to id
ssh  = "user@dev-1.lan:22"      # optional; anything ssh(1) accepts (or ~/.ssh/config alias)
```

| Field | Required | Default | Consumer |
|---|---|---|---|
| `id` | yes | — | `Host.host_id` — persistent routing key in `Binding` / `RunPointer` |
| `name` | no | empty (falls back to `id`) | `Host.display_name` — used in card badges + chooser rows |
| `ssh` | no | empty | `Host.ssh` — destination string; consumed by the future SSH multiplexer/watcher adapters |

Loader behaviour (`load_hosts_toml`):

- Missing file → `[]` (silent — common case).
- Parse error / non-list `host` key → `[]` + warning.
- Entry without `id`, with `id = "local"`, or with a duplicate `id` →
  skipped + warning (other entries still load).

## Layer 3 — runtime prefs

Per-(person, conversation) toggles set via `/session → ⚙ Prefs`. All
in-memory; resets on every restart.

| Service | Pref | Default | Cycle / values | Toggle |
|---|---|---|---|---|
| `MessageSeqService` | `_seq #N_` debug footer on outgoing messages | off | on / off | Prefs sub-pane button |
| `VerbosityService` | BRIEF vs FULL per content kind (text / tool_use / tool_result) | FULL for all kinds | per-kind FULL / BRIEF | Prefs sub-pane button per kind |
| `CollapsePrefService` | Body-line threshold for `collapsible_panel` wrapping | `25` lines | `25 → 50 → 100 → 0(off) → 25` | Prefs sub-pane button (cycles) |

Why no persistence: every pref is one tap to restore, restarts are
operator-initiated and infrequent, and persisting introduces
schema-versioning overhead disproportionate to the value. Revisit
only if a power user is restoring 10+ prefs per restart.

## PAIGE_DIR layout — four buckets

Four buckets, four owners, four lifecycles:

```
~/.paige/
├─ .env                           ← config — user-edited
├─ hosts.toml                     ← config — user-edited
│
├─ venv/                          ← artifact — prod.sh owns
├─ wheels/                        ← artifact — prod.sh owns (drop wheels here for `prod.sh upgrade`)
│
├─ state/                         ← state — paige's persistent memory (survives restart)
│  ├─ run_registry.json             (bindings + RunPointers — RunRegistry)
│  └─ jsonl_watcher_state.json      (watcher offsets — JsonlWatcher)
│
└─ runtime/                       ← runtime — process telemetry (one process lifetime)
   ├─ paige.log                     (log stream — `tail -f` this)
   └─ paige.pid                     (process anchor — managed by prod.sh)
```

| Bucket | Owner | Survives restart? | Safe to edit while paige runs? |
|---|---|---|---|
| top-level (config) | user | yes | yes — re-read on restart only |
| `venv/`, `wheels/` (artifact) | `prod.sh` | yes | no — managed by upgrade |
| `state/` | paige | yes | **no** — corrupts state |
| `runtime/` | paige + `prod.sh` | log truncated/appended; pid regenerated | log is safe to rotate; pid is `prod.sh`'s anchor — don't touch |

To reset state: stop paige first, delete `state/*.json`, restart. paige
re-derives bindings from `~/.paige/state/` (gone → empty registry → no
auto-rebinding) and re-discovers running panes via `RunDiscovery`.

### Pending: split state + runtime out of the top level

Today's deployments still have all six files (`paige.log`, `paige.pid`,
`run_registry.json`, `jsonl_watcher_state.json` plus `venv/`, `wheels/`)
sitting next to `.env` and `hosts.toml`. The four-bucket layout above is
the **target**. Migration is a small follow-up:

- `scripts/prod.sh` repoints `LOG_FILE` → `$PAIGE_DIR/runtime/paige.log`
  and `PID_FILE` → `$PAIGE_DIR/runtime/paige.pid`. One-time `mv` block
  at start handles existing deployments.
- `src/paige/adapters/storage.py` (`FileStorage`) writes into
  `$PAIGE_DIR/state/` instead of `$PAIGE_DIR/`.
- `src/paige/adapters/jsonl_watcher.py` state file path moves under
  `state/` (same instance as `FileStorage`, so this falls out for free
  if it already routes through Storage; otherwise update the path
  constant).
- `src/paige/entrypoint/main.py` — `mkdir -p` both subdirs on startup
  in addition to `paige_dir` itself.

Captured here so it's not lost; pick up when the next config-touching
slice lands.

## Adding a new env var

1. Add the field to `Config` in `src/paige/entrypoint/config.py`.
2. Parse it in `Config.from_env`.
3. Add a row to the right table above.
4. Add a commented-out entry to `env.example` showing the default
   (or `REQUIRED` for required-when-X vars).
5. Wire it through `build_app` / `assemble` to its consumer.

## Adding a new runtime pref

If a new toggle should survive across users in a thread → put it in
its own service shaped like `MessageSeqService` /
`CollapsePrefService` (per-(user, conversation), in-memory dict).
Add a button on the `/session → ⚙ Prefs` sub-pane. Add a row to the
Layer-3 table. Don't reach for env vars — Layer 1 is for operator
deploy-time concerns; user UX prefs belong in Layer 3.
