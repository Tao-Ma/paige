"""Card + Action — interactive UI surfaces and the buttons on them."""

from __future__ import annotations

from dataclasses import dataclass, field

from .conversation import Anchor, Conversation
from .person import Person


def _empty_value() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class Action:
    """A single tappable button on a card.

    `action_id` identifies the handler; `value` is a small dict
    payload carried back when the user taps.

    Adapters encode `(action_id, value)` into native callback data
    (Feishu carries the dict in the card's value JSON). The adapter
    is responsible for any encoding limits.
    """

    label: str
    action_id: str
    value: dict[str, str] = field(default_factory=_empty_value)


@dataclass(frozen=True)
class TextCell:
    """A markdown text cell in a `Card.column_set_rows` row. Renders
    as a Lark `column` with `width="weighted"` and the given `weight`,
    holding one `markdown` element with `content`."""

    content: str
    weight: int = 1


@dataclass(frozen=True)
class ActionCell:
    """A button cell in a `Card.column_set_rows` row. Renders as a
    Lark `column` with `width="auto"` (so the column hugs the button)
    holding a small, primary-styled `button` element wired to
    `action.action_id` / `action.value`."""

    action: Action


@dataclass(frozen=True)
class InputSlot:
    """An editable text input on a card.

    Renders as a Lark `input` element + an inline Send button on the
    Feishu backend. When the user edits the text and submits, the
    channel fires an `ActionEvent` carrying the static
    `(action_id, value)` payload AND the typed text, which the
    inbound parser surfaces as `value['_input']`.

    `default_value` is shown pre-filled in the input on initial
    render — callers seed this from per-binding state so the panel
    learns each user's most-used phrasings over time.
    """

    label: str
    default_value: str
    action_id: str
    value: dict[str, str] = field(default_factory=_empty_value)
    placeholder: str = ""
    submit_label: str = "Send"


@dataclass(frozen=True)
class Card:
    """An interactive card surface — text body + rows of action buttons.

    Optional `header_title` / `header_color` render as a colored
    header strip on the Feishu backend.

    Header colors are Feishu template names and pass through verbatim:
    blue, wathet, turquoise, green, yellow, orange, red, carmine,
    violet, purple, indigo, grey.

    `inputs` render before `rows`. Each slot is an editable text
    field plus its own Send button — see `InputSlot`.
    """

    text: str
    rows: tuple[tuple[Action, ...], ...] = ()
    header_title: str | None = None
    header_color: str | None = None
    inputs: tuple[InputSlot, ...] = ()
    column_set_rows: tuple[tuple[TextCell | ActionCell, ...], ...] = ()
    """Faux-table data rows rendered as Lark `column_set` containers
    (a JSON 2.0 component, so any card with `column_set_rows` forces
    the v2 envelope). Each row is a tuple of cells; cells are
    `TextCell` (markdown content with a weighted width) or
    `ActionCell` (small primary button with auto width). Rendered
    after the body text but before `rows`, with an `hr` separator
    in between when both are present. Used by the `/sessions` Resume
    sub-pane to present one button-per-dormant in a tabular layout
    that's hard to do with stacked single-button rows."""
    status_badge: str | None = None
    """Small status text appended as a trailing footer on the card —
    rendered as a `note` element in JSON 1.0 cards and an italic
    markdown line in JSON 2.0 cards. The badge is owned by
    `StatusCarrierService`, which migrates it via PATCH to the most
    recent outbound card per (person, conversation) so the live
    spinner state is always visible at the bottom of the chat
    surface, never tomb-stoned. Other Card producers should leave
    this field at None — the carrier service stamps it on send."""
    is_status_carrier: bool = False
    """Opt-in flag: when True, this card may be adopted as the live
    status-badge carrier on send. Set by the dispatcher (assistant
    text / tool_use / tool_result / ask_user) and the end-turn panel
    — i.e. the cards that constitute the agent-loop surface. Static
    command responses (`/sessions`, `/server`, `/history`, etc.)
    leave it False so the badge doesn't migrate to a sidebar that
    has nothing to do with what claude is doing right now. Edits
    landing on an existing carrier's anchor stay carriers regardless
    of this flag — only *new-anchor* adoption is gated."""
    force_no_collapse: bool = False
    """Opt-out flag: when True, the Feishu encoder ignores any
    per-conversation `collapse_threshold_lines` from the Outbox stamp
    and renders the body flat. Used by `/livepane` so the live pane
    text stays expanded regardless of the user's collapse-pref
    cycling for the topic — collapsing a live-updating buffer would
    require the user to re-expand on every PATCH."""


@dataclass(frozen=True)
class ActionEvent:
    """Delivered when a user taps an `Action` on a `Card`.

    `card_anchor` points at the card the user tapped — handlers
    edit / delete via this. `ack_token` is adapter-opaque; pass it
    back to the channel's `ack` to dismiss the click animation
    (Feishu card-action response token).
    """

    sender: Person
    conversation: Conversation
    card_anchor: Anchor
    action_id: str
    value: dict[str, str]
    ack_token: str
