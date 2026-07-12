"""Cause-chain walker — root finding for PILOT over the deep ``cause()`` walk.

Shared by the gate pipeline (excursion diagnosis), the outcome classifier
(causal attribution), and investigation (hypothesis generation).

The recorded ``cause()`` walk is *deep*: it chases each step's held supports
(temporal + absence hops) and classifies the terminals into
``CausalChain.roots``.  These walkers **consume** that single deep chain — the
held/reset-blocked steps and the classified roots — instead of re-walking the
history themselves.  A pipeline destination the old shallow walk dead-ended on
(a jump-table copy gated by a held ``StateRequested``) is now crossed natively
by the deep walk, so the old route-inversion *compass bridge* is gone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.causal.models import CausalChain
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


def _reference_constants(plc: PLC) -> frozenset[str]:
    """Lookup-table reference constants for *plc*'s program, cached per fork.

    ``never_written`` roots include the never-written copy sources that feed a
    jump table's pointer chain (``compute_reference_constants``, trace.py) —
    declared program constants, not field levers.  Consuming them as nogoods or
    folding them into chain membership would flood the pilot's avoid/nogood
    logic with lookup-table plumbing, so both walkers demote them.

    Computed once per fork (program-static: it depends only on the program shape
    and the external flags, both fixed for a fork's lifetime) and cached on the
    fork's ``__dict__`` alongside the chase memo.  ``frozenset()`` for a
    logic-list PLC with no ``Program`` (the reference-constant shape needs the
    ``Program`` rung/subroutine structure).
    """
    cached = plc.__dict__.get("_pilot_ref_constants")
    if cached is not None:
        return cached
    result: frozenset[str] = frozenset()
    program = getattr(plc, "_program", None)
    if program is not None and hasattr(program, "rungs"):
        try:
            from pyrung.core.analysis.pilot.trace import compute_reference_constants

            result = compute_reference_constants(
                plc._ensure_pdg(), program, plc._known_tags_by_name
            )
        except Exception:  # noqa: BLE001
            logger.debug("pilot causal: reference-constant computation failed", exc_info=True)
            result = frozenset()
    plc.__dict__["_pilot_ref_constants"] = result
    return result


def _changed_in_window(
    plc: PLC,
    start_scan: int,
    end_scan: int,
    relevant: frozenset[str],
) -> frozenset[str]:
    """Subset of *relevant* whose recorded value changed inside the window."""
    if not relevant:
        return frozenset()
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return frozenset()
    changed: set[str] = set()
    for prev, cur in zip(states, states[1:], strict=False):
        for tag in relevant:
            if tag not in changed and not _values_match(prev.tags.get(tag), cur.tags.get(tag)):
                changed.add(tag)
    return frozenset(changed)


def empirical_program_writes(
    plc: PLC,
    candidates: frozenset[str],
    *,
    start_scan: int,
    end_scan: int,
    pilot_touched: frozenset[str],
) -> frozenset[str]:
    """Steerable *candidates* the RECORDED RUN testifies the PROGRAM wrote.

    Static steerability is a *hypothesis*; the recorded run is *testimony*.  This
    is **"Verify is the sole source of CONFIRMED" applied to classification**: a
    candidate that changed value inside ``[start_scan, end_scan]`` at a scan where
    the pilot held no hold on it and issued no pulse to it — ``pilot_touched``
    names every tag the pilot's own fully-known actions (hold_log / applied
    overlays / journey) could have moved — was moved by the *program*, so it is
    not a free lever in the live context.

    **Fail-safe: positive evidence only.**  A candidate the pilot touched, or one
    that never changed in the window, keeps its static verdict unchanged.  The
    function only ever *demotes* (returns a subset of ``candidates``); it never
    promotes anything, and no recorded evidence returns the empty set.
    """
    suspects = frozenset(candidates) - frozenset(pilot_touched)
    return _changed_in_window(plc, start_scan, end_scan, suspects)


def pilot_touched_tags(
    hold_log: Any = (),
    journey: Any = (),
    rungs: Any = (),
) -> frozenset[str]:
    """Every tag the pilot's own actions could have moved.

    The union of held tags (``hold_log`` entries' ``.tags`` + the live
    ``rungs`` keys) and pulsed / applied inputs (each ``journey`` step's
    ``.inputs``).  Consumed by :func:`empirical_program_writes` as the exclusion
    set so a demotion never mistakes the pilot's own write for the program's.
    """
    touched: set[str] = set(rungs or ())
    for entry in hold_log or ():
        for pair in getattr(entry, "tags", ()):
            touched.add(pair[0])
    for step in journey or ():
        touched.update(getattr(step, "inputs", {}) or {})
    return frozenset(touched)


def chase_cause_roots(
    plc: PLC,
    tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None = None,
    bridge: Any | None = None,
    empirical_writes: frozenset[str] | None = None,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Chase the deep ``cause()`` chain to steerable-input roots.

    Returns ``(nogoods, holds)`` where:
    - *nogoods*: steerable inputs whose transition caused the regression
    - *holds*: ``(tag, value)`` pairs for inputs that must stay at their
      pre-transition value to prevent the regression

    The single deep chain already crossed every held-support hop (temporal +
    absence) and classified its terminals, so the walk here is a graph traversal
    over the chain's steps that stops at the *nearest* steerable lever — no
    history re-walking.  When no steerable lever *moved*, the absence causes are
    read off the chain's held/reset-blocked steps and ``roots`` (external /
    never-written, minus lookup-table reference constants): a sensor that never
    moved starving a watchdog is the hold, even with no mover.

    *empirical_writes* (opt-in) is the **empirical steerable veto**
    (:func:`empirical_program_writes`): tags that look steerable to the static
    classifier but that the recorded run shows the *program* wrote in the incident
    window.  Such a tag must not be a terminal nogood — the walk descends through
    it toward the real root.  ``None`` = the exact prior behavior; positive
    evidence only ever demotes, never promotes.

    *bridge* is accepted but ignored — the deep walk crosses the opaque-pipeline
    hop the compass bridge used to invert.  Retained only so investigation (which
    still passes ``bridge=ctx``) keeps working; deletable once that caller drops
    the keyword.
    """
    # Empirical veto: demote statically-steerable tags the recorded run shows the
    # PROGRAM wrote, so the walk descends through them instead of nogood-stopping
    # (see ``empirical_program_writes``).  Purely a subtraction from ``steerable``.
    steerable_eff = steerable - empirical_writes if empirical_writes else steerable

    # Cross-chase result memo, stored on the fork.  chase_cause_roots is pure for
    # a fixed fork — ``cause()`` is pure for a fixed fork (see ``_cause``) and a
    # fork's recorded history at a *past* scan is immutable — so
    # ``(tag, scan, steerable_eff) -> (nogoods, holds)`` is stable for the fork's
    # lifetime.  The verify loops re-chase the same key dozens of times, so the
    # memo saves ~95% of ``cause()`` calls.  ``fork()`` / ``load_world()`` hand
    # back a fresh fork with an empty memo, so it is invalidated by construction.
    # Only a resolved historical ``scan`` is memoized: ``scan is None`` resolves
    # against the moving tip.
    memo: dict[Any, Any] | None = None
    memo_key: tuple[Any, ...] | None = None
    if scan is not None:
        memo = plc.__dict__.get("_pilot_chase_memo")
        if memo is None:
            memo = plc.__dict__["_pilot_chase_memo"] = {}
        memo_key = (tag, scan, steerable_eff)
        cached = memo.get(memo_key)
        if cached is not None:
            return cached

    chain = _cause(plc, tag, scan)
    if chain is None:
        result: tuple[set[str], list[tuple[str, Any]]] = (set(), [])
    else:
        result = _roots_from_chain(chain, plc, steerable_eff)
    if memo is not None:
        memo[memo_key] = result
    return result


def chase_chain_tags(
    plc: PLC,
    tag: str,
    *,
    scan: int | None = None,
    bridge: Any | None = None,
) -> set[str]:
    """Every meaningful tag on the deep cause chain of *tag*'s transition.

    Causal-primacy ranking needs chain *membership* (is this watchdog Done part
    of why the channel register moved?), which :func:`chase_cause_roots` cannot
    answer: an ejection caused by an **absence** — a sensor that never moved
    starving a complement-reset watchdog — has no steerable mover at all, so the
    roots come back empty while the chain itself
    (``WD_tmr_Done -> Rotate_Error -> S_StateCurrent``) is right there.

    The deep walk crosses the opaque-pipeline hop natively (the held
    ``StateRequested`` / enable-flag enabler is chased to the requester's guard
    chain), so the watchdog Done sits in the chain without any route inversion.

    Membership is the chain's **spine** — step transitions, their triggers, and
    the classified roots — NOT the why-held support names.  A deep chain's
    support closure contains half the program's steady state (every falsified
    arm of every held writer), and granting those tags chain-member standing
    hands causal primacy to bystanders: ``Test_Simulate_1st_Scan`` appears as a
    support of the held ``S_Pending1stScanSuspend`` in almost *any* burner
    chain, which let its liveness hypothesis outrank the starved watchdog's.
    Everything that legitimately confers membership is on the spine already —
    a fired watchdog Done is a transition step, a never-moved sensor is a
    classified root.  System tags (``sys.*`` / ``rtc.*``) and lookup-table
    reference constants are dropped — steady-state plumbing, not causal levers.

    *bridge* is accepted but ignored (see :func:`chase_cause_roots`).
    """
    chain = _cause(plc, tag, scan)
    if chain is None:
        return set()
    ref_consts = _reference_constants(plc)
    spine: set[str] = {chain.effect.tag_name}
    for step in chain.steps:
        spine.add(step.transition.tag_name)
        for trig in step.triggers:
            spine.add(trig.tag_name)
    for root in chain.roots:
        spine.add(root.tag_name)
    for tr in chain.conjunctive_roots:
        spine.add(tr.tag_name)
    tags = {t for t in spine if not t.startswith(("sys.", "rtc."))}
    return tags - ref_consts


def _cause(
    plc: PLC,
    tag: str,
    scan: int | None = None,
    cache: dict[tuple[str, int | None], Any] | None = None,
) -> CausalChain | None:
    """Memoized deep ``cause()`` for one chase.

    The same ``(tag, scan)`` reappears across overlapping chases, and each call
    can fork+replay a historical view, so a per-chase cache avoids re-resolving
    the same registers dozens of times (``cause()`` is pure for a fixed fork).
    ``deep=True`` (the default): the returned chain has already chased every
    held support and classified its terminals into ``chain.roots``.
    """
    if cache is not None and (tag, scan) in cache:
        return cache[(tag, scan)]
    try:
        if scan is not None:
            result = plc.cause(tag, scan=scan)
        else:
            result = plc.cause(tag)
    except Exception:  # noqa: BLE001
        logger.debug("pilot causal: cause(%s) raised", tag, exc_info=True)
        result = None
    if cache is not None:
        cache[(tag, scan)] = result
    return result


def _roots_from_chain(
    chain: CausalChain,
    plc: PLC,
    steerable: frozenset[str],
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Nogoods + holds from a single deep chain, stopping at the nearest lever.

    Two passes, mirroring the old recursive walk over the flattened chain:

    1. **Mover pass** — descend the trigger graph from the effect (``step
       transition -> step.triggers``), stopping at the first steerable tag on
       each path.  A steerable tag that *moved* becomes a nogood plus a hold at
       its pre-transition value.
    2. **Absence fallback** — only when the mover pass found no steerable lever:
       the cause is a held / never-moved support (a stuck sensor).  Descend the
       held / reset-blocked steps' enablers the same way, and read the chain's
       classified ``roots`` (external / never-written, minus reference
       constants) for the never-moved externals the trigger graph can't reach.
    """
    ref_consts = _reference_constants(plc)

    # Index the flattened chain: written tag -> its steps, and the
    # pre-transition value of every tag that moved (for the hold value).
    steps_by_tag: dict[str, list[Any]] = {}
    from_value: dict[str, Any] = {}
    for step in chain.steps:
        steps_by_tag.setdefault(step.transition.tag_name, []).append(step)
        for tr in step.triggers:
            if not _values_match(tr.from_value, tr.to_value):
                from_value.setdefault(tr.tag_name, tr.from_value)
    for tr in (*chain.conjunctive_roots, *chain.ambiguous_roots):
        if not _values_match(tr.from_value, tr.to_value):
            from_value.setdefault(tr.tag_name, tr.from_value)

    nogoods: set[str] = set()
    holds: list[tuple[str, Any]] = []
    seen_holds: set[tuple[str, Any]] = set()
    visited: set[str] = set()

    def add_hold(name: str, value: Any) -> None:
        if value is None:
            return
        hold = (name, value)
        if hold not in seen_holds:
            seen_holds.add(hold)
            holds.append(hold)

    def take_lever(name: str, hold_value: Any) -> None:
        nogoods.add(name)
        add_hold(name, hold_value)

    def descend(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for step in steps_by_tag.get(name, ()):
            for tr in step.triggers:
                if tr.tag_name in steerable:
                    moved = not _values_match(tr.from_value, tr.to_value)
                    take_lever(tr.tag_name, tr.from_value if moved else None)
                else:
                    descend(tr.tag_name)

    # Mover pass.
    if chain.effect.tag_name in steerable:
        take_lever(chain.effect.tag_name, from_value.get(chain.effect.tag_name))
    else:
        descend(chain.effect.tag_name)

    # Absence fallback — no steerable mover, so the cause is a held support.
    if not nogoods:
        for step in chain.steps:
            if step.triggers:
                continue  # a moved-trigger step is the mover pass's business
            for ec in step.enablers:
                name = ec.tag_name
                if name in ref_consts:
                    continue
                if name in steerable:
                    take_lever(name, getattr(ec, "value", None))
                else:
                    descend(name)
        for root in chain.roots:
            name = root.tag_name
            if (
                root.kind in ("external", "never_written")
                and name in steerable
                and name not in ref_consts
            ):
                take_lever(name, root.value)

    return nogoods, holds
