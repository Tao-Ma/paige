# Changelog

All notable changes land here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project follows [SemVer](https://semver.org/spec/v2.0.0.html),
with **0.x meaning the public surface is not yet stable**.

## [0.1.0] — 2026-05-25

Initial public release.

### IM bridge over Feishu / Lark
- **`Channel` port** — backend-agnostic IM interface. Feishu is the
  shipped adapter; future backends slot in without touching the
  rest of the codebase.
- **Streams the agent's transcript into a thread**; replies type
  back as keystrokes.
- **Topic-mode group routing** (Lark "话题模式群"). Set
  `PAIGE_FEISHU_GROUP_ID=oc_…` to opt in; bindings inside the group
  key on Lark's topic id (`omt_…`) so each topic gets its own pane.
  DM bindings keep working unchanged. See
  [`doc/topic-mode.md`](doc/topic-mode.md). Bootstrap helpers:
  `scripts/create_topic_group.py`, `scripts/seed_general_topic.py`.
- **Group `@bot` mention filter bypassed** inside the configured
  `PAIGE_FEISHU_GROUP_ID` — every message in the operator's paige
  group routes through.

### Interactive UI
- **Pane-scrape interactive UI** — Bash-permission prompts,
  edit / file permissions, `ExitPlanMode`, restore-checkpoint, and
  Settings overlays render as tappable cards even though Claude
  Code never emits them in JSONL.
- **`AskUserQuestion` cards** — Claude's option list renders as
  buttoned rows; tap to answer.
- **`/sessions` chooser** with Active / Resume / Archive / New
  sub-panes. Resume / Archive sub-panes use a `column_set` faux-table
  picker (basename + path / date · time · N msg / button per row) for
  tabular layout on a backend whose `table` element is display-only.
  Dormant sessions can be **archived** (moved to `~/.claude/archive/`)
  and **restored**; archived transcripts are viewable in place via the
  paginated history card.
- **Coalesced fan-out cards** — a parallel `Agent` / `Task` launch
  renders as one `🤖 Agents` card that ticks each subagent off as it
  finishes; a burst of `TaskCreate` / `TaskUpdate` calls collapses
  into one live `📋 Tasks` checklist (new card per task group). Both
  replace what was a card-per-tool-call flood.
- **Live status badge** that migrates to the most recent agent
  card so the spinner / elapsed-time pill is always visible at the
  bottom of the chat surface.
- **Markdown-safe card rendering** — truncation never severs a code
  fence (it's balanced before sending), tool arguments / output
  render verbatim, ATX headings in transcript prose are demoted so a
  skill body's `# Heading` doesn't blow up a card, and fenced blocks
  carry the newline margin Lark needs so a `#` comment inside `bash`
  stays code. `/history` paginates by length and splits an oversized
  message across pages, fence-aware.

### Slash commands
`/start`, `/sessions` (active / resume / archive / new), `/session`,
`/history`, `/screenshot`, `/usage`, `/server`, `/esc`, `/unbind`,
`/help`, `/livepane`. (Verbosity / collapse / seq prefs live on the
`/session` Manage card's ⚙ Prefs sub-pane, not as a command.)

### Optional features
- **Voice transcription** via OpenAI (only needed for IM backends
  that don't pre-transcribe — Feishu pre-transcribes client-side
  and skips the path).
- **`/screenshot`** — render the current tmux pane as an image.

### Tooling
- **`prod.sh`** — host-side lifecycle (status / start / stop /
  restart / upgrade) with auto-rollback on health-check failure.
- **`do.sh`** — dev-container build / test / lint / type / layer
  checks. No host install, no live IM.
- **`paige.testing` package** — port-fake test scaffolding for
  downstream consumers. Sealed off from production by `import-linter`.

### Architecture
- Layered package (`domain` / `ports` / `adapters` / `application` /
  `infrastructure` / `entrypoint`) with the layer graph enforced by
  `import-linter`.
- 960+ unit tests, plus integration and end-to-end suites.
