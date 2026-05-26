"""Run — a Claude Code conversation."""

from pathlib import Path

from paige.domain.run import Run
from paige.domain.transcript import Transcript


def _t() -> Transcript:
    return Transcript(run_id="abc-uuid", file_path=Path("/tmp/abc.jsonl"))


def test_run_required_fields() -> None:
    r = Run(run_id="abc-uuid", cwd=Path("/proj"), transcript=_t())
    assert r.run_id == "abc-uuid"
    assert r.cwd == Path("/proj")
    assert r.transcript.run_id == "abc-uuid"
    assert r.summary == ""
    assert r.message_count == 0
    assert r.total_tokens == 0
    assert r.last_modified_ms == 0


def test_run_with_summary_fields() -> None:
    r = Run(
        run_id="abc",
        cwd=Path("/proj"),
        transcript=_t(),
        summary="feishu adapter polish",
        message_count=47,
        total_tokens=12_400,
        last_modified_ms=1700000000000,
    )
    assert r.summary == "feishu adapter polish"
    assert r.message_count == 47
    assert r.total_tokens == 12_400
