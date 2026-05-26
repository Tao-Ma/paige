"""Smoke test — verifies the package imports cleanly.

Until slice 1 fills in domain models, this is the minimum signal that
ruff / pyright / pytest are all wired correctly against an empty
package skeleton. Replace as real tests land.
"""

import paige


def test_paige_imports() -> None:
    assert paige.__version__ == "0.1.0"
