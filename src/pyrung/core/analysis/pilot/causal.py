"""Cause-chain walker — recursive root finding for PILOT.

Shared by the gate pipeline (excursion diagnosis), the outcome classifier
(causal attribution), and investigation (hypothesis generation).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_MAX_CAUSE_DEPTH = 32

# The compass bridge (see pilot/CLAUDE.md, causal.py bullet) crosses an
# opaque-pipeline destination transition by route inversion.  A request-set ->
# transfer hop is normally 1-2 scans; this window bounds the ``history.range``
# replay cost while comfortably covering the pipeline latency.
_BRIDGE_LOOKBACK = 256
# Routes can fan out (one destination value reachable from several requesters);
# cap the resume points generously so a pathological program can't explode the
# walk, while never truncating a normal PackML fan-out.
_BRIDGE_MAX_RESUME = 32


# ---------------------------------------------------------------------------
# The compass bridge — route-inversion crossing of an opaque-pipeline hop
#
# The recorded-history walk dead-ends at a PackML jump table: the destination
# register (``S_StateCurrent``) is written by an indirect copy gated by a
# freshly-computed constant-table enable flag, while the requester
# (``S_StateRequested``) is a *held* enabler at the transfer scan — added by
# name but never recursed (``_collect_chain_tags``), so the chain stops short of
# the watchdog that requested the state.  But the hop IS inverted statically:
# ``evidence.expand_routes`` produces ``TransitionRoute``s (destination value ->
# request tag/value -> requester writers/guards) that live on
# ``ctx.compass.graphs``.  The bridge consults those routes for the observed
# destination transition, confirms against recorded history which route fired,
# and resumes the history walk from that route's guard tags.
# ---------------------------------------------------------------------------


class _Bridge:
    """Precomputed route index over a bridge object's compass graphs.

    *bridge* is duck-typed: any object exposing ``.compass.graphs`` where each
    graph carries ``.routes`` (:class:`evidence.TransitionRoute`).  Tests pass a
    ``SimpleNamespace``; investigation passes the real ``_PilotContext``.  Built
    once per chase (the graphs are static for a drive), so the per-node hop check
    is a cheap set membership.
    """

    __slots__ = ("dest_tags", "routes")

    def __init__(self, bridge: Any) -> None:
        graphs = getattr(getattr(bridge, "compass", None), "graphs", ()) or ()
        self.routes = tuple(r for g in graphs for r in getattr(g, "routes", ()))
        self.dest_tags: frozenset[str] = frozenset(r.destination_tag for r in self.routes)


def _history_value_scan(
    plc: PLC,
    tag: str,
    value: Any,
    end_scan: int,
) -> int | None:
    """Most recent scan ``s <= end_scan`` (within the lookback window) whose
    recorded end-of-scan value of *tag* matches *value*, or ``None``."""
    try:
        states = plc.history.range(end_scan - _BRIDGE_LOOKBACK, end_scan + 1)
    except Exception:  # noqa: BLE001
        return None
    last: int | None = None
    for state in states:
        if _values_match(state.tags.get(tag), value):
            last = state.scan_id
    return last


def _bridge_last_transition_scan(plc: PLC, tag: str, end_scan: int) -> int | None:
    """Latest scan ``<= end_scan`` (within the lookback window) where *tag*
    changed value, or ``None`` if it never transitioned there.

    Mirrors ``investigate._last_transition_scan``; ``None`` is harmless — the
    resume ``_cause(plc, tag, None)`` falls back to the most-recent transition.
    """
    try:
        states = plc.history.range(end_scan - _BRIDGE_LOOKBACK, end_scan + 1)
    except Exception:  # noqa: BLE001
        return None
    last: int | None = None
    for prev, cur in zip(states, states[1:], strict=False):
        if not _values_match(prev.tags.get(tag), cur.tags.get(tag)):
            last = cur.scan_id
    return last


def _bridge_pipeline_hop(
    plc: PLC,
    effect: Any,
    bridge: _Bridge,
) -> list[tuple[str, Any, int | None]]:
    """Route-inversion crossing of an opaque-pipeline destination transition.

    Given a recorded ``effect`` transition (``tag_name``, ``scan_id``,
    ``from_value``, ``to_value``) on a pipeline destination, return resume points
    ``[(tag, value, scan)]`` — the fired requester's guard tags at their
    transition scans.  Empty list = punt (no matching route, or no route was
    confirmed against recorded history — **never fabricate an unconfirmed hop**).
    """
    resumes: list[tuple[str, Any, int | None]] = []
    seen: set[tuple[str, Any]] = set()

    def add(tag: str, value: Any, scan: int | None) -> None:
        key = (tag, value)
        if key not in seen:
            seen.add(key)
            resumes.append((tag, value, scan))

    for route in bridge.routes:
        if route.destination_tag != effect.tag_name:
            continue
        if not _values_match(route.destination_value, effect.to_value):
            continue
        req_tag = route.request_tag
        if req_tag is None:
            # Direct writer — the recorded walk already handles it; the bridge
            # exists only for the intermediate request hop.
            continue
        # History confirmation: the route fired only if its request value was
        # actually recorded at or before the destination transition.
        req_scan = _history_value_scan(plc, req_tag, route.request_value, effect.scan_id)
        if req_scan is None:
            continue  # this route did not fire — skip it
        add(req_tag, route.request_value, req_scan)
        for tag, value in (*route.enablers, *route.source_constraints, *route.call_site_gates):
            add(tag, value, _bridge_last_transition_scan(plc, tag, req_scan))
        if len(resumes) >= _BRIDGE_MAX_RESUME:
            break

    return resumes[:_BRIDGE_MAX_RESUME]


def chase_cause_roots(
    plc: PLC,
    tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None = None,
    bridge: Any | None = None,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Chase ``cause()`` chain to steerable-input roots.

    Returns ``(nogoods, holds)`` where:
    - *nogoods*: steerable inputs whose transition caused the regression
    - *holds*: ``(tag, value)`` pairs for inputs that must stay at their
      pre-transition value to prevent the regression

    *bridge* (opt-in, duck-typed ``Any`` exposing ``.compass.graphs`` whose
    graphs carry ``.routes``) crosses an opaque-pipeline destination hop by route
    inversion: at a pipeline destination the recorded walk dead-ends on, the
    fired requester's guard tags (confirmed against recorded history) are
    resumed as extra roots.  ``None`` = the exact prior behavior.
    """
    # Cross-chase result memo, stored on the fork.  chase_cause_roots is pure for
    # a fixed fork — ``cause()`` is pure for a fixed fork (see ``_cause``) and a
    # fork's recorded history at a *past* scan is immutable — so
    # ``(tag, scan, steerable) -> (nogoods, holds)`` is stable for the fork's
    # lifetime.  The verify loops re-chase the same ``(fork, tag, scan)`` dozens
    # of times (one ``_action_caused_change`` per changed node, on every
    # observation of the same fork), so without this ~95% of ``cause()`` calls
    # re-resolve a key already computed on this very fork.  The memo lives on the
    # fork, so it is invalidated by construction: ``fork()`` / ``revert_to()``
    # hand back a fresh fork with an empty memo.  Callers treat the result as
    # read-only.
    #
    # Only a resolved historical ``scan`` is memoized: ``scan is None`` resolves
    # against the *current tip*, which moves as the fork advances, so its result
    # is not stable across re-chases.  The measured redundancy is entirely on
    # explicit scans, so this loses nothing.
    bridge_idx = _Bridge(bridge) if bridge is not None else None
    memo: dict[Any, Any] | None = None
    memo_key: tuple[Any, ...] | None = None
    if scan is not None:
        memo = plc.__dict__.get("_pilot_chase_memo")
        if memo is None:
            memo = plc.__dict__["_pilot_chase_memo"] = {}
        # ``bridge is not None`` completes the key: a bridged chase is a superset
        # of the plain one, and the bridge is constant per drive, so its presence
        # (not identity) discriminates the two cached results.
        memo_key = (tag, scan, steerable, bridge is not None)
        cached = memo.get(memo_key)
        if cached is not None:
            return cached

    cache: dict[tuple[str, int | None], Any] = {}
    chain = _cause(plc, tag, scan, cache)
    if chain is None:
        result: tuple[set[str], list[tuple[str, Any]]] = (set(), [])
    else:
        result = _walk_cause_chain(chain, plc, steerable, set(), 0, cache, bridge_idx)
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
    """Every tag name on the cause chain of *tag*'s transition — effects,
    roots, triggers, and enabler names, steerable or not.

    Causal-primacy ranking needs chain *membership* (is this watchdog Done
    part of why the governing register moved?), which :func:`chase_cause_roots`
    cannot answer: an ejection caused by an **absence** — a sensor that never
    moved starving a complement-reset watchdog — has no steerable mover at
    all, so the roots come back empty while the chain itself
    (``WD_tmr_Done -> Rotate_Error -> S_StateCurrent``) is right there.

    *bridge* (opt-in, duck-typed ``Any`` exposing ``.compass.graphs`` whose
    graphs carry ``.routes``) crosses an opaque-pipeline destination hop: at a
    pipeline destination the recorded walk dead-ends on, the fired requester's
    guard tags (confirmed against recorded history) are folded in and recursed.
    ``None`` = the exact prior behavior.
    """
    bridge_idx = _Bridge(bridge) if bridge is not None else None
    cache: dict[tuple[str, int | None], Any] = {}
    chain = _cause(plc, tag, scan, cache)
    tags: set[str] = set()
    if chain is not None:
        _collect_chain_tags(chain, plc, tags, set(), 0, cache, bridge_idx)
    return tags


def _collect_chain_tags(
    chain: Any,
    plc: PLC,
    out: set[str],
    seen: set[tuple[str, int | None]],
    depth: int,
    cache: dict[tuple[str, int | None], Any],
    bridge: _Bridge | None = None,
) -> None:
    if depth > _MAX_CAUSE_DEPTH:
        return
    key = (chain.effect.tag_name, chain.effect.scan_id)
    if key in seen:
        return
    seen.add(key)
    out.add(chain.effect.tag_name)

    def visit(node: Any) -> None:
        out.add(node.tag_name)
        sub = _cause(plc, node.tag_name, getattr(node, "scan_id", None), cache)
        if sub is not None:
            _collect_chain_tags(sub, plc, out, seen, depth + 1, cache, bridge)

    for root in chain.conjunctive_roots:
        visit(root)
    for root in chain.ambiguous_roots:
        visit(root)
    for step in chain.steps:
        for trigger in step.triggers:
            visit(trigger)
        # Enabler *names* only — a held condition's identity matters for chain
        # membership; recursing into every enabler would pull in half the
        # program's steady state.
        for enabler in step.enablers:
            out.add(enabler.tag_name)

    # Compass bridge: cross an opaque-pipeline destination hop by route
    # inversion.  The dead-end is exactly a held pipeline enabler
    # (``StateRequested``) that ``_collect_chain_tags`` adds by name but never
    # recurses; the bridge recovers the fired requester's guard chain.
    if bridge is not None and chain.effect.tag_name in bridge.dest_tags:
        for res_tag, _value, res_scan in _bridge_pipeline_hop(plc, chain.effect, bridge):
            out.add(res_tag)
            sub = _cause(plc, res_tag, res_scan, cache)
            if sub is not None:
                _collect_chain_tags(sub, plc, out, seen, depth + 1, cache, bridge)


def _cause(
    plc: PLC,
    tag: str,
    scan: int | None = None,
    cache: dict[tuple[str, int | None], Any] | None = None,
) -> Any | None:
    """Memoized ``cause()`` for one chase: the same ``(tag, scan)`` reappears as
    a root across many overlapping cause chains, and each call can fork+replay a
    historical view — so without the cache one compass observation re-resolves
    the same registers dozens of times (``cause()`` is pure for a fixed fork)."""
    if cache is not None and (tag, scan) in cache:
        return cache[(tag, scan)]
    try:
        result = plc.cause(tag, scan=scan) if scan is not None else plc.cause(tag)
    except Exception:  # noqa: BLE001
        logger.debug("pilot causal: cause(%s) raised", tag, exc_info=True)
        result = None
    if cache is not None:
        cache[(tag, scan)] = result
    return result


def _walk_cause_chain(
    chain: Any,
    plc: PLC,
    steerable: frozenset[str],
    seen: set[tuple[str, int | None]],
    depth: int,
    cache: dict[tuple[str, int | None], Any] | None = None,
    bridge: _Bridge | None = None,
) -> tuple[set[str], list[tuple[str, Any]]]:
    if depth > _MAX_CAUSE_DEPTH:
        return set(), []

    key = (chain.effect.tag_name, chain.effect.scan_id)
    if key in seen:
        return set(), []
    seen.add(key)

    nogoods: set[str] = set()
    holds: list[tuple[str, Any]] = []
    seen_holds: set[tuple[str, Any]] = set()

    def add_hold(tag: str, value: Any) -> None:
        hold = (tag, value)
        if hold not in seen_holds:
            seen_holds.add(hold)
            holds.append(hold)

    def recurse(sub: Any) -> None:
        sub_ng, sub_holds = _walk_cause_chain(sub, plc, steerable, seen, depth + 1, cache, bridge)
        nogoods.update(sub_ng)
        for h in sub_holds:
            add_hold(*h)

    def process_root(root: Any) -> None:
        if root.tag_name in steerable:
            nogoods.add(root.tag_name)
            if root.from_value is not None and not _values_match(root.from_value, root.to_value):
                add_hold(root.tag_name, root.from_value)
            return
        sub = _cause(plc, root.tag_name, root.scan_id, cache)
        if sub is None:
            return
        recurse(sub)

    for root in chain.conjunctive_roots:
        process_root(root)
    for root in chain.ambiguous_roots:
        process_root(root)
    for step in chain.steps:
        for trigger in step.triggers:
            process_root(trigger)

    has_steerable = any(n in steerable for n in nogoods)
    if not has_steerable:
        for step in chain.steps:
            if step.triggers:
                continue
            for enabler in step.enablers:
                if enabler.tag_name in steerable:
                    nogoods.add(enabler.tag_name)
                    held_val = getattr(enabler, "value", None)
                    if held_val is not None:
                        add_hold(enabler.tag_name, held_val)
                    continue
                sub = _cause(
                    plc, enabler.tag_name, getattr(enabler, "held_since_scan", None), cache
                )
                if sub is None:
                    continue
                recurse(sub)

    # Compass bridge: cross an opaque-pipeline destination hop by route
    # inversion, augmenting (never replacing) the recorded walk.  A steerable
    # resume that *actually moved* becomes a nogood + pre-transition hold
    # (mirroring ``process_root``); a never-moved steerable resume — e.g. an
    # operator command a confirmed-by-request route names but that did not fire —
    # is NOT implicated (punt, never fabricate a cause).  Non-steerable resumes
    # recurse to reach the true root (the starved watchdog).
    if bridge is not None and chain.effect.tag_name in bridge.dest_tags:
        for res_tag, _value, res_scan in _bridge_pipeline_hop(plc, chain.effect, bridge):
            sub = _cause(plc, res_tag, res_scan, cache)
            if res_tag in steerable:
                if (
                    sub is not None
                    and sub.effect.from_value is not None
                    and not _values_match(sub.effect.from_value, sub.effect.to_value)
                ):
                    nogoods.add(res_tag)
                    add_hold(res_tag, sub.effect.from_value)
                continue
            if sub is not None:
                recurse(sub)

    return nogoods, holds
