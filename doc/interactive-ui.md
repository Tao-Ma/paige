# Interactive UI surfacing

Claude Code overlays a handful of full-pane prompts on its TUI that
never reach the JSONL transcript while they're active — Bash
permission prompts, `ExitPlanMode` confirmations,
`RestoreCheckpoint` pickers, the Settings palette, and
`AskUserQuestion` (the model's structured-choice tool). Paige
surfaces them in IM via two paths that share one detector but use
two renderers:

```
                    ┌─────────────────────────────┐
                    │  Pane-scrape (1 s ticks)    │
                    │  terminal_parser detects:   │
                    │  - BashApproval             │
                    │  - ExitPlanMode             │
                    │  - RestoreCheckpoint        │
                    │  - Settings                 │
                    │  - AskUserQuestion          │
                    └──────────┬──────────────────┘
                               │
                  ┌────────────┴────────────┐
                  │                         │
                  ▼                         ▼
        InteractiveUIService          LivePaneService
        (iui card)                    (/livepane card)
        for Bash / Plan /             for AskUserQuestion
        Restore / Settings            (single source of truth)
```

## What each renderer does

### `InteractiveUIService` (iui card)

Lives in `paige.application.interactive_ui`. For the four
"simple" overlays — Bash approval, ExitPlanMode confirmation,
RestoreCheckpoint picker, Settings palette — it:

- Captures the pane each tick via `Multiplexer.capture`.
- Calls `extract_interactive_content` to detect which overlay
  pattern matches and to slice the body to just the overlay (so
  the iui card doesn't carry the whole pane scrollback).
- Renders a card with the body as a markdown blockquote (one `>`
  per line — see the comment in `_render_pane_body` for why
  blockquote-over-code-block was the historical choice).
- Carries a 3×3 nav grid (Space / ↑ / Tab, ← / ↓ / →, Esc / 🔄 /
  Enter) plus a `🔄 Refresh` button.
- Deletes the card after `idle_debounce` ticks of "no overlay
  detected" (avoids flicker on transient pane redraws).

These overlays are short-lived yes/no decisions. The iui card is
deliberately simple.

### `LivePaneService` (`/livepane`)

Lives in `paige.application.live_pane`. Two entry points:

- **User-invoked**: `/livepane` command. Posts a card, spawns a
  poll loop that re-captures the pane every 1.5 s and PATCHes the
  card when the text changes.
- **Auto-spawned from iui**: when `InteractiveUIService` detects
  `AskUserQuestion`, it calls `LivePaneService.start_for_binding`
  instead of rendering its own iui card. The detector becomes a
  trigger; `LivePaneService` owns the rendering.

The /livepane card is richer than the iui card:

- **ANSI-preserved capture** via `Multiplexer.capture_with_ansi`.
  Background-color highlights are extracted (`extract_highlights`)
  and the active tab's `☐` glyph is rewritten to `☒` so the user
  can see which tab is current after ANSI is stripped.
- **Body trimmed to the overlay** via `extract_interactive_content`,
  with a `Planning: <path>` line preserved above when present
  (plan-mode anchor).
- **Code-block body** (monospace) — preserves TUI column
  alignment in Lark's narrow card width.
- **Mode-aware input slot**:
  - Selection mode, non-text option highlighted → input hidden
    (typed chars discarded by the picker).
  - Selection mode, "Type something"-style option highlighted →
    input shown with commit-first semantics (submit prepends an
    Enter so the option commits and the TUI transitions to
    text-input before the typed text lands).
  - Otherwise → input shown with plain `<text><Enter>` submit.
- **Stop** (freeze loop, keep card as scrollback) and **Dismiss**
  (freeze + delete) buttons in addition to the nav grid.
- **`force_no_collapse=True`** keeps the body expanded across
  PATCHes even if the user's per-topic collapse pref is set.

## Why the split

Three motivations:

1. **AskUserQuestion needs the rich UX.** Multi-tab forms,
   `Type something` follow-ups, plan-mode context, long
   selection-then-text flows — none of which suit the iui card's
   blockquote-and-nav-grid shape.
2. **The simple overlays don't need it.** Bash approval is yes/no
   in under five seconds. Spawning a 1.5 s-poll loop for that
   would burn rate budget for no benefit.
3. **One rendering implementation** for the rich case. Past
   attempts to grow the iui card into a livepane-equivalent
   (markdown tabs, input slots) introduced drift and
   subtle bugs. The handoff keeps the rich rendering in one
   place; the iui side is a thin detector for the simple cases.

## Auto-spawn lifecycle

When `InteractiveUIService.tick()` sees `AskUserQuestion`:

1. Calls `livepane.start_for_binding(person, conversation)`.
2. The call is idempotent — if a loop is already running for that
   binding (tracked by `LivePaneService._binding_anchors`), it
   returns immediately. Safe to call every tick.
3. `start_for_binding` resolves the bound pane via the registry,
   captures it, sends a card, and spawns a poll task.
4. Subsequent ticks while the overlay is still detected → no-op.
5. When the overlay clears (user picked / dismissed), iui's
   `_on_clear` calls `livepane.stop_for_binding`, which cancels
   the poll loop. The card stays in the chat as scrollback.

User-invoked `/livepane` cards are tracked separately by
`anchor.message_id` so `stop_for_binding` doesn't kill a card the
user explicitly asked for.

## What's NOT here

- The JSONL-based `AskUserQuestion` renderer in
  `paige.application.ask_user` is a separate path that fires once
  the `tool_use` line lands in JSONL — which is end-of-turn, often
  minutes after the prompt appeared in the TUI. The iui→livepane
  handoff is the primary surface; the JSONL renderer is the
  late-arriving safety net for deployments where `LivePaneService`
  isn't wired.
- Streaming-card root-id pinning (the `openclaw#28273` quirk where
  streaming chunks inside a topic-mode group can spawn unwanted
  sub-topics) hasn't been observed in practice with paige's
  current card payloads. Revisit if it surfaces.

## Files

- `application/interactive_ui.py` — detector + simple-overlay
  renderer.
- `application/live_pane.py` — `/livepane` command + auto-spawn
  API + rich renderer.
- `infrastructure/terminal_parser.py` — overlay-pattern matching,
  active-option extraction, selection-mode classifier.
- `infrastructure/ansi_markdown.py` — `strip_ansi`,
  `extract_highlights`.
