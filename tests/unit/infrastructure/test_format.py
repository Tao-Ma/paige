"""format_bytes / format_duration helpers."""

from __future__ import annotations

from paige.infrastructure.format import format_bytes, format_duration


def test_format_bytes_none() -> None:
    assert format_bytes(None) == "—"


def test_format_bytes_b_kb_mb_gb() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"
    assert format_bytes(1024 * 1024 * 1024) == "1.0 GB"


def test_format_duration_ranges() -> None:
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(60) == "1m 0s"
    assert format_duration(125) == "2m 5s"
    assert format_duration(3600) == "1h 0m"
    assert format_duration(3600 + 30 * 60) == "1h 30m"
    assert format_duration(86400) == "1d 0h"
    assert format_duration(86400 * 2 + 3600 * 5) == "2d 5h"


def test_format_duration_negative_clamps_to_zero() -> None:
    assert format_duration(-5) == "0s"
