"""Person — frozen, value-equality, defaults."""

import pytest

from paige.domain.person import Person


def test_person_required_fields() -> None:
    p = Person(user_id="ou_abc")
    assert p.user_id == "ou_abc"
    assert p.display_name == ""


def test_person_with_display_name() -> None:
    p = Person(user_id="123", display_name="Alice")
    assert p.display_name == "Alice"


def test_person_value_equality() -> None:
    a = Person(user_id="ou_abc", display_name="x")
    b = Person(user_id="ou_abc", display_name="x")
    assert a == b
    assert hash(a) == hash(b)


def test_person_immutable() -> None:
    p = Person(user_id="x")
    with pytest.raises(Exception):  # FrozenInstanceError
        p.user_id = "y"  # type: ignore[misc]
