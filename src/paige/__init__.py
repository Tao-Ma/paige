"""Paige — message bridge between a code agent (Claude Code) and people via IM.

The package is organized in layers; lower layers know nothing of higher ones.

  domain          pure data + rules. No I/O, no third-party libs beyond
                  the standard library + dataclasses.
  ports           Protocol interfaces — the "what" we depend on.
  adapters        concrete implementations of ports (Feishu, tmux,
                  filesystem). Free to import third-party libs.
  application     use cases. Imports `domain` + `ports` only — never adapters
                  directly. Wired together at composition.
  infrastructure  cross-cutting concerns (config, logging, lifecycle).
  entrypoint      composition root + CLI; the only place imports cross all
                  layers.

The layering is enforced by `import-linter` (see `pyproject.toml`).
"""

__version__ = "0.1.0"
