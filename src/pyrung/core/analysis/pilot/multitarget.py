"""Conservatively classify and order multiple requested targets.

The static pre-pass proves only direct same-tag conflicts and mutual retentive
clobber across every establish route. A one-directional clobber determines
clobberer-first order. Anything not proved is left for concrete driving and the
final all-target check; this module performs no simulation or probing.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.trace import _env_for, _scope_ref, trace_back
from pyrung.core.analysis.pilot.trace_tree import _visit_key
from pyrung.core.analysis.pilot.writer_selection import _can_produce
from pyrung.core.analysis.sp_values import _values_match, _written_value_for_tag

# (tag, value, predicate)
TargetSpec = tuple[str, Any, Any]


def _all_writer_rungs(node: Any, out: set[int]) -> None:
    if node.writer_rung is not None:
        out.add(node.writer_rung)
    for ch in node.children:
        _all_writer_rungs(ch, out)


def _producers(env: Any, tag: str, val: Any) -> set[int]:
    """Rungs that can produce ``tag == val`` (cold/initial state excluded)."""
    out: set[int] = set()
    for ri in env.pdg.writers_of.get(tag, frozenset()):
        ro = resolve_rung(env.program, env.pdg.rung_nodes[ri])
        if ro is not None and _can_produce(_written_value_for_tag(ro, tag), val):
            out.add(ri)
    return out


def _writes_off(env: Any, ri: int, tag: str, val: Any) -> Any:
    """Written value if rung ``ri`` drives ``tag`` off ``val`` retentively, else None."""
    if ri not in env.pdg.writers_of.get(tag, frozenset()):
        return None
    ro = resolve_rung(env.program, env.pdg.rung_nodes[ri])
    if ro is None:
        return None
    wv = _written_value_for_tag(ro, tag)
    if _can_produce(wv, val):
        return None  # this writer could itself produce the target — not a clobber
    if tag in env.pdg.rung_nodes[ri].ote_writes:
        return None  # OTE / self-clearing — transient, not a retentive clobber
    return wv


def _route_clobbers(env: Any, x: tuple[str, Any], y: tuple[str, Any]) -> list[tuple[int, list]]:
    """Per-route clobber analysis of 'establishing X drives held Y off-value'.

    One entry per establish route of X (forced through each producer via
    ``writer_locks``): ``(producer_ri, [(clobber_ri, written_value), …])``.
    """
    xt, xv = x
    yt, yv = y
    routes: list[tuple[int, list]] = []
    for ri in sorted(_producers(env, xt, xv)):
        tree = trace_back(
            xt,
            xv,
            env.snapshot,
            env.pdg,
            env.program,
            env.steerable,
            writer_locks={_visit_key(xt, xv): ri},
        )
        rungs: set[int] = {ri}
        _all_writer_rungs(tree, rungs)
        hits = []
        for rj in sorted(rungs):
            wv = _writes_off(env, rj, yt, yv)
            if wv is not None:
                hits.append((rj, wv))
        routes.append((ri, hits))
    return routes


def _clobbers_universally(env: Any, x: tuple[str, Any], y: tuple[str, Any]) -> tuple[bool, list]:
    """True iff EVERY establish route of X clobbers Y (∀ over routes).

    No rung producer (cold-only) → not a universal establish-clobber (fail-open):
    the drive loop attempts it rather than a false ME.
    """
    routes = _route_clobbers(env, x, y)
    if not routes:
        return False, []
    universal = all(hits for _, hits in routes)
    evidence = [hit for _, hits in routes for hit in hits]
    return universal, evidence


def _unwrap(wv: Any) -> Any:
    return getattr(wv, "value", wv)


def _me_reason(env: Any, a: TargetSpec, b: TargetSpec, ev_ab: list, ev_ba: list) -> str:
    at, av = a[0], a[1]
    bt, bv = b[0], b[1]

    def _ev(ev: list, ytag: str) -> str:
        return (
            ", ".join(
                f"{_scope_ref(ri, env.pdg.rung_nodes[ri])} sets {ytag}={_unwrap(wv)!r}"
                for ri, wv in ev
            )
            or "?"
        )

    return (
        f"pilot: {at}={av!r} and {bt}={bv!r} are mutually exclusive; "
        f"establishing {at} drives {bt} off ({_ev(ev_ab, bt)}); "
        f"establishing {bt} drives {at} off ({_ev(ev_ba, at)})."
    )


def _order(env: Any, targets: list[TargetSpec]) -> list[TargetSpec]:
    """Clobberer-first: if establishing i drives j off-value, i precedes j."""
    n = len(targets)
    before = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            clob, _ = _clobbers_universally(env, targets[i][:2], targets[j][:2])
            if clob:
                before[i][j] = True
    remaining = list(range(n))
    order: list[int] = []
    while remaining:
        pick = next(
            (idx for idx in remaining if not any(before[k][idx] for k in remaining if k != idx)),
            remaining[0],  # cycle (shouldn't survive the ME check) — break arbitrarily
        )
        order.append(pick)
        remaining.remove(pick)
    return [targets[i] for i in order]


def analyze(
    snapshot: dict[str, Any],
    pdg: Any,
    program: Any,
    steerable: frozenset[str],
    targets: list[TargetSpec],
) -> tuple[bool, str | None, list[TargetSpec]]:
    """Return ``(ok, reason, ordered)``.

    ``ok=False`` with a ``reason`` when a sound prune fires (same-tag / mutual
    retentive clobber).  Otherwise ``ok=True`` and ``ordered`` is a
    clobberer-first establish order for the drive loop.
    """
    env = _env_for(snapshot, pdg, program, steerable)

    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            ti, tj = targets[i], targets[j]
            if ti[0] == tj[0] and not _values_match(ti[1], tj[1]):
                return (
                    False,
                    f"pilot: {ti[0]} is one register; cannot be both "
                    f"{ti[1]!r} and {tj[1]!r} in the same scan.",
                    list(targets),
                )

    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            a, b = targets[i], targets[j]
            ab, ev_ab = _clobbers_universally(env, a[:2], b[:2])
            if not ab:
                continue
            ba, ev_ba = _clobbers_universally(env, b[:2], a[:2])
            if ba:
                return False, _me_reason(env, a, b, ev_ab, ev_ba), list(targets)

    return True, None, _order(env, targets)
