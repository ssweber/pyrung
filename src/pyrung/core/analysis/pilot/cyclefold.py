"""Fold long waits that contain a proven repeating active-hold cycle.

``detect_cycle`` identifies a stable period and its monotone progress
coordinate. ``cycle_fold_until`` advances that coordinate by whole periods,
then executes one real period at normal scan time so cyclic tags retain their
phase and time-dependent resets still run.

If the period, monotone coordinate, or safe crossing cannot be established, the
module declines to fold and the caller continues with ordinary scans.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

if TYPE_CHECKING:
    from pyrung.core.fold import _FoldContext
    from pyrung.core.runner import PLC
    from pyrung.core.state import SystemState


@dataclass(frozen=True)
class _Cycle:
    """A limit cycle detected over a stream of per-scan snapshots.

    *period* is P scans.  *monotone* maps each tag that advances by a constant,
    nonzero numeric delta **per period** (forward in time) to that delta.  Every
    other tag is *boundary-stable*: equal at ``snap[i]`` and ``snap[i+P]`` — a
    constant, or a cyclic tag that returns to its value each period (the
    oscillation, the watchdog reflections).
    """

    period: int
    monotone: dict[str, float]


def _is_number(v: Any) -> TypeGuard[int | float]:
    """Numeric for delta arithmetic — bools are excluded (they are not progress)."""
    return not isinstance(v, bool) and isinstance(v, (int, float))


def detect_cycle(
    snaps: Sequence[Mapping[str, Any]],
    *,
    monotone_allowed: frozenset[str] | None = None,
    significant_keys: frozenset[str] | None = None,
    period_multiple_of: int = 1,
    max_period: int = 64,
    min_repeats: int = 2,
) -> _Cycle | None:
    """Smallest period P explaining the tail of *snaps* as a clean limit cycle.

    *snaps* is consecutive per-scan full snapshots, **oldest first, newest last**.
    For a candidate P, anchor on the newest snapshot and step back P at a time for
    ``min_repeats + 1`` anchors.  P is accepted iff **every** tag is either

    * *boundary-stable* — equal across all anchors (a constant, or a cyclic tag
      that returns to its value every period), or
    * *monotone* — strictly numeric with one constant per-period delta across
      every adjacent anchor pair, **and** permitted by *monotone_allowed*.

    *period_multiple_of* restricts the search to periods that are whole multiples
    of it — used to align P with the read system clocks' full periods so the
    observed window spans each clock's full cycle (capturing its net effect) and
    every whole-period jump preserves each clock's phase (so ``rise()``/``fall()``
    ``_prev`` stays consistent across the skip).  ``1`` searches every period.

    *monotone_allowed* is the soundness governor.  A tag like ``i % 3`` reads
    ``[2, 1, 0]`` over three consecutive scans — locally indistinguishable from a
    linear ramp — so a purely empirical detector would extrapolate it past its
    wrap and patch a bogus value.  Pass the set of tags statically *certified*
    monotone within a regime (the fold context's ``acc_names``: timer / counter
    accumulators); any other ramping tag then forces P up to its true period by
    failing the boundary-stable test until the window spans a full cycle.  ``None``
    trusts any constant-delta numeric — for tests / callers that have already
    bounded the input — and is **not** safe against modular tags.

    *significant_keys* restricts classification to a fixed, caller-proven tag
    surface.  This lets callers retain immutable full-state snapshots instead
    of materializing a filtered dictionary per scan.  When omitted, the
    historical union-of-snapshot-keys behavior is preserved.

    Returns the :class:`_Cycle` for the smallest accepted P, or ``None`` if the
    data is too short or no clean cycle exists at any P ≤ *max_period*.

    Requiring the deltas to hold over ``min_repeats`` periods is the
    observe-before-skip guard: a transient that has not yet settled into a clean
    cycle is rejected, exactly as the runner fold only marks a clock inert after
    observing a full period unchanged.  ``P = 1`` with an empty *monotone* is the
    ordinary plateau; ``P = 1`` with one monotone tag is the ordinary accumulator
    ramp — both already handled by the runner fold, so callers invoke this only
    once that fold has stalled.
    """
    n = len(snaps)
    step = max(1, period_multiple_of)
    for period in range(step, max_period + 1, step):
        # min_repeats+1 anchors P apart: indices n-1, n-1-P, …, n-1-period*min_repeats.
        if period * min_repeats >= n:
            break  # too short for this P — and for every larger P, so stop.
        anchors = [snaps[n - 1 - period * r] for r in range(min_repeats + 1)]
        if significant_keys is None:
            discovered_keys: set[str] = set()
            for a in anchors:
                discovered_keys.update(a.keys())
            keys = discovered_keys
        else:
            keys = significant_keys

        monotone: dict[str, float] = {}
        ok = True
        for key in keys:
            vals = [a.get(key) for a in anchors]  # newest → oldest
            if all(v == vals[0] for v in vals):
                continue  # boundary-stable
            if monotone_allowed is not None and key not in monotone_allowed:
                ok = False  # ramping tag not certified monotone — wrong P
                break
            nums: list[float] = []
            for v in vals:
                if not _is_number(v):
                    ok = False  # non-numeric tag that is not boundary-stable
                    break
                nums.append(float(v))
            if not ok:
                break
            # Forward-in-time per-period deltas (newer − older) across anchor pairs.
            deltas = [nums[r] - nums[r + 1] for r in range(min_repeats)]
            d0 = deltas[0]
            if d0 == 0 or any(d != d0 for d in deltas):
                ok = False
                break
            monotone[key] = d0

        if ok:
            return _Cycle(period=period, monotone=monotone)
    return None


# ── Crossing bound (in period units) ─────────────────────────────────


def _periods_to_crossing(
    cyc: _Cycle,
    state: SystemState,
    fold_ctx: _FoldContext,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
) -> int | None:
    """Whole periods until the **first** regime change among the monotone coords.

    A regime change is any comparison the program reads on a monotone coordinate
    flipping, or that coordinate's timer/counter ``Done`` tripping (the preset
    crossing).  Lifts ``core/fold``'s per-scan crossing arithmetic to per-period
    granularity by passing the per-period delta as the rate.

    Returns the nearest such crossing in periods (≥ 1), or ``None`` when there is
    nothing to bound against or any threshold is unresolvable — both **fail
    closed**: the caller then does not fold and steps instead, so a coordinate we
    cannot bound is never skipped past.
    """
    k, live = _monotone_read_surface(cyc, state, fold_ctx, extra_comparisons) or (None, -1)
    if live == 0:
        return None  # all coordinates inert — caller handles via the surface split
    return k


def _monotone_read_surface(
    cyc: _Cycle,
    state: SystemState,
    fold_ctx: _FoldContext,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
) -> tuple[int | None, int] | None:
    """Partition the monotone coordinates by their readable future.

    For each coordinate, its read surface is the preset (Done flip) plus every
    comparison the program or the armed predicate reads on it.  A coordinate
    with a *future* crossing among those bounds is **live**; one whose every
    resolvable bound is already crossed and receding is **inert** — monotone
    motion means each of its readers is frozen forever, so it cannot cause any
    future regime change.  (Data reads self-protect: a copy of the acc puts
    the destination in the observation ring, which breaks the cycle.)

    Returns ``(k, live_count)`` where ``k`` is the nearest live crossing in
    whole periods (``None`` when no live coordinate bounds the jump), or
    ``None`` outright to fail closed (non-up coordinate, non-numeric value,
    an unresolvable threshold, or a coordinate with no resolvable surface at
    all).
    """
    from pyrung.core.fold import _acc_totals, _progress_bound, _resolve_num, _scans_to_cross

    sources = {s.acc_name: s for s in fold_ctx.sources}
    totals = _acc_totals(state, fold_ctx.sources)
    best: int | None = None
    live = 0
    for tag, d in cyc.monotone.items():
        src = sources.get(tag)
        # v1: only certified up-accumulators (timers / count-up) are foldable.
        if src is None or src.kind != "up" or d <= 0:
            return None
        cur = totals.get(tag)
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            return None
        prog = float(cur)

        bounds: list[tuple[float, bool]] = []
        preset = _resolve_num(src.preset, state)
        if preset is not None:
            bounds.append((preset, False))  # Done flips at acc ≥ preset
        cmps = fold_ctx.comparisons.get(tag, ())
        if extra_comparisons:
            cmps = cmps + extra_comparisons.get(tag, ())
        for form, operand in cmps:
            kv = _resolve_num(operand, state)
            if kv is None:
                return None  # unresolved threshold — fail closed
            bounds.append(_progress_bound("up", form, kv))
        if not bounds:
            return None  # no resolvable surface at all — fail closed

        tag_best: int | None = None
        for target, strict in bounds:
            periods = _scans_to_cross(prog, d, target, strict)
            if periods is not None:
                tag_best = periods if tag_best is None else min(tag_best, periods)
        if tag_best is None:
            continue  # inert: every bound already behind and receding
        live += 1
        best = tag_best if best is None else min(best, tag_best)
    return best, live


# ── Coast loop ───────────────────────────────────────────────────────


def cycle_fold_until(
    plc: PLC,
    predicate: Callable[[SystemState], bool],
    *,
    budget: int,
    fold_ctx: _FoldContext | None = None,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
    max_period: int = 64,
    min_repeats: int = 2,
    predicate_reads: frozenset[str] | None = None,
    stats: dict[str, int] | None = None,
    advances: list[tuple[str, Any]] | None = None,
) -> bool:
    """Coast *plc* until *predicate*, folding active-hold limit cycles.

    Steps scan-by-scan (so any installed reactive holds / oscillations and the
    ejection pause-guard run every scan), detects the limit cycle once it settles,
    and folds it the engineer's way: **patch the monotone accumulator(s) forward by
    whole periods and step the remainder** — never the dt-knob, so the sub-cycle
    that must keep running (the oscillation, the watchdog pet) is preserved.

    The whole-period jump is what makes the landing bit-equal to scan-by-scan: a
    multiple-of-P scan skip leaves every cyclic tag at exactly its current phase,
    and each monotone accumulator's invariant ``acc == rate·(scan_id − start)`` is
    preserved because the patch and the scan_id stamp advance in lockstep.

    *budget* counts **real** scans (the fold spends almost none — a soak of any
    length costs only the warm-up + one landing period).  Returns whether
    *predicate* holds at exit; ``stats`` (if given) collects logical scans,
    kernel scans, macro folds, skipped scans, and ``sterile_cycle`` for
    diagnostics.  The original ``real_scans`` / ``folds`` keys remain as
    compatibility aliases.

    **Sterile-cycle proof** (*predicate_reads* required): a certified *exact*
    cycle — every observed tag repeating, no monotone coordinate — on a
    deterministic machine with held inputs loops forever, so no predicate over
    ring-covered tags can ever flip.  When the predicate's reads are all
    ring-covered and the harness has nothing scheduled or actively driving,
    the coast stops immediately (``stats["sterile_cycle"] = 1``) instead of
    burning the rest of its budget proving nothing new.

    Soundness mirrors the runner fold: observe ≥ ``min_repeats`` periods before
    trusting the cycle, bound every jump at the nearest comparison/preset crossing,
    and re-detect after each landing (the ring is cleared, so a regime change forces
    fresh observation).  Fails closed everywhere it cannot certify a jump.
    """
    import math

    from pyrung.core.fold import _acc_totals, _harness_nearest_scan, fold_run_until

    if fold_ctx is None:
        fold_ctx = plc._ensure_fold_context()
    assert fold_ctx is not None
    dt = fold_ctx.normal_dt

    # Scan-id-derived signals (scan_clock_toggle / scan_counter) change *every*
    # scan with no periodic timestamp edge to align to — no sound jump exists, so
    # degrade to the runner fold (still skips clean plateaus, just not the cycle).
    if fold_ctx.scan_derived_names or dt <= 0:
        fold_run_until(
            plc,
            predicate,
            max_cycles=budget,
            fold_ctx=fold_ctx,
            extra_comparisons=extra_comparisons,
            stats=stats,
        )
        return bool(predicate(plc.state))

    # Align the cycle period to every read system clock's *full* period (in scans)
    # so the observed window spans each clock's whole cycle (its net effect is
    # captured) and every whole-period jump preserves each clock's phase (exact
    # timestamp + same phase ⇒ rise()/fall() _prev stays consistent across the
    # skip).  This is the runtime form of the soft-clock partition: instead of
    # statically proving an edge inert, we observe a full clock cycle and confirm
    # the net change is boundary-stable or a certified accumulator.
    read_hps = [
        *fold_ctx.clock_half_periods,
        *(hp for _n, hp in fold_ctx.soft_clocks),
        *(hp for _n, hp, _a in fold_ctx.sat_clocks),
    ]
    period_multiple_of = 1
    for hp in read_hps:
        full_scans = 2.0 * hp / dt
        r = round(full_scans)
        if r <= 0 or abs(full_scans - r) > 1e-6:
            # A read clock not on the scan grid — no aligned period exists.
            fold_run_until(
                plc,
                predicate,
                max_cycles=budget,
                fold_ctx=fold_ctx,
                extra_comparisons=extra_comparisons,
                stats=stats,
            )
            return bool(predicate(plc.state))
        period_multiple_of = math.lcm(period_multiple_of, r)
    if period_multiple_of > 4096:  # pathological clock LCM — give up on the cycle
        fold_run_until(
            plc,
            predicate,
            max_cycles=budget,
            fold_ctx=fold_ctx,
            extra_comparisons=extra_comparisons,
            stats=stats,
        )
        return bool(predicate(plc.state))
    max_period = max(max_period, period_multiple_of * 4)

    monotone_allowed = frozenset(s.acc_name for s in fold_ctx.sources if s.kind == "up")
    # Classify over the runner fold's *significant* set: drop the tags it already
    # treats as don't-care for the plateau (resolved-on-read-driven frozen writes,
    # unread churn, harness feedback) so they don't spuriously break the cycle.
    # Accumulators stay in — they are the monotone coordinates.
    ignore = fold_ctx.frozen_writes | fold_ctx.churn_excluded | fold_ctx.profile_fb_names
    significant_keys = frozenset(plc.state.tags) - ignore
    ring: list[Mapping[str, Any]] = []
    ring_cap = max_period * (min_repeats + 2) + 4
    real_scans = 0
    folds = 0
    # The sterile proof holds only when every tag the predicate reads is
    # covered by the ring (an excluded tag could change without breaking the
    # observed cycle).  System clocks are safe uncovered: the period is
    # aligned to their full cycles, so their pattern repeats with the ring.
    sterile_eligible = predicate_reads is not None and not (predicate_reads & ignore)
    # Detection cadence: detect_cycle is O(window x tags); running it every
    # scan roughly doubles the cost of every stepped scan while certification
    # delayed by a few scans costs nothing against the post-fold
    # re-observation window.  Every scan would only certify ≤7 scans sooner.
    detect_every = 8
    since_detect = 0
    start_scan = plc.state.scan_id

    def _finish(reached: bool) -> bool:
        if stats is not None:
            logical_scans = plc.state.scan_id - start_scan
            stats["logical_scans"] = logical_scans
            stats["kernel_scans"] = real_scans
            stats["macro_folds"] = folds
            stats["skipped_scans"] = logical_scans - real_scans
            stats["scan_by_scan_counterfactual"] = logical_scans
            stats["saved_kernel_scans"] = logical_scans - real_scans
            stats["real_scans"] = real_scans
            stats["folds"] = folds
        return reached

    def _harness_quiet() -> bool:
        harness = getattr(plc, "_harness", None)
        if harness is None:
            return True
        if harness.pending_count > 0:
            return False
        snap = plc.current_state.tags
        for c in getattr(harness, "_profile_couplings", ()):
            en_raw = snap.get(c.en_name, False)
            enabled = en_raw == c.trigger_value if c.trigger_value is not None else bool(en_raw)
            if enabled:
                return False  # an analog coupling is still driving its ramp
        return True

    while real_scans < budget:
        plc._consume_pause_request()
        plc._run_single_scan(consume_pause_request=False)
        real_scans += 1
        paused = plc._consume_pause_request()
        if predicate(plc.state) or paused:
            return _finish(bool(predicate(plc.state)))

        # SystemState tag maps are immutable persistent maps. Retain them
        # directly and let the detector read only the precomputed significant
        # surface instead of allocating a filtered full-state dictionary after
        # every scan.
        # Timers can make real sub-tick progress while their integer Acc tag is
        # unchanged.  The ordinary runner fold already treats ``Acc + _frac``
        # as the scalar coordinate; retain that same reading here so active
        # corrective rungs do not turn a live timer into a false sterile cycle.
        # The persistent-map update keeps the immutable tag image structurally
        # shared and replaces only the owner-coordinate readings.
        ring.append(plc.state.tags.update(_acc_totals(plc.state, fold_ctx.sources)))
        if len(ring) > ring_cap:
            del ring[0]

        since_detect += 1
        if since_detect < detect_every:
            continue
        since_detect = 0

        cyc = detect_cycle(
            ring,
            monotone_allowed=monotone_allowed,
            significant_keys=significant_keys,
            period_multiple_of=period_multiple_of,
            max_period=max_period,
            min_repeats=min_repeats,
        )
        if stats is not None:
            key = (
                "detect_none"
                if cyc is None
                else ("detect_exact" if not cyc.monotone else "detect_monotone")
            )
            stats[key] = stats.get(key, 0) + 1
        if cyc is None:
            continue
        surface = (
            _monotone_read_surface(cyc, plc.state, fold_ctx, extra_comparisons)
            if cyc.monotone
            else (None, 0)
        )
        if surface is None:
            if stats is not None:
                stats["k_none"] = stats.get("k_none", 0) + 1
            continue  # unresolvable coordinate — fail closed, step
        k, live = surface
        if live == 0:
            # An exact limit cycle on every *influential* tag: the ring
            # repeats forever and any remaining monotone coordinate is
            # read-frozen (every bound already crossed and receding), so
            # nothing observable can ever change.  If the predicate reads
            # only ring-covered tags and the harness has nothing scheduled
            # or actively driving, this coast can never reach anything —
            # stop now instead of burning the budget.
            if sterile_eligible and _harness_nearest_scan(plc) is None and _harness_quiet():
                if stats is not None:
                    stats["sterile_cycle"] = 1
                return _finish(False)
            continue

        if k is None or k <= 1:
            if stats is not None:
                key = "k_none" if k is None else "k_small"
                stats[key] = stats.get(key, 0) + 1
            continue  # crossing within the next period — step it, do not fold

        # Fold whole periods only.  Bound by the crossing (k-1 periods) and by the
        # next scheduled harness feedback (a non-clock, non-comparison regime edge).
        periods_to_jump = k - 1
        harness_scan = _harness_nearest_scan(plc)
        if harness_scan is not None:
            room = harness_scan - plc.state.scan_id - 1
            periods_to_jump = min(periods_to_jump, max(0, room // cyc.period))
        if periods_to_jump < 1:
            continue

        # Patch each monotone acc forward, stamp the skipped scans onto scan_id /
        # timestamp so the acc invariant and every clock phase hold across the gap.
        cur = plc.state.tags
        progress_now = _acc_totals(plc.state, fold_ctx.sources)
        sources = {source.acc_name: source for source in fold_ctx.sources}
        jump_scans = periods_to_jump * cyc.period
        patches: dict[str, int | float] = {}
        fractions: dict[str, float] = {}
        for tag, delta in cyc.monotone.items():
            source = sources.get(tag)
            if source is not None and source.timed:
                total = progress_now[tag] + delta * periods_to_jump
                whole = int(total)
                patches[tag] = whole
                fractions[f"_frac:{tag}"] = total - whole
            else:
                patches[tag] = cur[tag] + round(delta * periods_to_jump)
        if advances is not None:
            # Presentation receipt: these are the exact accumulator edits a
            # technician would make to reproduce the proved-safe jump.
            advances.extend(patches.items())
        plc.patch(patches)
        memory = plc._state.memory
        for key, value in fractions.items():
            memory = memory.set(key, value)
        plc._state = plc._state.set(
            memory=memory,
            scan_id=plc._state.scan_id + jump_scans,
            timestamp=plc._state.timestamp + jump_scans * dt,
        )
        folds += 1
        ring.clear()  # consecutive gap — re-observe before trusting the cycle again

    return _finish(bool(predicate(plc.state)))
