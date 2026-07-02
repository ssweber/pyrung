"""Compiled coast (pilot B1) — step a coast fork via the compiled kernel.

The interpreted coast (``fold_run_until`` / ``cycle_fold_until``) steps
~5.6 ms/scan: user rungs, a full ``SystemState`` commit, the ejection monitor,
and scan recording.  When the coast settles within a near horizon, we instead
step a ``CompiledPLC`` built from the fork's soft-exec program (holds + plant +
user — the same synthesis overlay the fills replay) at ~1.7 ms/scan, evaluate
the reached/ejection predicates directly on the kernel's plain tag dict (no
per-scan commit), then hand the landing back to the fork.

**B1 is unfolded — it runs every scan.**  That is only sound *and fast* for near
targets; a far target (a 5 min / 1 hr timer — hundreds of thousands of scans)
must fold, so B1 caps its run and returns ``None`` (leaving the fork untouched)
to defer to the interpreted fold path.  B2 will drive the fold arithmetic
(``cycle_fold_until`` / ``fold_run_until``) over this same compiled stepping so
far targets fold to a handful of real compiled scans.

Returning ``None`` (the fork stays untouched) also covers any unsupported
program (no compiled replay kernel): the interpreted fold path owns it.  A coast
that *reaches* or *ejects* within the cap is handed back directly — the compiled
trajectory is bit-equal to the interpreted one, so the landing (and the pilot's
ejection/investigation off it) is the same; only the fold's ``accelerators`` and
its one-scan-late landing differ, both artifacts of *not folding*.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

# Escape hatch + A/B switch.  Set PYRUNG_PILOT_COMPILED_COAST=0 to force the
# interpreted fold path everywhere.
_ENABLED = os.environ.get("PYRUNG_PILOT_COMPILED_COAST", "1") != "0"

# B1 unfolded reach horizon: run at most this many scans before deferring to the
# interpreted fold.  Comfortably covers near coasts (the burner reaches every
# coast in < 1000 scans) while bounding the wasted work on a far target that
# would have folded.  B2 removes this cap.
_UNFOLDED_COAST_CAP = 5000


class _KernelView:
    """Minimal ``state``-like view so the reached/ejected predicates (which read
    ``s.tags.get(...)``) run against the kernel's live tag dict — no per-scan
    ``SystemState`` commit."""

    __slots__ = ("tags",)

    def __init__(self, tags: dict[str, Any]) -> None:
        self.tags = tags


def coast_compiled(
    plc: PLC,
    reached: Callable[[Any], bool],
    *,
    budget: int,
    ejected: Callable[[Any], bool] | None = None,
) -> bool | None:
    """Try a no-fold compiled coast.

    On a clean stop within the unfolded cap — reached or ejected — hand the
    landing back to *plc* and return whether *reached* holds.  Return ``None`` —
    leaving *plc* unmodified — when the coast is unsupported or does not settle
    within the cap; the caller then runs the interpreted fold coast.
    """
    if not _ENABLED:
        return None

    kernel = plc._compiled_replay_supported_kernel()
    if kernel is None:
        return None

    from pyrung.core.compiled_plc import CompiledPLC

    start_state = plc.state
    comp = CompiledPLC(
        plc._soft_exec_program(),
        initial_state=start_state,
        dt=plc._dt,
        compiled=kernel,
    )
    comp._set_rtc_internal(plc._system_runtime._rtc_now(start_state), start_state.timestamp)
    forces = plc._input_overrides.forces
    if forces:
        comp._input_overrides._forces.update(dict(forces))

    view = _KernelView(comp._kernel.tags)  # kernel mutates this dict in place
    cap = min(budget, _UNFOLDED_COAST_CAP)
    for _ in range(cap):
        comp.step_replay()
        if ejected is not None and ejected(view):
            plc._adopt_coast_state(comp._materialize_replay_state())
            return False  # ejected — hand back the ejection state (no reach)
        if reached(view):
            plc._adopt_coast_state(comp._materialize_replay_state())
            return True
    return None  # cap hit without reaching → defer to the interpreted fold
