"""A Person — the human user a backend identifies as the message sender."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Person:
    """A user identified by a backend-specific id.

    `user_id` carries the native id stringified. Feishu open_ids
    pass through; other backends would convert at the wire boundary.
    Everything inside paige sees `str` only.
    """

    user_id: str
    display_name: str = ""
