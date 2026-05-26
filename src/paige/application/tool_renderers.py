"""Tool-use → card body renderers (per-tool, with a JSON-pretty fallback).

The card header already carries `🔧 {tool_name}`, so each renderer
focuses the *body* on the interesting argument(s) for that tool —
the command for Bash, the path for Read, a diff for Edit, etc. —
rather than dumping the entire JSON input as one line.

A renderer takes the parsed input dict and returns a markdown body
string. The dispatcher wraps the result in a `Card` whose header
labels the tool; consumers can also use `render_tool_use` directly
with the raw JSON input text.

Unknown tools fall through to `_render_generic`, which pretty-prints
the input fields as `**key**: <value>` lines. That's strictly an
improvement over today's `🔧 *Name*({"k": "v", ...})` single-line
JSON dump on every tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlparse

from ..infrastructure.markdown_safe import fenced

# A renderer takes the parsed input dict and returns a body string.
# Empty body is allowed — callers (Dispatcher) decide whether to send.
ToolRenderer = Callable[[dict[str, Any]], str]


_VALUE_CLIP_CHARS = 400


def render_tool_use(tool_name: str, input_text: str) -> str:
    """Parse `input_text` as JSON, dispatch to the per-tool renderer
    (or the generic fallback), and return a markdown body string.

    `input_text` is the JSON-string form produced by `jsonl_parser`
    (`json.dumps(block["input"])`). Non-JSON input falls through to
    an empty-dict path; renderers degrade gracefully when their
    expected fields are missing."""
    input_dict = _safe_parse(input_text)
    renderer = _RENDERERS.get(tool_name, _render_generic)
    return renderer(input_dict)


def _safe_parse(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        parsed: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return cast("dict[str, Any]", parsed)


# ── per-tool renderers ──────────────────────────────────────────


def _render_bash(d: dict[str, Any]) -> str:
    command = str(d.get("command", "")).strip()
    description = str(d.get("description", "")).strip()
    if not command:
        return _render_generic(d)
    body = fenced(command, "bash")
    if description:
        # Blank line so the fence stays its own paragraph — `_split_paragraphs`
        # then keeps it a clean standalone code block (which Lark needs to
        # render as code rather than raw text with leaked `#` headings).
        body += f"\n\n_{description}_"
    return body


def _render_read(d: dict[str, Any]) -> str:
    path = str(d.get("file_path", "")).strip()
    if not path:
        return _render_generic(d)
    offset = d.get("offset")
    limit = d.get("limit")
    suffix = ""
    if isinstance(offset, int) and isinstance(limit, int):
        suffix = f" (lines {offset}–{offset + limit - 1})"
    elif isinstance(limit, int):
        suffix = f" (first {limit} lines)"
    elif isinstance(offset, int):
        suffix = f" (from line {offset})"
    return f"`{path}`{suffix}"


_WRITE_PREVIEW_LINES = 50


def _render_write(d: dict[str, Any]) -> str:
    path = str(d.get("file_path", "")).strip()
    if not path:
        return _render_generic(d)
    content = str(d.get("content", ""))
    if not content:
        return f"`{path}`"
    lines = content.split("\n")
    line_count = len(lines)
    lang = _lang_from_path(path)
    if line_count <= _WRITE_PREVIEW_LINES:
        return f"`{path}` _({line_count} lines)_\n\n{fenced(content, lang)}"
    preview = "\n".join(lines[:_WRITE_PREVIEW_LINES]) + "\n…"
    return (
        f"`{path}` _({line_count} lines · first {_WRITE_PREVIEW_LINES} shown)_\n\n"
        f"{fenced(preview, lang)}"
    )


_EXT_TO_LANG: dict[str, str] = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "jsx": "javascript",
    "md": "markdown",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "sh": "bash",
    "bash": "bash",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "sql": "sql",
    "rs": "rust",
    "go": "go",
    "java": "java",
    "kt": "kotlin",
    "rb": "ruby",
    "php": "php",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "hpp": "cpp",
    "cs": "csharp",
    "swift": "swift",
    "dockerfile": "dockerfile",
}


def _lang_from_path(path: str) -> str:
    """Lark/markdown fence info string for `path`'s extension.
    Unknown extensions fall back to the bare extension (Lark may still
    recognize many of them); no extension → empty string (no
    highlighting). `Dockerfile` (no extension, well-known name)
    handled as a special case."""
    name = path.rsplit("/", 1)[-1]
    if name.lower() == "dockerfile" or name.lower().startswith("dockerfile."):
        return "dockerfile"
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[1].lower()
    return _EXT_TO_LANG.get(ext, ext)


def _render_edit(d: dict[str, Any]) -> str:
    path = str(d.get("file_path", "")).strip()
    if not path:
        return _render_generic(d)
    old = str(d.get("old_string", ""))
    new = str(d.get("new_string", ""))
    replace_all = bool(d.get("replace_all", False))
    head = f"`{path}`" + (" _(replace all)_" if replace_all else "")
    diff = _format_diff(old, new)
    if not diff:
        return head
    return f"{head}\n\n{fenced(diff, 'diff')}"


def _format_diff(old: str, new: str) -> str:
    """Lines from `old` prefixed `-`, lines from `new` prefixed `+`.
    Empty inputs collapse to empty output (caller drops the diff
    block). No context-line interleaving — Edit replaces an exact
    block, so the visual `- … / + …` two-block shape matches the
    user's mental model."""
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []
    if not old_lines and not new_lines:
        return ""
    parts = [f"- {line}" for line in old_lines] + [f"+ {line}" for line in new_lines]
    return "\n".join(parts)


def _render_grep(d: dict[str, Any]) -> str:
    pattern = str(d.get("pattern", "")).strip()
    path = str(d.get("path", "")).strip()
    if not pattern:
        return _render_generic(d)
    return f"`{pattern}` in `{path}`" if path else f"`{pattern}`"


def _render_glob(d: dict[str, Any]) -> str:
    pattern = str(d.get("pattern", "")).strip()
    path = str(d.get("path", "")).strip()
    if not pattern:
        return _render_generic(d)
    return f"`{pattern}` in `{path}`" if path else f"`{pattern}`"


def _render_web_fetch(d: dict[str, Any]) -> str:
    url = str(d.get("url", "")).strip()
    prompt = str(d.get("prompt", "")).strip()
    if not url:
        return _render_generic(d)
    host = _hostname(url) or url
    body = f"[{host}]({url})"
    if prompt:
        body += f"\n_{prompt}_"
    return body


def _render_web_search(d: dict[str, Any]) -> str:
    query = str(d.get("query", "")).strip()
    if not query:
        return _render_generic(d)
    return f"_{query}_"


def _render_todo_write(d: dict[str, Any]) -> str:
    raw_todos = d.get("todos", [])
    if not isinstance(raw_todos, list) or not raw_todos:
        return "_(no todos)_"
    todos = cast("list[Any]", raw_todos)
    glyphs = {"completed": "✅", "in_progress": "🔄", "pending": "◯"}
    lines = [f"_({len(todos)} todos)_"]
    for t in todos:
        if not isinstance(t, dict):
            continue
        td = cast("dict[str, Any]", t)
        status = str(td.get("status", "pending"))
        content = str(td.get("content", "")).strip() or "_(no content)_"
        lines.append(f"{glyphs.get(status, '◯')} {content}")
    return "\n".join(lines)


def _render_task(d: dict[str, Any]) -> str:
    description = str(d.get("description", "")).strip()
    subagent = str(d.get("subagent_type", "general-purpose")).strip()
    prompt = str(d.get("prompt", "")).strip()
    head = f"**{subagent}**" + (f" — {description}" if description else "")
    if not prompt:
        return head
    first_line = prompt.split("\n", 1)[0].strip()
    return f"{head}\n\n_{_clip(first_line, _VALUE_CLIP_CHARS)}_"


def _render_skill(d: dict[str, Any]) -> str:
    skill = str(d.get("skill", "")).strip()
    args = str(d.get("args", "")).strip()
    if not skill:
        return _render_generic(d)
    return f"`{skill}` — {args}" if args else f"`{skill}`"


def _render_exit_plan_mode(d: dict[str, Any]) -> str:
    """`ExitPlanMode` carries the model's plan in `plan`. Render it
    verbatim — clipping the plan to 400 chars (the generic
    fallback) hides the most important content from the user, and
    the plan is usually well under the card body limit."""
    plan = d.get("plan", "")
    if not isinstance(plan, str) or not plan.strip():
        return "_(no plan)_"
    return plan


def _render_generic(d: dict[str, Any]) -> str:
    """Fallback for unknown tools: render each input field as
    `**key**: value`. Long values are clipped (Lark renders huge
    bodies poorly anyway, and we'd rather show a clean preview than
    an unbroken JSON wall)."""
    if not d:
        return "_(no input)_"
    parts: list[str] = []
    for key, value in d.items():
        parts.append(f"**{key}**: {_format_value(value)}")
    return "\n".join(parts)


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        clipped = _clip(value, _VALUE_CLIP_CHARS)
        return f"`{clipped}`" if "\n" not in clipped else f"\n{fenced(clipped)}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return f"_(list of {len(cast('list[Any]', value))})_"
    if isinstance(value, dict):
        return f"_(dict with {len(cast('dict[str, Any]', value))} keys)_"
    if value is None:
        return "_(null)_"
    return _clip(str(value), _VALUE_CLIP_CHARS)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hostname(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None
    return parsed.netloc or None


_RENDERERS: dict[str, ToolRenderer] = {
    "Bash": _render_bash,
    "Read": _render_read,
    "Write": _render_write,
    "Edit": _render_edit,
    "Grep": _render_grep,
    "Glob": _render_glob,
    "WebFetch": _render_web_fetch,
    "WebSearch": _render_web_search,
    "TodoWrite": _render_todo_write,
    "Task": _render_task,
    "Agent": _render_task,
    "Skill": _render_skill,
    "ExitPlanMode": _render_exit_plan_mode,
}


__all__ = ["ToolRenderer", "render_tool_use"]
