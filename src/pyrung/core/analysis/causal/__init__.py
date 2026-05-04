"""Causal chain analysis for pyrung programs.

Recorded backward walk (``cause``): from a tag transition, walk history
backward using per-rung SP-tree attribution to find proximate causes and
enabling conditions.

Recorded forward walk (``effect``): from a tag transition, walk history
forward using counterfactual SP evaluation to find downstream effects.

Projected walks (``cause(to=)``, ``effect(from_=)``): project from the
current state using the static PDG to find reachable causal paths.
Returns ``mode='projected'`` when reachable, ``mode='unreachable'`` with
populated ``blockers`` when not.

PDG fallback for filtered firing logs
-------------------------------------

The recorded walks consume the rung-firings log via ``rung_firings_fn``.
Under PDG-filtered capture (see ``context.py::capturing_rung``), writes
to non-Bool tags that no rung reads are dropped from the log at
capture time — the filter saves memory on internal churn like timer
accumulators.  Bool tags are always kept regardless of read status
(low cardinality, user-facing state transitions, common target of
``cause()``) so the direct-log path handles the typical case.

The filter's *downstream* claim (a write that no rung reads can't
matter to analysis) holds for the **recursive** walk step: once we
identify the writing rung, its causes are reads by upstream rungs,
all of which are consumed-and-therefore-logged by definition.  The
filter's claim **fails at the root step** whenever the analysis
target is a terminal **non-Bool** output — e.g. ``cause("Timer_Acc")``
where nothing reads the accumulator.  (Terminal Bool outputs like
``Alarm_Horn`` are preserved by the Bool-keep rule and hit the
direct-log path.)  Without a fallback, the firing log would lack
the rung that wrote the non-Bool terminal, and the chain's first
step would never materialize.

The fix is a PDG fallback keyed off the static ``writers_of`` /
``readers_of`` sets.  When the firing log doesn't identify a writer
for a transition, :func:`_fallback_writers_from_pdg` iterates the
PDG's static writers and re-evaluates each candidate's SP-tree
against the historical state at that scan.  A candidate whose tree
evaluates True is treated as the writer.  Symmetric logic widens
the effect forward walk: for each scan, rungs missing from the log
but reading a current frontier tag are re-entered and evaluated
with PDG-synthesized candidate writes (history then filters via
``_find_transition_at_scan``).

Trade-off: the fallback adds one SP-tree eval per candidate rung
per unresolved step — bounded by ``len(writers_of[tag])``, typically
1–2.  Memory/correctness both preserved; the filter's cache miss
turns into a handful of extra evaluations, not a lost answer.

``FiredOnly`` rungs deliberately do *not* round-trip through
``cause``'s value match: their synthesized writes carry a sentinel
that never equals a real transition value.  Such rungs drop out of
recorded backward chains past their promotion point.  The
assumption is monotonic counters don't carry useful causal signal;
analysis that truly needs the value replays to the scan.
"""

from .models import (
    BlockerReason,
    BlockingCondition,
    CausalChain,
    ChainStep,
    EnablingCondition,
    Transition,
)
from .projected import projected_cause, projected_effect
from .recorded import recorded_cause, recorded_effect

__all__ = [
    "BlockerReason",
    "BlockingCondition",
    "CausalChain",
    "ChainStep",
    "EnablingCondition",
    "Transition",
    "projected_cause",
    "projected_effect",
    "recorded_cause",
    "recorded_effect",
]
