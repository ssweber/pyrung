"""Walk-layer fold shim: re-exports from core/fold.py + walk-specific _advance_time.

The fold engine now lives in ``pyrung.core.fold``.  This module re-exports
everything walk callers need and keeps ``_advance_time`` — the
governing-tag advance loop used by ``steer.py`` — which depends on the
walk-specific ``_DebugSink``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.fold import (
    _EMPTY_CAP,
    _MAX_ADVANCE_ITERS,
    _acc_totals,
    _do_fold,
    _harness_nearest_scan,
    _nearest_acc_crossing,
    _nearest_mod_flip,
    _visible_items,
)

# ── Re-exports from core/fold.py ────────────────────────────────────
# Every name walk callers import stays importable from here.
from pyrung.core.fold import (
    _AccSource as _AccSource,
)
from pyrung.core.fold import (
    _build_fold_context as _build_fold_context,
)
from pyrung.core.fold import (
    _calc_self_referential as _calc_self_referential,
)
from pyrung.core.fold import (
    _collect_acc_sources as _collect_acc_sources,
)
from pyrung.core.fold import (
    _FoldContext as _FoldContext,
)
from pyrung.core.fold import (
    _is_clock_view as _is_clock_view,
)
from pyrung.core.fold import (
    _is_free_running_selfcalc as _is_free_running_selfcalc,
)
from pyrung.core.fold import (
    _match_affine_selfcalc as _match_affine_selfcalc,
)
from pyrung.core.fold import (
    _ModWrap as _ModWrap,
)
from pyrung.core.fold import (
    _scans_to_cross as _scans_to_cross,
)
from pyrung.core.fold import (
    _scans_to_uncross as _scans_to_uncross,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.walk.base import _DebugSink
    from pyrung.core.runner import PLC

# Walk callers still reference _do_jump — alias to the renamed _do_fold.
_do_jump = _do_fold


# ── Walk-specific: the governing-tag advance loop ───────────────────


def _advance_time(
    runner: PLC,
    governing: str,
    from_value: Any,
    ctx: _FoldContext,
    react_cap: int,
    sink: _DebugSink | None = None,
) -> int | None:
    """Hold inputs and advance time until *governing* leaves *from_value*.

    Each iteration's normal scan doubles as the plateau probe: if it changed
    only accumulators (and profile feedback tags), fold the next
    pure-accumulation run to one-before the nearest actionable crossing via
    ``_do_fold``.  *react_cap* bounds consecutive *churn* scans (a visible
    non-accumulator change every scan) before a plateau forms, so an inert or
    oscillating steer bails; productive folding always runs to ``_EMPTY_CAP``
    regardless, so a dwell a pulse merely started is never cut short.  Returns
    the equivalent normal-dt scan count advanced, or ``None`` (fixpoint, churn
    budget exhausted, or iteration guard).

    When the runner has an installed Harness, ``_do_fold`` bumps scan_id by
    *skip* so that scan-indexed feedback patches drain at the correct time.
    Pending harness patches constrain the fold distance.
    """
    used = 0
    iters = 0
    react = 0
    jumps = 0
    mod_idle = 0
    reacted_first = False
    exclude = (
        ctx.acc_names
        | ctx.profile_fb_names
        | ctx.churn_excluded
        | ctx.modwrap_names
        | ctx.mirror_names
    )

    while used < _EMPTY_CAP and iters < _MAX_ADVANCE_ITERS:
        iters += 1

        # ── Probe: one normal scan ───────────────────────────────
        before_tot = _acc_totals(runner.state, ctx.sources)
        before_vis = _visible_items(runner.state, exclude)
        runner.step()
        used += 1

        # ── Check: did the governing tag flip? ───────────────────
        if runner.state.tags.get(governing) != from_value:
            if sink is not None:
                nv = runner.state.tags.get(governing)
                sink.emit(
                    "fold-done",
                    tag=governing,
                    detail=f"from={from_value!r} to={nv!r}, used={used}, jumps={jumps}",
                )
            return used

        # ── Plateau test: did anything visible change? ───────────
        after_vis = _visible_items(runner.state, exclude)
        if after_vis != before_vis:
            # Visible change — program is doing real work, can't fold.
            react += 1
            mod_idle = 0
            if not reacted_first:
                reacted_first = True
                if sink is not None and (jumps > 0 or used > 2):
                    changed = sorted(k for k in after_vis if before_vis.get(k) != after_vis[k])[:10]
                    sink.emit(
                        "fold-react",
                        tag=governing,
                        detail=f"visible change at scan {runner.state.scan_id}: {changed}, react={react}/{react_cap}",
                    )
            if react > react_cap:
                if sink is not None and used > 1:
                    sink.emit(
                        "fold-bail",
                        tag=governing,
                        detail=f"react-cap ({react}>{react_cap}), used={used}",
                    )
                return None
            continue

        # ── Plateau confirmed: compute jump distance ─────────────
        after_tot = _acc_totals(runner.state, ctx.sources)
        acc_scans = _nearest_acc_crossing(ctx, before_tot, after_tot, runner.state)
        mod_scans = _nearest_mod_flip(ctx, runner.state)
        cands = [s for s in (acc_scans, mod_scans) if s is not None]
        skip = min(cands) - 1 if cands else None

        # Constrain fold distance by pending harness feedback patches.
        harness_scan = _harness_nearest_scan(runner)
        if harness_scan is not None:
            gap = harness_scan - runner.state.scan_id - 1
            if gap >= 0:
                skip = min(skip, gap) if skip is not None else gap

        if skip is None:
            # No crossing reachable.
            if runner._harness is not None and any(
                c.active for c in runner._harness._profile_couplings
            ):
                continue
            if sink is not None:
                sink.emit(
                    "fold-bail",
                    tag=governing,
                    detail=f"no-crossing, used={used}",
                )
            return None

        react = 0
        skip = min(skip, _EMPTY_CAP - used)

        # ── Fold ─────────────────────────────────────────────────
        if skip >= 1:
            _do_fold(runner, skip, ctx, before_tot, after_tot)
            used += skip
            jumps += 1

            if runner.state.tags.get(governing) != from_value:
                if sink is not None:
                    nv = runner.state.tags.get(governing)
                    sink.emit(
                        "fold-done",
                        tag=governing,
                        detail=f"from={from_value!r} to={nv!r}, used={used}, jumps={jumps}",
                    )
                return used

        # ── Mod-wrap limit-cycle futility ────────────────────────
        if ctx.mod_period:
            if acc_scans is not None:
                mod_idle = 0
            else:
                mod_idle += 1 + max(skip, 0)
                if mod_idle > ctx.mod_period and _harness_nearest_scan(runner) is None:
                    if sink is not None:
                        sink.emit(
                            "fold-bail",
                            tag=governing,
                            detail=f"mod-limit-cycle, used={used}",
                        )
                    return None

    if sink is not None:
        reason = "iter-cap" if iters >= _MAX_ADVANCE_ITERS else "empty-cap"
        sink.emit("fold-bail", tag=governing, detail=f"{reason}, used={used}")
    return None
