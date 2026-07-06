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


def chase_cause_roots(
    plc: PLC,
    tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None = None,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Chase ``cause()`` chain to steerable-input roots.

    Returns ``(nogoods, holds)`` where:
    - *nogoods*: steerable inputs whose transition caused the regression
    - *holds*: ``(tag, value)`` pairs for inputs that must stay at their
      pre-transition value to prevent the regression
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
    memo: dict[Any, Any] | None = None
    memo_key: tuple[Any, ...] | None = None
    if scan is not None:
        memo = plc.__dict__.get("_pilot_chase_memo")
        if memo is None:
            memo = plc.__dict__["_pilot_chase_memo"] = {}
        memo_key = (tag, scan, steerable)
        cached = memo.get(memo_key)
        if cached is not None:
            return cached

    cache: dict[tuple[str, int | None], Any] = {}
    chain = _cause(plc, tag, scan, cache)
    if chain is None:
        result: tuple[set[str], list[tuple[str, Any]]] = (set(), [])
    else:
        result = _walk_cause_chain(chain, plc, steerable, set(), 0, cache)
    if memo is not None:
        memo[memo_key] = result
    return result


def chase_chain_tags(
    plc: PLC,
    tag: str,
    *,
    scan: int | None = None,
) -> set[str]:
    """Every tag name on the cause chain of *tag*'s transition — effects,
    roots, triggers, and enabler names, steerable or not.

    Causal-primacy ranking needs chain *membership* (is this watchdog Done
    part of why the governing register moved?), which :func:`chase_cause_roots`
    cannot answer: an ejection caused by an **absence** — a sensor that never
    moved starving a complement-reset watchdog — has no steerable mover at
    all, so the roots come back empty while the chain itself
    (``WD_tmr_Done -> Rotate_Error -> S_StateCurrent``) is right there.
    """
    cache: dict[tuple[str, int | None], Any] = {}
    chain = _cause(plc, tag, scan, cache)
    tags: set[str] = set()
    if chain is not None:
        _collect_chain_tags(chain, plc, tags, set(), 0, cache)
    return tags


def _collect_chain_tags(
    chain: Any,
    plc: PLC,
    out: set[str],
    seen: set[tuple[str, int | None]],
    depth: int,
    cache: dict[tuple[str, int | None], Any],
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
            _collect_chain_tags(sub, plc, out, seen, depth + 1, cache)

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

    def process_root(root: Any) -> None:
        if root.tag_name in steerable:
            nogoods.add(root.tag_name)
            if root.from_value is not None and not _values_match(root.from_value, root.to_value):
                hold = (root.tag_name, root.from_value)
                if hold not in seen_holds:
                    seen_holds.add(hold)
                    holds.append(hold)
            return
        sub = _cause(plc, root.tag_name, root.scan_id, cache)
        if sub is None:
            return
        sub_ng, sub_holds = _walk_cause_chain(sub, plc, steerable, seen, depth + 1, cache)
        nogoods.update(sub_ng)
        for h in sub_holds:
            if h not in seen_holds:
                seen_holds.add(h)
                holds.append(h)

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
                        hold = (enabler.tag_name, held_val)
                        if hold not in seen_holds:
                            seen_holds.add(hold)
                            holds.append(hold)
                    continue
                sub = _cause(
                    plc, enabler.tag_name, getattr(enabler, "held_since_scan", None), cache
                )
                if sub is None:
                    continue
                sub_ng, sub_holds = _walk_cause_chain(sub, plc, steerable, seen, depth + 1, cache)
                nogoods.update(sub_ng)
                for h in sub_holds:
                    if h not in seen_holds:
                        seen_holds.add(h)
                        holds.append(h)

    return nogoods, holds
