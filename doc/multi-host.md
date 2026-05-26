# Multi-host paige — design of record

Status: **local-host runtime is shipped; SSH adapters are planned.**
Today the host domain (`Host`, `HostsService`, `host_id` on
`Binding` / `RunPointer` / `RunRegistry` persistence), the
`MultiplexerRouter` + `WatcherRouter`, the `hosts.toml` loader,
both the `/sessions` and `/server` chooser redesigns, and the
multi-host `/sessions` overview UX all run with a single registered
`local` adapter. The SSH multiplexer + watcher adapters and the
per-host operational verbs (Connect / Disconnect / Reconnect) are
the next slices.

paige today operates on the same machine it runs on: one local tmux
server, one local `~/.claude/projects` tree, one local proc table.
This doc proposes extending that model to **multiple hosts** — each
host runs its own tmux + claude, paige bridges them through SSH —
without rewriting the inbound / outbound surfaces.

## Mental model

- **Host** is a first-class entity. Properties: `host_id`, `name`,
  connection (`Local` or `Ssh`), eventual runtime status.
- **Session** = tmux session on a host. Sessions on different hosts
  with the same name are unrelated.
- **Binding** is `conversation ↔ (host_id, pane_id)`. The host is part
  of the routing key, not a sidecar attribute.

`local` is always implicit — present even when the user has added
remotes; serves as the fallback for everything that isn't explicitly
remote.

## Hosts config — `~/.paige/hosts.toml`

Schema + loader behaviour (`load_hosts_toml`) live in
[`doc/config.md`](config.md#layer-2--paigehoststoml). The mental-model
points specific to multi-host:

- Read once at startup. `SIGHUP` reload (or a `🔄 Reload` button on
  `/server`) is deferred to Step 12.
- Read-only from paige's perspective — no Add / Edit / Remove verbs.
  Users edit the file via their normal editor / config-management
  workflow.
- `local` is always implicit; the loader silently drops a typo'd
  `local` entry.

## Data model — what changes in code

Status legend: ✅ landed · 🚧 partial · ⬜ pending.

| Where | Change | Status |
|---|---|---|
| `domain/host.py` | `Host(host_id, name)` + `LOCAL_HOST_ID` constant + synthetic `LOCAL` Host. The `connection` discriminated union (`Local` / `Ssh(uri)`) is deferred — it isn't needed until the SSH adapter lands. | ✅ |
| `application/hosts.py` | `HostsService.get(host_id) / list()`. Today only knows the synthetic `LOCAL`; future config loader will populate from `~/.paige/hosts.toml`. | ✅ |
| `domain/pane.py` | `Binding.host_id: str = "local"` (defaulted so existing persisted state loads). | ✅ |
| `application/run_registry.py` | Internal storage is `{topic_key: (host_id, pane_id)}`. `RunPointer.host_id` defaults to `"local"`. New methods: `get_host(person, conv)`, `get_binding(person, conv)`. State file gains `host_id` per entry; load backfills `"local"` on legacy entries. | ✅ |
| `ports/multiplexer.py` | Every Protocol method gains `host_id: str = LOCAL_HOST_ID` keyword-only. | ✅ |
| `application/multiplexer_router.py` | NEW: `MultiplexerRouter` impl of the Multiplexer Protocol — dispatches to per-host adapters, falls back to local on unknown ids. `assemble()` wraps the libtmux adapter in this router. | ✅ |
| `ports/watcher.py` | Same shape — `host_id` parameter on `track()`. | ⬜ |
| `application/watcher_router.py` | NEW: `WatcherRouter` analogous to `MultiplexerRouter`, with the lifecycle dance (`start`/`stop`/`flush` global; `track`/`untrack` per-host). | ⬜ |
| Every existing call site | Today: `host_id` defaults to `"local"`, so call sites are unchanged. Per-binding host-aware code passes `host_id=binding.host_id` at the call site (none yet — pending the SSH slice). | 🚧 |

The full SSH adapter slice is what makes `host_id != "local"` actually
do anything; until then, the param threads through but always lands on
the local adapter.

## Card surface — what users will see

Both `/sessions` and `/server` already use the chooser→sub-pane
shape that scales cleanly to remote hosts. The only delta when SSH
lands is *which entries the per-host views show* and *which buttons
appear* (Connect/Disconnect on disconnected hosts).

### `/sessions` chooser ✅

```
🔗 Sessions
───────────────
*Sessions* — *3 active* · 12 dormant
───────────────
[● Active (3)] [○ Resume (12)]
[🆕 New]       [🔄 Refresh]    [✕ Dismiss]
```

Five buttons in two rows. Each top-level category drills into a
sub-pane that lists its rows ordered by cwd, with a trailing
`◀ Back | 🔄 Refresh | ✕ Dismiss` row. The "many sessions"
density problem is now scoped to the per-category sub-pane the
user opted into.

When the SSH slice lands, the eventual host-overview is a layer
*above* this chooser — `/sessions` will list configured hosts when
≥2 are in `hosts.toml`, and tapping a host opens this same chooser
scoped to that host. Single-host installs (only `local`) keep the
current shape unchanged.

### `/server` chooser ✅

```
🖥 Server
───────────────
*paige* uptime 2h 14m · 12 panes · 312 MB
───────────────
[🖥 Hosts (1)]   [🪟 Panes (12)]
[💾 Storage]     [⚙ Process]
[🔄 Refresh]     [✕ Dismiss]
```

Six buttons in three rows. Drilldowns:

- **🖥 Hosts** — list of configured hosts (today: `local` only;
  future: remotes from `hosts.toml`). Tap a host → host-detail
  card. Local detail shows paige's own pid / uptime / rss; remote
  detail will show SSH probe results when that slice lands.
- **🪟 Panes** — one row per multiplexer pane. Tap → pane-detail
  card with a primary `⚠ Kill` button. Same shape as `/sessions`
  row-detail.
- **💾 Storage** — read-only paige/projects dir sizes + container
  memory.
- **⚙ Process** — read-only paige pid/uptime/rss/pane count.

The Hosts sub-pane is where future operational verbs land:

| Verb | When | Effect |
|---|---|---|
| 🟢 Connect | offline host | Open SSH control master |
| 🔌 Disconnect | online host | Close control master |
| 🔁 Reconnect | online host | Disconnect + Connect |
| 🔄 Probe | overview | Health check, no state change |

### Manage card — host badge ✅

```
*main*
● active · bound to this topic · 🖥 dev-1
_pane `@1` · run `abc12345` · `~/repos/paige`_
```

Landed. Single-line addition that only renders when ≥2 hosts are
configured (single-host installs skip the badge — `🖥 local` on
every card is noise). Routes to that host's multiplexer for
forwarded commands transparently. The five forwarded slash-commands
(`/clear /compact /cost /memory /model`) moved to a `🛠 Commands`
sub-pane in the same slice — Manage card density dropped from 10
buttons in 5 rows to 6 in 3.

## Operational verbs — eventual scope

Only these touch host runtime state. None edits config.

| Verb | Where | Effect | Status |
|---|---|---|---|
| 🟢 Connect | offline host card | Open SSH control master, mark host up | ⬜ |
| 🔌 Disconnect | online host card | Close control master; sessions on host become "stale, host offline" until reconnect | ⬜ |
| 🔁 Reconnect | online host card | Disconnect + connect | ⬜ |
| 🔄 Probe | overview cards | Health check, no state change | 🚧 (button wired, no SSH probe yet) |
| 🆕 Spawn here | per-host `/sessions` | `claude` in a new pane on that host (same shape as today's `/start`) | 🚧 (works for local; remote needs SSH multiplexer) |

## Transport plan (deferred)

**Recommendation: subprocess + ssh control-master.**

- Each operational call shells out: `ssh -S <ctl-socket> host tmux …`
- Control master kept open for the host's lifetime so individual ops
  don't pay TLS+auth handshake cost.
- Auth: relies entirely on the user's existing `~/.ssh/config` —
  passwordless via key/agent, BatchMode=yes. paige does NOT manage
  keys, passwords, or known_hosts.
- If `BatchMode=yes` fails, the host shows `✗ auth required`.
- Pros: no new pinned deps, integrates with whatever the user has
  already configured (jump hosts, IdentityFile, etc.).
- Cons: subprocess per op — slower than an in-process SSH library.
  Mitigated by control-master keepalive.

**Alternative considered:** `asyncssh` library, in-process. Faster,
more controllable, but adds a heavy dep and we'd need to reimplement
all the config-file plumbing the user already has working.

## Persistence

- `hosts.toml` — user-edited, read-only from paige.
- Host status (up/down, last_ok, ping) — in-memory only, rebuilt on
  startup. Persisted state would lie after restart.
- Bindings — disk-backed (existing `RunRegistry` storage). Migration:
  on load, entries without `host_id` are treated as `host_id="local"`.

## What stays local-forever

| Subsystem | Why it stays local |
|---|---|
| `Channel` | Talks to Lark from one place — not host-scoped. |
| `Outbox` | Per-Person queue; bound to channel, not to hosts. |
| `MessageSeqService`, `VerbosityService`, `SentLogService` (if revived) | Per-(person, conversation) state; topology-independent. |
| `EchoDedup` | Per-pane dedup; pane is identified by `(host_id, pane_id)` after the refactor, but the dedup data itself doesn't move. |

## Open questions (parked for SSH slice)

1. **Discovery on remote hosts** — `RunDiscovery` scans
   `~/.claude/projects` + procs. Naive port: `ssh host find ~/.claude/projects -name '*.jsonl'` + `ssh host ps` per tick. Polling overhead per host. Acceptable at single-digit host counts; revisit beyond that.
2. **Watcher strategy** — `tail -F` over SSH per active run vs. periodic full-file diff via SFTP. `tail -F` simpler but multiplies SSH connections; SFTP polling is more uniform. Likely `tail -F` with a connection cap.
3. **Failure semantics** — when a host disconnects mid-turn, what do bindings on it look like? Suspect: bindings stay (so reconnect rebinds automatically), but inbound forwards get a hint card "🖥 host offline — reconnect to continue".
4. **`local` always present** — even when the user lists only remotes? Yes, currently. Confirm before locking in.

## What's planned next

- **SSH multiplexer adapter** — subprocess + SSH control-master.
  First a read-only slice (`list_panes / find_pane / capture`),
  then write (`send_keys / create_pane / kill_pane`).
- **SSH watcher adapter** — `tail -F` over SSH per active run;
  remote run discovery via `ssh host find ~/.claude/projects` +
  `ssh host ps`.
- **Operational verbs** — `🟢 Connect / 🔌 Disconnect /
  🔁 Reconnect` on the Hosts sub-pane host-detail card; per-host
  status (online / offline / probing) populates `/server`'s Hosts
  listing.

Each slice is independently committable, independently shippable, and
verified by a live IM test before commit.
