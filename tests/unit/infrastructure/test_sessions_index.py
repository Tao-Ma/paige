"""sessions_index — JSONL walk + dormant session enumeration."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from paige.infrastructure.sessions_index import (
    DormantSession,
    archive_dormant_session,
    archive_root_for,
    count_archived_sessions,
    delete_dormant_session,
    list_archived_sessions,
    list_dormant_sessions,
    restore_archived_session,
)


def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines))


def _user(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def test_missing_root_returns_empty(tmp_path: Path) -> None:
    assert list_dormant_sessions(tmp_path / "nope") == []


def test_empty_root_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "ignored").mkdir()
    assert list_dormant_sessions(tmp_path) == []


def test_finds_a_simple_session(tmp_path: Path) -> None:
    proj = tmp_path / "-home-u-proj"
    _write_jsonl(proj / "abc.jsonl", [_user("hello"), _assistant_text("hi")])

    [d] = list_dormant_sessions(tmp_path)
    assert d.session_id == "abc"
    assert d.message_count == 2
    assert d.summary == "hello"
    # Decoded cwd is a best-effort reverse.
    assert str(d.cwd) == "/home/u/proj"
    assert d.file_path == proj / "abc.jsonl"


def test_excludes_listed_run_ids(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    _write_jsonl(proj / "live.jsonl", [_user("a")])
    _write_jsonl(proj / "dead.jsonl", [_user("b")])

    out = list_dormant_sessions(tmp_path, exclude_run_ids=frozenset({"live"}))
    assert {d.session_id for d in out} == {"dead"}


def test_skips_zero_message_files(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    proj.mkdir()
    (proj / "empty.jsonl").write_text("")
    _write_jsonl(proj / "real.jsonl", [_user("hi")])

    out = list_dormant_sessions(tmp_path)
    assert {d.session_id for d in out} == {"real"}


def test_skips_sessions_index_sentinel(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    _write_jsonl(proj / "sessions-index.jsonl", [_user("ignored")])
    _write_jsonl(proj / "real.jsonl", [_user("hi")])

    out = list_dormant_sessions(tmp_path)
    assert {d.session_id for d in out} == {"real"}


def test_sorted_newest_first(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    _write_jsonl(proj / "old.jsonl", [_user("a")])
    # Touch old to be older.
    old_time = time.time() - 3600
    os.utime(proj / "old.jsonl", (old_time, old_time))
    _write_jsonl(proj / "new.jsonl", [_user("b")])

    out = list_dormant_sessions(tmp_path)
    assert [d.session_id for d in out] == ["new", "old"]


def test_summary_truncates_at_80_chars(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    _write_jsonl(proj / "long.jsonl", [_user("x" * 200)])

    [d] = list_dormant_sessions(tmp_path)
    assert d.summary.endswith("…")
    assert len(d.summary) <= 81  # 80 chars + ellipsis


def test_summary_uses_first_user_text_block(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    _write_jsonl(
        proj / "s.jsonl",
        [
            _assistant_text("I'm Claude, what can I help with?"),
            _user("real first user message"),
            _assistant_text("sure"),
        ],
    )

    [d] = list_dormant_sessions(tmp_path)
    assert d.summary == "real first user message"


def test_summary_falls_back_when_no_user_string_msg(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    # Only assistant turns + a tool-result-like user block.
    _write_jsonl(
        proj / "s.jsonl",
        [
            _assistant_text("hello"),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
                },
            },
        ],
    )
    [d] = list_dormant_sessions(tmp_path)
    assert d.summary == "(no summary)"


def test_limit_caps_results(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    for i in range(5):
        _write_jsonl(proj / f"s{i}.jsonl", [_user(f"msg {i}")])

    out = list_dormant_sessions(tmp_path, limit=2)
    assert len(out) == 2


def test_delete_dormant_session_unlinks_file(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    f = proj / "abc.jsonl"
    _write_jsonl(f, [_user("a")])
    assert f.is_file()

    assert delete_dormant_session(f, projects_root=tmp_path) is True
    assert not f.exists()


def test_delete_dormant_session_idempotent_when_missing(tmp_path: Path) -> None:
    """Already-gone file is treated as success — same end state."""
    f = tmp_path / "-x" / "gone.jsonl"
    assert delete_dormant_session(f, projects_root=tmp_path) is True


def test_delete_dormant_session_refuses_outside_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("x")
    other_root = tmp_path / "root"
    other_root.mkdir()

    assert delete_dormant_session(outside, projects_root=other_root) is False
    assert outside.exists()


def test_delete_dormant_session_refuses_non_jsonl(tmp_path: Path) -> None:
    proj = tmp_path / "-x"
    proj.mkdir()
    f = proj / "secret.txt"
    f.write_text("nope")

    assert delete_dormant_session(f, projects_root=tmp_path) is False
    assert f.exists()


def test_archive_root_is_projects_sibling(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    assert archive_root_for(proj) == tmp_path / "archive"


def test_archive_moves_file_preserving_project_subdir(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    f = proj / "-home-u-proj" / "abc.jsonl"
    _write_jsonl(f, [_user("hi")])

    assert archive_dormant_session(f, projects_root=proj) is True
    assert not f.exists()
    assert (tmp_path / "archive" / "-home-u-proj" / "abc.jsonl").is_file()


def test_archive_idempotent_when_source_missing(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    proj.mkdir()
    gone = proj / "-x" / "gone.jsonl"
    assert archive_dormant_session(gone, projects_root=proj) is True


def test_archive_refuses_outside_projects_root(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    proj.mkdir()
    outside = tmp_path / "rogue.jsonl"
    outside.write_text("x")

    assert archive_dormant_session(outside, projects_root=proj) is False
    assert outside.exists()


def test_archive_refuses_non_jsonl(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    f = proj / "-x" / "secret.txt"
    f.parent.mkdir(parents=True)
    f.write_text("x")

    assert archive_dormant_session(f, projects_root=proj) is False
    assert f.exists()


def test_archive_refuses_collision_at_destination(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    src = proj / "-x" / "abc.jsonl"
    _write_jsonl(src, [_user("hi")])
    existing = tmp_path / "archive" / "-x" / "abc.jsonl"
    _write_jsonl(existing, [_user("older copy")])

    assert archive_dormant_session(src, projects_root=proj) is False
    # Both files should be untouched.
    assert src.exists()
    assert existing.read_text().startswith('{"type": "user"')


def test_restore_moves_file_back(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    archived = tmp_path / "archive" / "-x" / "abc.jsonl"
    _write_jsonl(archived, [_user("hi")])

    assert restore_archived_session(archived, projects_root=proj) is True
    assert not archived.exists()
    assert (proj / "-x" / "abc.jsonl").is_file()


def test_restore_refuses_collision_with_existing_dormant(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    archived = tmp_path / "archive" / "-x" / "abc.jsonl"
    _write_jsonl(archived, [_user("archived copy")])
    existing = proj / "-x" / "abc.jsonl"
    _write_jsonl(existing, [_user("live copy")])

    assert restore_archived_session(archived, projects_root=proj) is False
    assert archived.exists()
    assert existing.exists()


def test_restore_refuses_outside_archive_root(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    proj.mkdir()
    rogue = tmp_path / "rogue.jsonl"
    rogue.write_text("x")
    assert restore_archived_session(rogue, projects_root=proj) is False


def test_restore_refuses_non_jsonl(tmp_path: Path) -> None:
    proj = tmp_path / "projects"
    archive = tmp_path / "archive" / "-x"
    archive.mkdir(parents=True)
    f = archive / "secret.txt"
    f.write_text("x")
    assert restore_archived_session(f, projects_root=proj) is False
    assert f.exists()


def test_list_and_count_archived(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_jsonl(archive / "-x" / "a.jsonl", [_user("one")])
    _write_jsonl(archive / "-y" / "b.jsonl", [_user("two")])

    rows = list_archived_sessions(archive)
    assert {d.session_id for d in rows} == {"a", "b"}
    assert count_archived_sessions(archive) == 2


def test_dataclass_is_frozen() -> None:
    """The result type is meant to be immutable for safe sharing."""
    d = DormantSession(
        session_id="x",
        cwd=Path("/p"),
        summary="s",
        mtime=0.0,
        message_count=1,
        file_path=Path("/p/x.jsonl"),
    )
    try:
        d.session_id = "y"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DormantSession should be frozen")
