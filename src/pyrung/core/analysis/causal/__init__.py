"""Recorded, projected, and snapshot causal analysis.

The public :class:`~pyrung.core.runner.PLC` methods select one of three
evidence models:

- Recorded ``cause()`` and ``effect()`` start from a committed historical
  transition and return ``mode="recorded"``.  ``cause()`` walks backward,
  separating transitions that triggered a writer from steady conditions that
  enabled it; a deep walk also classifies terminal :class:`RootCause` objects.
  ``effect()`` walks forward through downstream reads and writes.
- Projected ``cause(to=...)`` and ``effect(from_=...)`` reason from the current
  snapshot and static program graph.  They return ``mode="projected"`` for a
  viable path or ``mode="unreachable"`` with blocking evidence.
- ``why()`` performs a structural backward explanation from a snapshot when no
  scan history is available and returns ``mode="why"``.

Recorded evidence is layered.  Committed history owns transition endpoints.
When interpreted replay is available, its ordered rung-run journal identifies
the exact dynamic writer and the reads visible to each write, including
multiple writes in one scan and parent/subroutine interleaving.  Compact firing
timelines provide the cheaper common path; the PDG narrows fallback candidates,
but replayed execution proves which candidate actually ran.  Filtering,
timeline compression, and recent-state cache residency affect cost and evidence
selection, not the committed historical result.

Implementation modules are split by responsibility: :mod:`recorded` owns the
historical walks, :mod:`projected` owns what-if reachability, :mod:`why` owns
snapshot-only explanations, :mod:`history` resolves transitions, and
:mod:`models` defines the shared result objects.
"""

from .models import (
    BlockerReason,
    BlockingCondition,
    BlockingMove,
    BlockingRelation,
    CausalChain,
    ChainStep,
    EnablingCondition,
    RootCause,
    Transition,
)
from .projected import projected_cause, projected_effect
from .recorded import recorded_cause, recorded_effect
from .why import why_cause

__all__ = [
    "BlockerReason",
    "BlockingCondition",
    "BlockingMove",
    "BlockingRelation",
    "CausalChain",
    "ChainStep",
    "EnablingCondition",
    "RootCause",
    "Transition",
    "why_cause",
    "projected_cause",
    "projected_effect",
    "recorded_cause",
    "recorded_effect",
]
