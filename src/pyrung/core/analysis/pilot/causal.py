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
    chain = _cause(plc, tag, scan)
    if chain is None:
        return set(), []
    return _walk_cause_chain(chain, plc, steerable, set(), 0)


def _cause(plc: PLC, tag: str, scan: int | None = None) -> Any | None:
    try:
        return plc.cause(tag, scan=scan) if scan is not None else plc.cause(tag)
    except Exception:  # noqa: BLE001
        logger.debug("pilot causal: cause(%s) raised", tag, exc_info=True)
        return None


def _walk_cause_chain(
    chain: Any,
    plc: PLC,
    steerable: frozenset[str],
    seen: set[tuple[str, int | None]],
    depth: int,
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
        sub = _cause(plc, root.tag_name, root.scan_id)
        if sub is None:
            return
        sub_ng, sub_holds = _walk_cause_chain(sub, plc, steerable, seen, depth + 1)
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
                sub = _cause(plc, enabler.tag_name, getattr(enabler, "held_since_scan", None))
                if sub is None:
                    continue
                sub_ng, sub_holds = _walk_cause_chain(sub, plc, steerable, seen, depth + 1)
                nogoods.update(sub_ng)
                for h in sub_holds:
                    if h not in seen_holds:
                        seen_holds.add(h)
                        holds.append(h)

    return nogoods, holds
