"""Static facts about a pyrung program, for how() and the validators.

``_build_prior_facts`` classifies each tag as state, input or neither, gives it
the values it can take, and names what PILOT's world key tracks.  It runs no
search and proves nothing.  PILOT steers with the facts, and each plan it
returns is checked by running the PLC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .passes import _PassContext, _PriorFacts, _run_prior_pipeline
from .results import PENDING as PENDING
from .results import Intractable

if TYPE_CHECKING:
    from pyrung.core.kernel import CompiledKernel
    from pyrung.core.program import Program

__all__ = ["PENDING", "Intractable", "_PriorFacts", "_build_prior_facts"]


def _build_prior_facts(
    program: Program,
    *,
    dt: float = 0.010,
    compiled: CompiledKernel | None = None,
) -> _PriorFacts | Intractable:
    """Build the value domains and state names how() steers with."""
    return _run_prior_pipeline(_PassContext(program=program, dt=dt, compiled=compiled))
