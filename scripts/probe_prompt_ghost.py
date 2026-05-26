"""Research probe: dump a tmux pane's *styled* capture so we can read
the SGR codes Claude Code uses for the grey "ghost" prompt suggestion.

Background — paige's ready card wants to surface Claude Code's grey
auto-suggested prompt (the one optimized for "just hit Enter") as a
one-tap Accept button. Detecting it means scraping the pane *with*
escape sequences and matching the exact dim/grey styling — which is
version-specific and can't be guessed. This script captures ground
truth so the real extractor (`terminal_parser.extract_prompt_suggestion`)
can be written + fixture-tested against it.

This reproduces exactly what `TmuxMultiplexer.capture_with_ansi` sees:
`tmux capture-pane -p -e` over the visible pane (no scrollback).

NOT paige code — a throwaway under scripts/ per the research-isolation
rule. It talks to tmux directly and imports nothing from paige.

Usage (run anywhere; needs only `tmux` on PATH):

    # 1. Find your claude pane:
    python scripts/probe_prompt_ghost.py --list

    # 2. At an end_turn where the grey ghost suggestion is showing,
    #    dump the prompt region with escape codes made visible:
    python scripts/probe_prompt_ghost.py %3
    python scripts/probe_prompt_ghost.py mysession:1.0

    # Variants:
    python scripts/probe_prompt_ghost.py %3 --full   # whole visible pane
    python scripts/probe_prompt_ghost.py %3 --repr   # python repr (every byte)

Paste the output back. We're looking for the SGR run wrapping the
ghost text — typically one of:
    \\e[2m         faint / dim
    \\e[90m        bright-black (grey) foreground
    \\e[38;5;Nm    256-color grey (N often 8 / 240-245)
    \\e[38;2;r;g;bm truecolor grey
…and crucially, how it differs from (a) the generic placeholder hint
and (b) real typed text on the prompt line.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def _run(args: list[str]) -> str:
    """Run a tmux command, returning stdout. Exits with the tmux
    error text on failure (e.g. "can't find pane")."""
    proc = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or f"tmux {' '.join(args)} failed\n")
        sys.exit(proc.returncode or 1)
    return proc.stdout


def list_panes() -> None:
    fmt = (
        "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"
        "\tcmd=#{pane_current_command}\ttitle=#{pane_title}"
    )
    out = _run(["list-panes", "-a", "-F", fmt])
    if not out.strip():
        print("(no tmux panes — is anything running under tmux?)")
        return
    print("PANE_ID\tLOCATION\tCOMMAND\tTITLE")
    print(out.rstrip("\n"))
    print("\nPick the pane running `claude` and re-run with its PANE_ID.")


def _visible_escapes(text: str) -> str:
    r"""Render ESC (0x1b) as a visible `\e` so SGR sequences are
    readable inline (e.g. `\e[2m`). Leaves everything else intact."""
    return text.replace("\x1b", "\\e")


def dump(target: str, *, full: bool, as_repr: bool, tail: int) -> None:
    # -p print to stdout, -e keep escape sequences. Default capture is
    # the visible pane only — same as capture_with_ansi.
    raw = _run(["capture-pane", "-p", "-e", "-t", target])
    lines = raw.split("\n")
    # Trim trailing all-blank lines tmux pads to pane height.
    while lines and lines[-1].strip() == "":
        lines.pop()

    if not full:
        # The ghost lives on the `>` prompt line near the bottom; show
        # the tail so the dump stays focused and paste-friendly.
        lines = lines[-tail:]

    print(f"# pane={target}  lines={len(lines)}  "
          f"({'full visible pane' if full else f'last {tail}'})")
    print("# ESC shown as \\e ; each SGR run wraps styled text\n")
    for i, line in enumerate(lines):
        if as_repr:
            print(f"{i:>3} | {line!r}")
        else:
            print(f"{i:>3} | {_visible_escapes(line)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="tmux pane target (e.g. %%3 or session:win.pane). "
        "Omit with --list to enumerate panes.",
    )
    parser.add_argument("--list", action="store_true", help="List all tmux panes and exit.")
    parser.add_argument("--full", action="store_true", help="Dump the whole visible pane.")
    parser.add_argument(
        "--repr",
        dest="as_repr",
        action="store_true",
        help="Print python repr of each line (shows every byte, no \\e folding).",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=20,
        help="Lines from the bottom to show when not --full (default: 20).",
    )
    args = parser.parse_args()

    if args.list or not args.target:
        list_panes()
        return 0

    dump(args.target, full=args.full, as_repr=args.as_repr, tail=args.tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
