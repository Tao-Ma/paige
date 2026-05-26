# Contributing to paige

Thanks for the interest. paige is in early-release shape, so the
contribution surface is intentionally narrow right now — bug reports
and adapter contributions (additional IM backends) are the highest-
leverage paths. See the [filing-an-issue](#filing-an-issue) section
before opening one.

## Dev loop

Everything runs inside the `paige-dev` container — no host pip
install needed.

```bash
./do.sh build           # one-time: build the dev image
./do.sh ci              # the full gate — ruff + pyright + import-linter + pytest
./do.sh test            # fast: pytest tests/unit
./do.sh test-all        # slow: tests/unit + tests/integration + tests/e2e
./do.sh shell           # interactive shell inside the container
```

`./do.sh ci` is the same set of checks CI runs. **A PR is ready when
`./do.sh ci` is green.** If a change is small enough that you'd
otherwise hand-wave the test, `./do.sh test` is a reasonable
shortcut while iterating.

On docker-in-docker hosts (where bind mounts don't reach sibling
containers), prefix commands with `PAIGE_SYNC_MODE=copy` to use the
copy-mode persistent container.

## Architecture rule (enforced)

paige is layered:

```
paige.entrypoint                          (composition root + CLI)
paige.application │ paige.adapters        (siblings — can't see each other)
paige.ports
paige.infrastructure
paige.domain                              (pure dataclasses, no I/O)
```

Lower layers don't know about higher ones. **Application and
adapters are siblings**: application code can't import an adapter
directly, and an adapter can't import application code. Everything
crosses through a port (`paige.ports.*`).

`import-linter` runs in `./do.sh ci` and rejects violations. If you
get a contract failure, that's a design signal — usually the right
fix is to introduce or use a port rather than to add an exception.

Four contracts are declared in `pyproject.toml`:

1. The layer order above (no upward imports).
2. `application` ↛ `adapters`.
3. `adapters` ↛ `application`.
4. Production code ↛ `paige.testing` (the fake-adapters package is
   dev-only — it must never end up in a production wheel).

## Style + types

- `ruff` for lint + format (`./do.sh ci` runs both).
- `pyright` in strict mode for the `src/paige/` tree.
- Python 3.12 features are fine (`StrEnum`, `match`, PEP 695
  type aliases, etc.).
- Tests get a slightly looser ruff config (`B` and `N` rules
  ignored under `tests/**`).

## Testing

Three tiers, three purposes:

| Tier | Runs | What it does |
|---|---|---|
| `tests/unit/` | Default `pytest` | Pure data + port-fake-driven service tests. No fs, no tmux, no network. |
| `tests/integration/` | `pytest tests/integration` | Real filesystem, real `libtmux`. Slow. |
| `tests/e2e/` | `pytest tests/e2e` | Full pipeline driven by `mock_claude`. Real `tmux`, real `psutil`. |

When adding a new feature, the default expectation is unit-level
coverage with port fakes. Reach for integration / e2e only when the
new code is doing real I/O that fakes can't validate.

### `paige.testing` — fakes for downstream consumers

The `paige.testing.fakes` package is dev-only, sealed off from
production by an `import-linter` contract, and explicitly excluded
from the production wheel. It exists so downstream projects (and
out-of-tree adapters) can drive paige's domain objects in their own
tests without rolling their own fakes. If you're writing a test that
needs a `Channel`, `Multiplexer`, `Watcher`, or `Storage` stand-in,
look in `paige.testing.fakes` before inventing one.

## Filing an issue

Bug reports: paige logs every inbound + every adapter call to
`~/.paige/paige.log`. The most useful thing you can include is the
relevant log window (a few seconds around the bad behaviour) plus
the wheel version (`./scripts/prod.sh status`).

Feature requests: please describe the use case before the proposed
solution. paige tries hard to keep its abstractions narrow; a
detailed use case helps decide whether a new port / new adapter /
new application service is the right shape.

## Pull requests

Small, focused PRs are easier to review than sprawling ones. If a
change touches more than one layer, consider splitting.

- Include a one-line problem statement and a one-line approach.
- Note which tests were added or updated.
- Confirm `./do.sh ci` is green.
- New env vars need a row in `doc/config.md`, an entry in
  `env.example`, and parsing in `Config.from_env` — see the
  "Adding a new env var" checklist in `doc/config.md`.

## Adapter contributions (e.g. new IM backend)

This is the contribution shape paige is most ready for. A new
backend goes in `src/paige/adapters/<name>/`, implements the
`Channel` port, and gets:

1. Unit tests under `tests/unit/adapters/<name>/` against a
   stubbed version of the backend's SDK (no live credentials).
2. An optional extra in `pyproject.toml` so users opt in to the
   dependency.
3. The selection wiring in `entrypoint/main.py`'s `_build_channel`
   (probably as a config-driven branch, similar to how Feishu is
   wired today).
4. A `doc/` page or a section update in
   `doc/architecture.md`'s "Adapter quirks" listing the backend's
   notable constraints.

For design reference: the [`Channel` port](src/paige/ports/channel.py)
docstring lists the invariants every backend must honour, and
`adapters/feishu/` is the working example.

## License

By contributing, you agree your contributions are licensed under
the MIT license, the same as the rest of the project. See
[`LICENSE`](LICENSE).
