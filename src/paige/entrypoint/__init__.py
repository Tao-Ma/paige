"""Composition root — the only layer allowed to import every other.

This is where concrete adapters meet application services. Tests
import `App` directly and pass fakes; `build_app(config)` is the
production wiring that pulls in `paige.adapters.*`.
"""
