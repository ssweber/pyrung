"""Severity vocabulary shared by all validation findings.

Four levels, from the validator spec:

* ``error``    — provably wrong: no input sequence makes the rung behave as any
  reasonable intent.
* ``warning``  — high-confidence bug pattern; a repair hint is usually offered.
* ``info``     — convention / consistency; auto-fixable where semantics-preserving.
* ``advisory`` — off-by-default or info-level; intent heuristics too weak for a warning.

``SEVERITY_ORDER`` ranks them so callers can threshold (e.g. "fail on >= warning").
"""

from __future__ import annotations

from typing import Literal

Severity = Literal["error", "warning", "info", "advisory"]

SEVERITY_ORDER: dict[Severity, int] = {
    "error": 3,
    "warning": 2,
    "info": 1,
    "advisory": 0,
}
