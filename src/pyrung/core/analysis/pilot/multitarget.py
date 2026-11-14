"""Read compatibility and input preferences for a simultaneous goal.

The static pre-pass proves only direct same-tag conflicts and the mutual
exclusion in :mod:`static_proofs`. Input supports rank current-world
alternatives only; this module never orders a sequence of target drives.
Anything not proved is left for joint concrete execution and verification.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pilot.static_proofs import ProofFacts, exclusion_evidence
from pyrung.core.analysis.sp_values import _values_match

# (tag, value, predicate)
TargetSpec = tuple[str, Any, Any]


def input_preferences(
    targets: Any, program: Any, steerable: frozenset[str]
) -> tuple[tuple[str, Any], ...]:
    """Rank joint routes by common input supports, without excluding detours.

    Simplified terminal forms supply static proposals only. Retentive and
    temporal program behavior still passes through ordinary trace/execution;
    a conflicting input remains available when no preferred route survives.
    """
    from pyrung.core.analysis.simplified import And, Atom, Or, _negate, simplified_forms
    from pyrung.core.analysis.sp_values import _required_from_atom

    forms = simplified_forms(program)

    def required(expr: Any) -> set[tuple[str, Any]]:
        if isinstance(expr, Atom):
            pairs = _required_from_atom(expr) or ()
            return {(tag, value) for tag, value in pairs if tag in steerable}
        if isinstance(expr, And):
            return set().union(*(required(term) for term in expr.terms))
        if isinstance(expr, Or) and expr.terms:
            branches = [required(term) for term in expr.terms]
            return branches[0].intersection(*branches[1:])
        return set()

    preferences: set[tuple[str, Any]] = set()
    for target in targets:
        if target.predicate is not None:
            preferences.update(required(target.predicate))
        elif target.tag in steerable:
            preferences.add((target.tag, target.value))
        elif type(target.value) is bool and target.tag in forms:
            expr = forms[target.tag].expr
            preferences.update(required(expr if target.value else _negate(expr)))
    # Contradictory supports are unresolved alternatives, never exclusions.
    return tuple(
        sorted(
            (
                (tag, value)
                for tag, value in preferences
                if all(
                    other_tag != tag or _values_match(value, other)
                    for other_tag, other in preferences
                )
            ),
            key=repr,
        )
    )


def analyze(facts: ProofFacts, targets: list[TargetSpec]) -> tuple[bool, str | None]:
    """Prove direct incompatibility, otherwise leave joint navigation open.

    Relational operands are live predicates, never equality values. Static
    exclusion evidence applies only to concrete equality targets.
    """
    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            ti, tj = targets[i], targets[j]
            if (
                ti[2] is None
                and tj[2] is None
                and ti[0] == tj[0]
                and not _values_match(ti[1], tj[1])
            ):
                return (
                    False,
                    f"pilot: {ti[0]} is one register; cannot be both "
                    f"{ti[1]!r} and {tj[1]!r} in the same scan.",
                )

    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            a, b = targets[i], targets[j]
            if a[0] == b[0] or a[2] is not None or b[2] is not None:
                continue
            evidence = exclusion_evidence(facts, a[:2], b[:2])
            if evidence is not None:
                return False, (
                    f"pilot: {a[0]}={a[1]!r} and {b[0]}={b[1]!r} are mutually exclusive; "
                    f"establishing {a[0]} leaves {b[0]} off ({', '.join(evidence[0])}); "
                    f"establishing {b[0]} leaves {a[0]} off ({', '.join(evidence[1])})."
                )

    return True, None
