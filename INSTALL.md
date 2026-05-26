# paige — install on a fresh host

This guide assumes you've extracted the release tarball:

```
paige-release/
├── paige-X.Y.Z-py3-none-any.whl
├── prod.sh
├── env.example
├── SHA256SUMS
└── INSTALL.md  (this file)
```

If you only have the wheel and `prod.sh`, the rest of the bundle is
nice-to-have; only `prod.sh` and the wheel are strictly required.

## Verify integrity (optional, recommended)

Two checksum files ship with each release:

- **`paige-release.tar.gz.sha256`** — sits next to the tarball.
  Verify before extracting:
  ```bash
  sha256sum -c paige-release.tar.gz.sha256
  # → paige-release.tar.gz: OK
  ```
- **`SHA256SUMS`** — lives inside the bundle after extract.
  Covers every file:
  ```bash
  cd paige-release/
  sha256sum -c SHA256SUMS
  # → paige-X.Y.Z-py3-none-any.whl: OK
  # → prod.sh: OK
  # → INSTALL.md: OK
  # → env.example: OK
  ```

The wheel ships with its own `paige-X.Y.Z-py3-none-any.whl.sha256`
sidecar in the build output too, for single-wheel-only handoffs.

If verification fails, **don't install** — the artifact has been
modified or corrupted in transit. Re-fetch from the source.

## Prerequisites

| Need | Why | Check |
|---|---|---|
| Python ≥ 3.12 | paige's runtime | `python3 --version` |
| `tmux` | the multiplexer paige drives | `tmux -V` |
| `claude` CLI | the agent paige bridges to | `claude --version` |
| `uv` (recommended) or `python3 -m venv` | venv creation | `uv --version` |
| 100 MB disk under `~/.paige` | wheels + state | `df -h ~` |

On Debian/Ubuntu: `apt-get install -y tmux`. On macOS:
`brew install tmux`. Process introspection (`psutil`) is bundled in
the wheel — no system-binary deps for run discovery.

paige does **not** require a system Python install of itself —
`prod.sh` creates `~/.paige/venv` and installs into that.

## First-time install

```bash
# 1. Stage the bundle on the host (wherever you extracted it).
cd paige-release/

# 2. Set up the state directory + your env file.
mkdir -p ~/.paige
cp env.example ~/.paige/.env
$EDITOR ~/.paige/.env   # fill in PAIGE_FEISHU_APP_ID + PAIGE_FEISHU_APP_SECRET

# 3. Install the wheel + start the bot.
./prod.sh upgrade ./paige-X.Y.Z-py3-none-any.whl

# 4. Confirm.
./prod.sh status
./prod.sh logs        # tails ~/.paige/paige.log
```

`prod.sh upgrade` does five things atomically: snapshot the previous
wheel for rollback, SIGTERM the running bot (15 s grace), install
the new wheel, start it, and verify `App started` shows up in the
log within 30 s. If health check fails it auto-rolls back.

## Configuration

Edit `~/.paige/.env`. Required keys: `PAIGE_FEISHU_APP_ID` and
`PAIGE_FEISHU_APP_SECRET`.

After editing the env, restart paige:

```bash
./prod.sh restart
```

`prod.sh restart` re-sources `~/.paige/.env` and re-spawns the
process — the running bot doesn't pick up env changes
automatically.

See `env.example` (the file you copied to `~/.paige/.env`) for every
supported key with comments.

## Day-to-day operations

| Command | Effect |
|---|---|
| `./prod.sh status` | Wheel version, PID, uptime, log path, where the venv lives. |
| `./prod.sh start` | `nohup paige > ~/.paige/paige.log` & write `~/.paige/paige.pid`. |
| `./prod.sh stop` | SIGTERM with 15 s grace → SIGKILL escalation if it doesn't drain. |
| `./prod.sh restart` | `stop` → `start`. Picks up env changes. |
| `./prod.sh logs [-f]` | Tail `~/.paige/paige.log`. `-f` to follow. |
| `./prod.sh upgrade <wheel>` | Snapshot → graceful stop → install → start → health-check → auto-rollback on failure. |

## Upgrade

When a newer wheel arrives (typically `paige-X.Y.Z-py3-none-any.whl`
in another release tarball):

```bash
./prod.sh upgrade ./paige-X.Y.Z-py3-none-any.whl
```

Same flow as the first install. The previous wheel survives at
`~/.paige/wheels/.previous/` for one-step rollback. The last 3
wheels in `~/.paige/wheels/` are kept; older ones get pruned.

## Rollback

If a new release misbehaves and you need to retreat to the prior
wheel:

```bash
ls ~/.paige/wheels/                                   # see what's available
./prod.sh upgrade ~/.paige/wheels/.previous/paige-*.whl
```

The auto-rollback inside `prod.sh upgrade` handles the case where
the new wheel fails its health check; manual rollback is for when
the bot started cleanly but the new behaviour broke something
you only notice later.

## Where everything lives

| Path | Contents |
|---|---|
| `~/.paige/venv/` | Python virtualenv (paige + deps). |
| `~/.paige/paige.log` | Persistent log; survives reboots. |
| `~/.paige/paige.pid` | Running PID; written by `prod.sh start`. |
| `~/.paige/.env` | Backend credentials + tunables. |
| `~/.paige/wheels/` | The last 3 wheels (newest = currently installed). |
| `~/.paige/wheels/.previous/` | One-step rollback target. |
| `~/.paige/run_registry.json` | Conversation ↔ tmux pane bindings. |
| `~/.paige/message_seq.json` | Per-(person, conversation) seq stamping toggle. |
| `~/.paige/jsonl_watcher_state.json` | Byte offsets per tracked transcript. |

## Troubleshooting

- **`./prod.sh status` says "not running" but I just started it** —
  check `~/.paige/paige.log`. The most common cause is a missing
  required env var (the bot logs the `ConfigError` and exits before
  it can write its PID).

- **Auto-rollback fired and I want to know why** — `~/.paige/paige.log`
  has the failing-version startup attempt. Look for the line just
  before "App stopping" — usually a stack trace.

- **Bot stopped responding to messages** — `./prod.sh logs -f` while
  you send a test message. If you see the inbound logged but no
  outbound, paige is bound to a pane that's gone (`/sessions` will
  let you re-bind).

- **`tmux: command not found` after fresh install** — paige drives
  tmux via libtmux; the binary still has to be present. `apt install
  tmux` (or your distro's equivalent) and restart paige.
