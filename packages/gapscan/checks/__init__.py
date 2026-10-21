"""The checks, one module each, each a pure function from the scan context to findings.

A check reads the artifacts it is given and returns findings; it does not read files itself,
print, exit, or know about the others. That is what lets the tool's own tests hand a check a
fixture tree and assert exactly which finding it fires.
"""

from __future__ import annotations

from collections.abc import Callable

from packages.gapscan.checks import g1, g2, g3, g4, g9, x1, x2
from packages.gapscan.config import Finding

#: Every check the tool runs, in the order their names sort. The scanner imports this and
#: walks it, so adding a check is adding a module and one line here.
CHECKS: tuple[Callable[..., list[Finding]], ...] = (
    g1.check,
    g2.check,
    g3.check,
    g4.check,
    g9.check,
    x1.check,
    x2.check,
)

__all__ = ["CHECKS", "g1", "g2", "g3", "g4", "g9", "x1", "x2"]
