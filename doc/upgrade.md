# Production upgrade flow

Two-environment split. Build + verify in the dev container; install
+ run on the host. The handoff is **one verified wheel file** —
nothing else moves between the two.

## Two environments

**Code env** (the host) — paige runs here at `~/.paige/venv/bin/paige`
against a real Feishu app. **Do not** run build/test here. Host pip
operations risk knocking the live bot over, and tests need fakes /
disposable processes that should never sit next to a production
install.

**Dev container** — `paige-dev-box`, driven by `./do.sh`. All build
and test work happens here. Real Feishu credentials never go in.
The smoke tests run under a dummy env so the bot never tries to
connect during build.

The handoff between the two is one wheel: `~/.paige/wheels/paige-x.y.z.whl`.

## Phase A — build + verify (dev container)

```bash
./do.sh artifact [--export <host-dir>]
```

Inside the dev container:

1. `uv build --wheel` → `dist/paige-x.y.z-py3-none-any.whl`.
2. Fresh venv at `/tmp/paige-artifact-venv`.
3. `uv pip install <wheel>[feishu]` into that venv.
4. Smoke checks:
   - `paige` script entry installs in venv `bin/`.
   - `paige.entrypoint.main` imports under dummy env.
   - `Config.from_env({...dummy Feishu creds...})` works.
   - Wheel content audit: required modules present, dev files
     (`tests/`, `doc/`, `scripts/`, `do.sh`, `Dockerfile.dev`,
     `paige.testing/`) excluded.
   - Wheel size sanity: < 1024 KB. paige is ~60 KB; bloat is a
     regression.
5. With `--export <dir>`, copy the verified wheel out of the
   container into `<dir>` (typically `~/.paige/wheels/`).

Exit code is non-zero on any failure. **Nothing exposes a broken
wheel to the host.**

## Phase B — install + run (host)

```bash
./scripts/prod.sh <command>
```

**The only script allowed to touch `~/.paige/venv` and the running
bot.**

| Command | Effect |
|---|---|
| `status` | Wheel version, PID, uptime, log path |
| `start` | `nohup paige > ~/.paige/paige.log` & write PID |
| `stop` | SIGTERM + 15 s grace → SIGKILL escalation |
| `restart` | stop → start |
| `logs [-f]` | tail `~/.paige/paige.log` |
| `upgrade <wheel>` | snapshot → graceful stop → install → start → health check → auto-rollback on failure |

### Lifecycle details

- **PID file** — `~/.paige/paige.pid`.
- **Log file** — `~/.paige/paige.log` (persistent — survives reboots).
- **Venv** — `~/.paige/venv`.
- **Wheels store** — `~/.paige/wheels/`. Last 3 wheels kept; older
  pruned on each upgrade.
- **Rollback snapshot** — `~/.paige/wheels/.previous/` holds the
  prior wheel for one-step rollback.
- **Stop signal policy** — always SIGTERM first. paige's signal
  handler in `entrypoint/main.py` flips a stop_event;
  `App.stop()` drains the Outbox, then shuts adapters down. SIGKILL
  is only used after a 15 s timeout — and it's a sign of trouble
  worth investigating, not papering over.
- **Health check on upgrade** — wait up to 30 s for the log to
  show `App started`. If it doesn't appear, the upgrade is treated
  as failed and rolled back to the previous wheel.

### Rollback

`upgrade` keeps the prior wheel under `~/.paige/wheels/.previous/`.
On health-check failure `prod.sh` automatically:
1. Stops the new (failing) bot.
2. `pip install --force-reinstall --no-deps <previous wheel>` and
   then re-installs deps.
3. Starts the old version.
4. Exits non-zero so you know something went wrong.

Manual rollback at any later time:
```bash
ls ~/.paige/wheels/                            # what's available
./scripts/prod.sh upgrade ~/.paige/wheels/paige-0.1.0-old.whl
```

## End-to-end recipe

```bash
# 1. Build + verify in dev container; export to host wheel store.
./do.sh artifact --export ~/.paige/wheels/

# 2. Inspect the artifact.
ls -la ~/.paige/wheels/

# 3. Upgrade live bot (graceful stop → install → start → health check).
./scripts/prod.sh upgrade ~/.paige/wheels/paige-0.1.0-py3-none-any.whl
```

## Configuration

```bash
export PAIGE_FEISHU_APP_ID=cli_xxx
export PAIGE_FEISHU_APP_SECRET=xxx
# Optional: Lark International domain override
# export PAIGE_FEISHU_DOMAIN=https://open.larksuite.com
```

The rest of the env (PAIGE_DIR, PAIGE_ALLOWED_USERS, etc.) is shared.
See `src/paige/entrypoint/config.py` for the full list.

## Migration from a manually-launched paige

If paige is already running because you started it with `nohup` /
`uv run paige` / a tmux pane (anything that doesn't write
`~/.paige/paige.pid`), `prod.sh status` will adopt the running
process via `pgrep -f "$VENV/bin/paige"` and write the PID file. No
restart needed.

Caveats:
- The log path only switches to `~/.paige/paige.log` on the next
  `prod.sh start` / `restart` / `upgrade`. Until then the live
  bot's stdout is wherever the manual launch pointed it.
- If you have multiple paige binaries on PATH (unusual), the
  adoption path is anchored on `$VENV/bin/paige` to avoid picking
  up the wrong one.

## Why this shape

- **Phase A validates the artifact in isolation.** A broken wheel
  can't reach the production venv.
- **Phase B is a single auditable script.** No ad-hoc `kill -9` /
  `pip install --force` chains in shell history.
- **Auto-rollback** means a botched deploy self-heals before the
  next message arrives.
- **The graceful-stop path exercises the queue drain** so messages
  in flight aren't lost on every restart (the Outbox.stop test
  pins this).

## What this is NOT

- **Not** a CI/CD pipeline. No remote registry, no GitHub Actions,
  no cron. It's a two-step manual deploy by intent — the host has
  one operator and one bot.
- **Not** a way to run multiple paige versions side-by-side. The
  host venv is single-tenant.
- **Not** docker-based for the running bot. paige runs natively on
  the host; the container is for build-and-test only.
