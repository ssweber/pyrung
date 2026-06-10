"""Corridor walker — the interpreted forward planner behind ``plc.how()``.

The walker plans by forking and stepping the real interpreter and verifies
every plan by replay; the prover (``..prove``) verifies exhaustively via the
compiled kernel.  See ``CLAUDE.md`` in this package for the contract.
"""

from pyrung.core.analysis.walk.engine import plan_walk

__all__ = ["plan_walk"]
