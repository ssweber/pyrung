"""Pure candidate-admission and hold-conflict policy.

These predicates classify proposed actions and retained holds against current
orientation constraints. They do not read routes, create Bearings, or mutate
Pilot state.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import product
from typing import Any

from pyrung.core.analysis.pilot.navigation_contracts import _ActionPair
from pyrung.core.analysis.sp_values import _values_match


def _action_allowed(ctx: Any, pair: _ActionPair) -> bool:
    """Whether the current orientation constraints admit this exact action."""

    return pair not in ctx.blocked_actions


def _hold_values(hold_value: Any) -> tuple[Any, ...]:
    """Steady values a scalar or oscillating hold can pin its tag to."""

    rules = getattr(hold_value, "rules", None)
    if rules is not None:
        return tuple(rule.value for rule in rules)
    return (hold_value,)


def hold_defeats_needed(
    tag: str,
    hold_value: Any,
    needed: Sequence[tuple[str, Any]],
    pdg: Any,
    program: Any,
) -> bool:
    """Whether an option hold provably pins a checkpoint need.

    ``needed`` is ordered target-most first, so the first value for a tag is its
    requirement and deeper values are en-route stopovers. Direct contradictions
    and held guards that force a contradicting literal write are self-defeating.
    """

    return _holds_defeat_needed(((tag, hold_value),), needed, pdg, program)


def _holds_defeat_needed(
    holds: Sequence[tuple[str, Any]],
    needed: Sequence[tuple[str, Any]],
    pdg: Any,
    program: Any,
) -> bool:
    """Static proof that one hold assignment defeats a checkpoint need.

    A hold can defeat progress in either direction: it can enable a literal
    write of the wrong value, or force every writer of a required value
    non-conductive.  The latter matters for retained occurrence repairs: a
    master-enable cut may prevent the historical fault while also disabling
    the only sibling writer that earns later progress.
    """

    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.simplified import Atom, _conditions_list_to_expr, _expr_forced_true
    from pyrung.core.analysis.steerable import _literal_write

    needed_first: dict[str, Any] = {}
    for needed_tag, needed_value in needed:
        if isinstance(needed_value, Atom):
            continue
        needed_first.setdefault(needed_tag, needed_value)
    if not needed_first:
        return False

    held_values = {tag: _hold_values(value) for tag, value in holds}
    if not held_values:
        return False
    if any(
        tag in needed_first and any(not _values_match(value, needed_first[tag]) for value in values)
        for tag, values in held_values.items()
    ):
        return True

    for node in pdg.rung_nodes:
        read_tags = tuple(tag for tag in node.condition_reads if tag in held_values)
        if not read_tags:
            continue
        rung = resolve_rung(program, node)
        if rung is None:
            continue
        expr = _conditions_list_to_expr(getattr(rung, "_conditions", []))
        assignments = (
            dict(zip(read_tags, values, strict=True))
            for values in product(*(held_values[tag] for tag in read_tags))
        )
        if not any(_expr_forced_true(expr, assignment) is True for assignment in assignments):
            continue
        for needed_tag, needed_value in needed_first.items():
            written = _literal_write(rung, needed_tag)
            if written is not None and not _values_match(written, needed_value):
                return True

    # Negative-write proof. For each required value, collect every literal
    # writer capable of producing it. The hold defeats that need only when it
    # structurally forces *all* such writer guards false; an unreadable or
    # unaffected alternative keeps the result conservative.
    for needed_tag, needed_value in needed_first.items():
        matching_writers: list[Any] = []
        for node_index in pdg.writers_of.get(needed_tag, frozenset()):
            node = pdg.rung_nodes[node_index]
            rung = resolve_rung(program, node)
            if rung is None:
                matching_writers = []
                break
            written = _literal_write(rung, needed_tag)
            if written is not None and _values_match(written, needed_value):
                matching_writers.append((node, rung))
        if not matching_writers:
            continue

        all_blocked = True
        for node, rung in matching_writers:
            read_tags = tuple(tag for tag in node.condition_reads if tag in held_values)
            if not read_tags:
                all_blocked = False
                break
            expr = _conditions_list_to_expr(getattr(rung, "_conditions", []))
            assignments = (
                dict(zip(read_tags, values, strict=True))
                for values in product(*(held_values[tag] for tag in read_tags))
            )
            if not all(_expr_forced_true(expr, assignment) is False for assignment in assignments):
                all_blocked = False
                break
        if all_blocked:
            return True
    return False
