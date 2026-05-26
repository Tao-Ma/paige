"""Per-tool body renderers — verifies each known tool produces a
clean, header-less body. The Dispatcher tests cover wiring;
these tests cover *what* each tool's body looks like."""

from __future__ import annotations

import json

from paige.application.tool_renderers import render_tool_use


def _render(tool_name: str, input_dict: dict) -> str:
    return render_tool_use(tool_name, json.dumps(input_dict))


# ── Bash ─────────────────────────────────────────────────────────


def test_bash_fenced_command_with_italic_description() -> None:
    out = _render("Bash", {"command": "ls -la /tmp", "description": "List /tmp"})
    assert "```bash\nls -la /tmp\n```" in out
    assert "_List /tmp_" in out


def test_bash_command_without_description_renders_only_fenced_block() -> None:
    out = _render("Bash", {"command": "echo hi"})
    assert out == "```bash\necho hi\n```"


def test_bash_empty_command_falls_through_to_generic() -> None:
    """If `command` is missing, we can't claim to render a bash
    block; fall through to the generic key:value pretty-print so
    the user still sees what was sent."""
    out = _render("Bash", {"description": "broken call"})
    assert "**description**" in out
    assert "broken call" in out


# ── Read ─────────────────────────────────────────────────────────


def test_read_renders_path_only_by_default() -> None:
    assert _render("Read", {"file_path": "/etc/passwd"}) == "`/etc/passwd`"


def test_read_renders_offset_limit_as_line_range() -> None:
    out = _render("Read", {"file_path": "/x.py", "offset": 10, "limit": 5})
    assert out == "`/x.py` (lines 10–14)"


def test_read_renders_limit_only() -> None:
    out = _render("Read", {"file_path": "/x.py", "limit": 5})
    assert out == "`/x.py` (first 5 lines)"


# ── Write ────────────────────────────────────────────────────────


def test_write_short_file_includes_full_content_with_fenced_block() -> None:
    out = _render("Write", {"file_path": "/new.py", "content": "a\nb\nc"})
    # path + line count badge, then full content in a fenced python block.
    assert "`/new.py` _(3 lines)_" in out
    assert "```python\na\nb\nc\n```" in out


def test_write_picks_language_from_extension() -> None:
    out = _render("Write", {"file_path": "/x.md", "content": "# Heading\n\nbody"})
    assert "```markdown\n" in out
    out_ts = _render("Write", {"file_path": "/x.tsx", "content": "export const x = 1"})
    assert "```typescript\n" in out_ts
    out_toml = _render("Write", {"file_path": "pyproject.toml", "content": "[x]\nk=1"})
    assert "```toml\n" in out_toml


def test_write_unknown_extension_uses_extension_verbatim() -> None:
    out = _render("Write", {"file_path": "/x.weird", "content": "hi"})
    assert "```weird\n" in out


def test_write_no_extension_uses_empty_fence_info() -> None:
    out = _render("Write", {"file_path": "/Makefile", "content": "all:\n\techo hi"})
    assert "```\nall:\n\techo hi\n```" in out


def test_write_dockerfile_is_handled_specifically() -> None:
    out = _render("Write", {"file_path": "/Dockerfile", "content": "FROM alpine"})
    assert "```dockerfile\n" in out


def test_write_long_file_truncates_to_preview_with_marker() -> None:
    content = "\n".join(f"line {i}" for i in range(120))
    out = _render("Write", {"file_path": "/big.py", "content": content})
    assert "120 lines" in out
    assert "first 50 shown" in out
    # Preview ends with an ellipsis line so the user sees the truncation.
    assert "line 0" in out
    assert "line 49" in out
    assert "line 50" not in out
    assert "…" in out


def test_write_empty_content_renders_path_only() -> None:
    assert _render("Write", {"file_path": "/empty.txt", "content": ""}) == "`/empty.txt`"


# ── Edit ─────────────────────────────────────────────────────────


def test_edit_renders_path_and_diff_block() -> None:
    out = _render(
        "Edit",
        {"file_path": "/foo.py", "old_string": "foo()", "new_string": "bar()"},
    )
    assert out.startswith("`/foo.py`")
    assert "```diff\n- foo()\n+ bar()\n```" in out


def test_edit_replace_all_is_annotated() -> None:
    out = _render(
        "Edit",
        {"file_path": "/foo.py", "old_string": "x", "new_string": "y", "replace_all": True},
    )
    assert "_(replace all)_" in out


def test_edit_multiline_diff_lines_keep_prefix_per_line() -> None:
    out = _render(
        "Edit",
        {"file_path": "/m.py", "old_string": "a\nb", "new_string": "x\ny"},
    )
    assert "- a\n- b\n+ x\n+ y" in out


# ── Grep / Glob ──────────────────────────────────────────────────


def test_grep_renders_pattern_and_path() -> None:
    out = _render("Grep", {"pattern": "TODO", "path": "src/"})
    assert out == "`TODO` in `src/`"


def test_grep_renders_pattern_only_when_no_path() -> None:
    assert _render("Grep", {"pattern": "TODO"}) == "`TODO`"


def test_glob_uses_same_shape_as_grep() -> None:
    out = _render("Glob", {"pattern": "**/*.py", "path": "src/"})
    assert out == "`**/*.py` in `src/`"


# ── WebFetch / WebSearch ─────────────────────────────────────────


def test_web_fetch_renders_host_link_and_italic_prompt() -> None:
    out = _render(
        "WebFetch",
        {"url": "https://example.com/some/long/path", "prompt": "summarize"},
    )
    assert "[example.com](https://example.com/some/long/path)" in out
    assert "_summarize_" in out


def test_web_fetch_without_prompt_renders_only_link() -> None:
    out = _render("WebFetch", {"url": "https://example.com/x"})
    assert out == "[example.com](https://example.com/x)"


def test_web_search_renders_query_in_italics() -> None:
    assert _render("WebSearch", {"query": "claude 4.7"}) == "_claude 4.7_"


# ── TodoWrite ────────────────────────────────────────────────────


def test_todo_write_renders_count_and_each_todo_with_glyph() -> None:
    out = _render(
        "TodoWrite",
        {
            "todos": [
                {"content": "Investigate bug", "status": "in_progress"},
                {"content": "Write tests", "status": "pending"},
                {"content": "Ship", "status": "completed"},
            ]
        },
    )
    lines = out.split("\n")
    assert lines[0] == "_(3 todos)_"
    assert "🔄 Investigate bug" in lines
    assert "◯ Write tests" in lines
    assert "✅ Ship" in lines


def test_todo_write_empty_list_renders_placeholder() -> None:
    assert _render("TodoWrite", {"todos": []}) == "_(no todos)_"


# ── Task / Agent ─────────────────────────────────────────────────


def test_task_renders_subagent_and_description_and_first_line_of_prompt() -> None:
    out = _render(
        "Task",
        {
            "description": "Find the bug",
            "subagent_type": "Explore",
            "prompt": "Look for the bug in src/.\n\nDon't fix it yet.",
        },
    )
    assert out.startswith("**Explore** — Find the bug")
    assert "_Look for the bug in src/._" in out
    # Only first line of prompt — second paragraph stays out.
    assert "Don't fix" not in out


def test_agent_uses_same_renderer_as_task() -> None:
    out = _render("Agent", {"subagent_type": "Plan", "description": "Plan it"})
    assert out == "**Plan** — Plan it"


# ── Skill ────────────────────────────────────────────────────────


def test_skill_renders_name_and_args() -> None:
    assert _render("Skill", {"skill": "review", "args": "HEAD~3..HEAD"}) == (
        "`review` — HEAD~3..HEAD"
    )


def test_skill_without_args_renders_name_only() -> None:
    assert _render("Skill", {"skill": "init"}) == "`init`"


# ── Generic fallback ────────────────────────────────────────────


def test_unknown_tool_renders_as_key_value_lines() -> None:
    out = _render("MysteryTool", {"alpha": "first", "beta": 42, "gamma": True})
    lines = out.split("\n")
    assert "**alpha**: `first`" in lines
    assert "**beta**: 42" in lines
    assert "**gamma**: true" in lines


def test_unknown_tool_with_no_input_renders_placeholder() -> None:
    assert render_tool_use("MysteryTool", "") == "_(no input)_"


def test_unknown_tool_with_multiline_string_uses_fenced_block() -> None:
    out = _render("MysteryTool", {"body": "line1\nline2\nline3"})
    assert "**body**:" in out
    assert "```\nline1\nline2\nline3\n```" in out


def test_unknown_tool_with_list_and_dict_summarises_size() -> None:
    out = _render(
        "MysteryTool",
        {"items": [1, 2, 3, 4], "config": {"a": 1, "b": 2}},
    )
    assert "**items**: _(list of 4)_" in out
    assert "**config**: _(dict with 2 keys)_" in out


def test_unknown_tool_null_value_renders_placeholder() -> None:
    out = _render("MysteryTool", {"missing": None})
    assert "**missing**: _(null)_" in out


# ── Bad input handling ──────────────────────────────────────────


def test_non_json_input_text_renders_no_input() -> None:
    """Tool whose `text` is not valid JSON (shouldn't happen — the
    parser always emits json.dumps — but be defensive)."""
    assert render_tool_use("Bash", "not json {") == "_(no input)_"


def test_non_dict_json_input_falls_through_to_no_input() -> None:
    """A JSON list / string / number for `input` is parser-impossible
    today but the renderer survives it."""
    assert render_tool_use("Bash", '["arr"]') == "_(no input)_"
    assert render_tool_use("Bash", '"str"') == "_(no input)_"


# ── Long value clipping ─────────────────────────────────────────


def test_generic_clips_very_long_string_values() -> None:
    long_val = "x" * 1000
    out = _render("MysteryTool", {"big": long_val})
    assert "**big**:" in out
    # 400-char clip + ellipsis sentinel; the full string must not
    # appear unchanged.
    assert long_val not in out
    assert "…" in out
