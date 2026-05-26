"""transcript_path — encoding + composition."""

from __future__ import annotations

from pathlib import Path

from paige.infrastructure.transcript_path import encode_cwd, transcript_path


def test_encode_simple_path() -> None:
    assert encode_cwd(Path("/home/u/proj")) == "-home-u-proj"


def test_encode_replaces_underscore_and_dot() -> None:
    assert encode_cwd(Path("/home/user_name/foo.bar")) == "-home-user-name-foo-bar"


def test_encode_keeps_dash_and_alnum() -> None:
    assert encode_cwd(Path("/Code/my-project-42")) == "-Code-my-project-42"


def test_encode_strips_trailing_slash_via_path_str() -> None:
    # Path collapses trailing slashes; resulting str has no trailing dash.
    assert encode_cwd(Path("/p/")) == "-p"


def test_transcript_path_composes_correctly(tmp_path: Path) -> None:
    p = transcript_path("abc-123", Path("/home/u/proj"), projects_root=tmp_path)
    assert p == tmp_path / "-home-u-proj" / "abc-123.jsonl"


def test_transcript_path_default_root_uses_home_claude_projects() -> None:
    p = transcript_path("rid", Path("/x"))
    parts = p.parts
    # Expect the path to land under ~/.claude/projects/.
    assert ".claude" in parts
    assert "projects" in parts
    assert p.name == "rid.jsonl"
