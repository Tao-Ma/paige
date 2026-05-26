"""terminal_parser — pure pane-text → PaneStatus + interactive UI detection.

Stateless. Takes a tmux pane snapshot (the bytes from
`Multiplexer.capture`) and detects:
  - whether Claude Code's TUI is showing its `Thinking…` spinner
    (`parse_status`); and
  - whether the TUI is showing an interactive UI overlay
    (`extract_interactive_content` / `is_interactive_ui`).

Layout we look for (Claude Code 2.x):

    ⎿ tool progress hint (optional)
    ⎿ tool progress hint (optional)
    ✻ Thinking… (45s · 2.1k tokens)
    ─────────────────────────────────  ← chrome separator
    > prompt prompt prompt
    ─────────────────────────────────  ← chrome separator (sometimes)

The chrome line is what anchors detection — find it from the bottom,
then scan up for a spinner glyph. v1's hard-won detail: if the
spinner falls 4+ non-empty non-`⎿` lines above the chrome, it's not
a status line; it's prose that contains a spinner-shaped char.

Spinner glyphs: { · ✻ ✽ ✶ ✳ ✢ } — Claude Code rotates through these
to animate.

Interactive UIs (PermissionPrompt, BashApproval, ExitPlanMode,
RestoreCheckpoint, Settings, AskUserQuestion fallback) are detected
by regex pattern pairs (top-marker + bottom-marker). Order in
`UI_PATTERNS` is significant — the more specific patterns
(BashApproval, version-specific prompts) come before the generic
fallbacks. The richer JSONL-based AskUserQuestion path in
`paige.application.ask_user` takes precedence over the AskUserQuestion
fallback patterns here when both fire — the JSONL data has the real
option labels and option descriptions, where the pane scrape only
sees the truncated checkbox glyphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SPINNER_CHARS = frozenset({"·", "✻", "✽", "✶", "✳", "✢"})
_CHROME_LOOKBACK = 15
_SPINNER_SEARCH_DEPTH = 4


@dataclass(frozen=True)
class PaneStatus:
    """Outcome of a pane scrape.

    `spinner` is True when Claude is actively working. `text` is the
    body after the spinner glyph (e.g. "Thinking… (12s · 4k tokens)"),
    or None if no readable text remains.
    """

    spinner: bool
    text: str | None = None


def parse_status(pane_text: str) -> PaneStatus:
    """Parse the pane snapshot. Returns the spinner state.

    Returns `PaneStatus(spinner=False, text=None)` for empty input,
    panes without chrome separators, or panes whose chrome region
    has no spinner glyph above it.
    """
    if not pane_text:
        return PaneStatus(spinner=False)

    lines = pane_text.rstrip("\n").split("\n")
    if not lines:
        return PaneStatus(spinner=False)

    chrome_idx = _find_chrome(lines)
    if chrome_idx < 0:
        return PaneStatus(spinner=False)

    return _scan_for_spinner(lines, chrome_idx)


def _find_chrome(lines: list[str]) -> int:
    """Index of the topmost chrome line in the last `_CHROME_LOOKBACK`
    lines. Searches bottom-up; returns the FIRST chrome we see (which
    is the bottom one). Returns -1 if none found.

    Why bottom-up: Claude's TUI may render two chrome separators
    sandwiching the prompt area. We want the one closest to the
    bottom, then scan upward from it for the spinner above the
    higher chrome (or above this one if there's only one).
    """
    start = len(lines) - 1
    end = max(-1, start - _CHROME_LOOKBACK)
    for i in range(start, end, -1):
        if _is_chrome(lines[i]):
            return i
    return -1


def _scan_for_spinner(lines: list[str], chrome_idx: int) -> PaneStatus:
    """Scan up to `_SPINNER_SEARCH_DEPTH` non-empty, non-`⎿` lines
    above the chrome looking for a leading spinner glyph.

    Skipped:
    - blank lines
    - tool-progress hints starting with `⎿`
    - the second chrome separator (when two are present)
    """
    seen_useful = 0
    for i in range(chrome_idx - 1, -1, -1):
        line = lines[i].lstrip()
        if not line:
            continue
        if line.startswith("⎿"):
            continue
        if _is_chrome(lines[i]):
            continue
        # First "useful" line. Spinner must be on this line OR within
        # the next few useful lines.
        first = line[0]
        if first in _SPINNER_CHARS:
            text = line[1:].strip()
            return PaneStatus(spinner=True, text=text or None)
        seen_useful += 1
        if seen_useful >= _SPINNER_SEARCH_DEPTH:
            break
    return PaneStatus(spinner=False)


def _is_chrome(line: str) -> bool:
    """A chrome separator: a non-empty stripped line of only `─`."""
    s = line.strip()
    return len(s) >= 3 and all(c == "─" for c in s)


# ── Interactive UI detection ────────────────────────────────────────


@dataclass(frozen=True)
class InteractiveUIContent:
    """Content extracted from an interactive UI overlay.

    `name` is the matching pattern's label (PermissionPrompt,
    BashApproval, ExitPlanMode, AskUserQuestion, RestoreCheckpoint,
    Settings) — callers use it to pick a render style.
    """

    content: str
    name: str


@dataclass(frozen=True)
class _UIPattern:
    """A top-marker + bottom-marker pair delimiting a TUI overlay.

    Extraction scans top-down: the first line matching any `top`
    pattern marks the start, the first subsequent line matching any
    `bottom` pattern marks the end. Both boundary lines are included
    in the extracted content. Empty `bottom` means "extend to last
    non-empty line".
    """

    name: str
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2


# Order matters — more specific patterns first.
UI_PATTERNS: tuple[_UIPattern, ...] = (
    _UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    _UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),
        bottom=(),
        min_gap=1,
    ),
    _UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    _UIPattern(
        # Bash approval — capture the full prompt (command + question
        # + numbered choices). Listed before the generic permission
        # patterns so the bash-specific context isn't cut off.
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(
            re.compile(r"^\s*Esc to cancel"),
            # Claude Code 2.x+ uses a numbered menu and may drop the
            # Esc hint — match the last "N. No" line instead.
            re.compile(r"^\s*3\.\s*No\b"),
        ),
    ),
    _UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(
            re.compile(r"^\s*Esc to cancel"),
            re.compile(r"^\s*3\.\s*No\b"),
        ),
    ),
    _UIPattern(
        # Numbered-choice fallback when neither BashApproval nor a
        # specific PermissionPrompt top matched (the `❯ 1. Yes` line
        # alone is enough to signal a permission menu).
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    _UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    _UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
)


_LONG_DASH_RE = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace runs of 5+ ─ chars with exactly ───── — a cosmetic
    pass so the captured TUI text fits the IM card without horizontal
    scrolling on mobile."""
    return "\n".join("─────" if _LONG_DASH_RE.match(line) else line for line in text.split("\n"))


def _try_extract(lines: list[str], pattern: _UIPattern) -> InteractiveUIContent | None:
    top_idx: int | None = None
    bottom_idx: int | None = None
    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break
    if top_idx is None:
        return None
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break
    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None
    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Detect any interactive TUI overlay in `pane_text`.

    Tries each `UI_PATTERNS` entry in declaration order (specific
    before fallback); first match wins. Returns None when no
    recognizable UI is present.
    """
    if not pane_text:
        return None
    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result is not None:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """True iff the pane is currently showing one of the recognized
    interactive UI overlays."""
    return extract_interactive_content(pane_text) is not None


# ── Prompt-ghost (autosuggest) extraction ──────────────────────────
#
# Claude Code renders a grey "ghost" suggestion on the prompt line —
# a pre-filled next prompt that submits if you just hit Enter. We
# scrape it (from an ANSI capture, `capture_with_ansi`) to offer a
# one-tap Accept on the end-turn panel.
#
# Ground truth, from a live pane (ESC shown as \e), three states of
# the `❯ ` prompt line between the chrome separators:
#
#   empty   \e[39m❯ \e[7m \e[0m
#   typed   \e[39m❯ update lazytui\e[7m \e[0m
#   ghost   \e[39m❯ \e[7ml\e[0;2meave it, commit & push as one commit\e[0m
#
# The ghost is the only state carrying SGR attribute 2 (faint) after
# the marker: the reverse-video cursor (`\e[7m`) sits on the FIRST
# ghost char, then `\e[2m` faints the remainder. Typed text is
# default-fg (no faint); empty is just the cursor on a space. So the
# rule is: after the marker, require a faint run and reject any
# non-whitespace plain (non-faint, non-reverse) char — that last
# guard skips the partial-typed-then-completed case conservatively.
#
# Note the faint attribute (`2`) is distinct from the 256-color greys
# (`38;5;24x`) this build uses for the spinner/chrome — we parse SGR
# properly so an extended-color index that happens to be `2` is not
# mistaken for the faint attribute. If a future Claude switches the
# ghost to a 256-color grey this returns None (no suggestion) rather
# than misfiring — the caller degrades to an empty slot.

_CSI_RE = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")
_PROMPT_LOOKBACK = 15


def _strip_ansi(text: str) -> str:
    return _CSI_RE.sub("", text)


def _apply_sgr(params: str, faint: bool, reverse: bool) -> tuple[bool, bool]:
    """Fold one SGR parameter list into the (faint, reverse) state.

    Consumes the sub-parameters of extended-color codes (`38;5;N`,
    `38;2;R;G;B`, and the `48;…` background forms) so a colour index
    of `2` is never read as the faint attribute.
    """
    codes = [int(p) if p else 0 for p in params.split(";")] if params else [0]
    idx = 0
    while idx < len(codes):
        code = codes[idx]
        if code == 0:
            faint = reverse = False
        elif code == 2:
            faint = True
        elif code == 22:
            faint = False
        elif code == 7:
            reverse = True
        elif code == 27:
            reverse = False
        elif code in (38, 48) and idx + 1 < len(codes):
            mode = codes[idx + 1]
            idx += 2 if mode == 5 else 4 if mode == 2 else 0
        idx += 1
    return faint, reverse


def _styled_chars(raw: str) -> list[tuple[str, bool, bool]]:
    """Walk an ANSI line into `(char, faint, reverse)` triples,
    tracking SGR state. Non-SGR escape sequences are consumed without
    effect; lone ESC bytes are dropped."""
    out: list[tuple[str, bool, bool]] = []
    faint = reverse = False
    i, n = 0, len(raw)
    while i < n:
        if raw[i] == "\x1b":
            m = _CSI_RE.match(raw, i)
            if m:
                if m.group(2) == "m":
                    faint, reverse = _apply_sgr(m.group(1), faint, reverse)
                i = m.end()
                continue
            i += 1  # lone / unknown ESC
            continue
        out.append((raw[i], faint, reverse))
        i += 1
    return out


def extract_prompt_suggestion(ansi_text: str) -> str | None:
    """Return Claude Code's grey ghost prompt suggestion, or None.

    Takes an ANSI pane capture (`Multiplexer.capture_with_ansi`).
    Returns None when there's no ghost — empty prompt, real typed
    text, no recognizable prompt line, or styling this doesn't match
    (callers treat None as "no suggestion" and degrade gracefully).
    """
    if not ansi_text:
        return None
    lines = ansi_text.split("\n")
    start = len(lines) - 1
    end = max(-1, start - _PROMPT_LOOKBACK)
    prompt_raw: str | None = None
    for i in range(start, end, -1):
        stripped = _strip_ansi(lines[i]).lstrip()
        if stripped.startswith("❯") or stripped.startswith(">"):
            prompt_raw = lines[i]
            break
    if prompt_raw is None:
        return None

    chars = _styled_chars(prompt_raw)
    marker = next((j for j, (c, _, _) in enumerate(chars) if c in ("❯", ">")), None)
    if marker is None:
        return None
    rest = chars[marker + 1 :]
    if rest and rest[0][0] == " ":  # the single space after the marker
        rest = rest[1:]
    while rest and rest[-1][0].isspace():  # trailing padding
        rest = rest[:-1]
    if not rest:
        return None
    if not any(faint for _, faint, _ in rest):
        return None  # empty prompt or typed text — no faint ghost run
    if any(not faint and not reverse and not c.isspace() for c, faint, reverse in rest):
        return None  # real typed text present — don't treat as a pure ghost
    text = "".join(c for c, _, _ in rest).strip()
    return text or None


_OPTION_LINE_RE = re.compile(r"^\s*[❯>]?\s*(\d+)\.\s+(.+?)\s*$")


def extract_options(content: str) -> list[tuple[int, str]]:
    """Parse numbered menu options out of an interactive UI content
    block.

    Matches lines like `❯ 1. Yes`, `  2. No`, `> 3. Cancel`. Returns
    `[(num, text), ...]` in source order. Empty list when the UI is
    text-input style (no numbered menu) or the pattern doesn't bite —
    the caller falls back to an arrow-nav keyboard.
    """
    out: list[tuple[int, str]] = []
    for line in content.split("\n"):
        m = _OPTION_LINE_RE.match(line)
        if m is None:
            continue
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        text = m.group(2).strip()
        if text:
            out.append((num, text))
    return out
