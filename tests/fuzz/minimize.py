"""Structural delta-debugging minimizer for ProgramSpec."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .strategies import CondSpec, ProgramSpec, RungSpec

log = logging.getLogger(__name__)


def minimize(
    spec: ProgramSpec,
    check: Callable[[ProgramSpec], bool],
    *,
    budget: float = 60.0,
) -> ProgramSpec:
    """Structurally reduce *spec* while *check* still returns True.

    Phases: rungs, subroutines, instructions, branches, conditions,
    composite-condition simplification.  Repeats to fixpoint or budget.
    """
    deadline = time.monotonic() + budget
    phases = [
        _try_remove_rungs,
        _try_remove_subroutines,
        _try_inline_subroutines,
        _try_unwrap_forloops,
        _try_remove_instructions,
        _try_remove_branches,
        _try_remove_conditions,
        _try_simplify_conditions,
    ]

    prev_size = _spec_size(spec) + 1
    while _spec_size(spec) < prev_size:
        if time.monotonic() > deadline:
            break
        prev_size = _spec_size(spec)
        for phase in phases:
            if time.monotonic() > deadline:
                break
            spec = phase(spec, check, deadline)
    log.info("minimize: final size = %d", _spec_size(spec))
    return spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_size(spec: ProgramSpec) -> int:
    total = len(spec.rungs) + len(spec.subroutines)
    for r in spec.rungs:
        total += _rung_size(r)
    for s in spec.subroutines:
        for r in s.rungs:
            total += _rung_size(r)
    return total


def _rung_size(r: RungSpec) -> int:
    total = len(r.conditions) + len(r.instructions) + len(r.branches)
    for b in r.branches:
        total += len(b.conditions) + len(b.instructions)
    return total


def _remove_at(lst: list, idx: int) -> list:
    return lst[:idx] + lst[idx + 1 :]


def _is_call_to(rung: RungSpec, name: str) -> bool:
    return any(i.kind == "call" and i.args.get("name") == name for i in rung.instructions)


def _has_any_call(rung: RungSpec) -> bool:
    return any(i.kind == "call" for i in rung.instructions)


def _expired(deadline: float) -> bool:
    return time.monotonic() > deadline


def _safe_check(check: Callable[[ProgramSpec], bool], candidate: ProgramSpec) -> bool:
    try:
        return check(candidate)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Phase 1: Remove entire rungs
# ---------------------------------------------------------------------------


def _try_remove_rungs(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    i = len(spec.rungs) - 1
    while i >= 0:
        if _expired(deadline):
            break
        if i >= len(spec.rungs):
            i = len(spec.rungs) - 1
            continue
        if _has_any_call(spec.rungs[i]):
            i -= 1
            continue
        candidate = replace(spec, rungs=_remove_at(spec.rungs, i))
        if _safe_check(check, candidate):
            spec = candidate
        i -= 1
    return spec


# ---------------------------------------------------------------------------
# Phase 2: Remove subroutines (with their call sites)
# ---------------------------------------------------------------------------


def _try_remove_subroutines(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    i = len(spec.subroutines) - 1
    while i >= 0:
        if _expired(deadline):
            break
        sub_name = spec.subroutines[i].name
        candidate = replace(
            spec,
            rungs=[r for r in spec.rungs if not _is_call_to(r, sub_name)],
            subroutines=_remove_at(spec.subroutines, i),
        )
        if _safe_check(check, candidate):
            spec = candidate
        i -= 1
    return spec


# ---------------------------------------------------------------------------
# Phase 3: Inline subroutines (replace call + definition with body at call site)
# ---------------------------------------------------------------------------


def _try_inline_subroutines(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    i = len(spec.subroutines) - 1
    while i >= 0:
        if _expired(deadline):
            break
        if i >= len(spec.subroutines):
            i = len(spec.subroutines) - 1
            continue
        sub = spec.subroutines[i]
        call_indices = [ri for ri, r in enumerate(spec.rungs) if _is_call_to(r, sub.name)]
        if len(call_indices) != 1:
            i -= 1
            continue
        ci = call_indices[0]
        new_rungs = spec.rungs[:ci] + list(sub.rungs) + spec.rungs[ci + 1 :]
        candidate = replace(
            spec,
            rungs=new_rungs,
            subroutines=_remove_at(spec.subroutines, i),
        )
        if _safe_check(check, candidate):
            spec = candidate
        i -= 1
    return spec


# ---------------------------------------------------------------------------
# Phase 4: Unwrap forloops (keep body, remove loop wrapper)
# ---------------------------------------------------------------------------


def _try_unwrap_forloops(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    spec = _try_unwrap_forloops_in_rungs(spec, check, deadline, sub_idx=None)
    for si in range(len(spec.subroutines)):
        if _expired(deadline):
            break
        spec = _try_unwrap_forloops_in_rungs(spec, check, deadline, sub_idx=si)
    return spec


def _try_unwrap_forloops_in_rungs(
    spec: ProgramSpec,
    check: Callable[[ProgramSpec], bool],
    deadline: float,
    *,
    sub_idx: int | None,
) -> ProgramSpec:
    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
    for ri in range(len(rungs)):
        if _expired(deadline):
            break
        rung = rungs[ri]
        if rung.forloop is None:
            continue
        new_rung = replace(rung, forloop=None)
        candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
        if _safe_check(check, candidate):
            spec = candidate
            rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
    return spec


# ---------------------------------------------------------------------------
# Phase 5: Remove instructions
# ---------------------------------------------------------------------------


def _try_remove_instructions(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    spec = _try_remove_instrs_from_rungs(spec, check, deadline, sub_idx=None)
    for si in range(len(spec.subroutines)):
        if _expired(deadline):
            break
        spec = _try_remove_instrs_from_rungs(spec, check, deadline, sub_idx=si)
    return spec


def _try_remove_instrs_from_rungs(
    spec: ProgramSpec,
    check: Callable[[ProgramSpec], bool],
    deadline: float,
    *,
    sub_idx: int | None,
) -> ProgramSpec:
    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
    ri = 0
    while ri < len(rungs):
        if _expired(deadline):
            break
        rung = rungs[ri]
        # main body instructions
        ii = len(rung.instructions) - 1
        while ii >= 0:
            if _expired(deadline):
                break
            if len(rung.instructions) <= 1:
                break
            new_rung = replace(rung, instructions=_remove_at(rung.instructions, ii))
            candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
            if _safe_check(check, candidate):
                spec = candidate
                rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
                rung = rungs[ri]
            ii -= 1
        # branch body instructions
        for bi in range(len(rung.branches)):
            if bi >= len(rung.branches):
                break
            branch = rung.branches[bi]
            bii = len(branch.instructions) - 1
            while bii >= 0:
                if _expired(deadline):
                    break
                if len(branch.instructions) <= 1:
                    break
                new_branch = replace(branch, instructions=_remove_at(branch.instructions, bii))
                new_branches = rung.branches[:bi] + [new_branch] + rung.branches[bi + 1 :]
                new_rung = replace(rung, branches=new_branches)
                candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
                if _safe_check(check, candidate):
                    spec = candidate
                    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
                    rung = rungs[ri]
                    branch = rung.branches[bi] if bi < len(rung.branches) else None
                    if branch is None:
                        break
                bii -= 1
        ri += 1
    return spec


def _replace_rung(
    spec: ProgramSpec, ri: int, new_rung: RungSpec, *, sub_idx: int | None
) -> ProgramSpec:
    if sub_idx is None:
        new_rungs = spec.rungs[:ri] + [new_rung] + spec.rungs[ri + 1 :]
        return replace(spec, rungs=new_rungs)
    sub = spec.subroutines[sub_idx]
    new_sub_rungs = sub.rungs[:ri] + [new_rung] + sub.rungs[ri + 1 :]
    new_sub = replace(sub, rungs=new_sub_rungs)
    new_subs = spec.subroutines[:sub_idx] + [new_sub] + spec.subroutines[sub_idx + 1 :]
    return replace(spec, subroutines=new_subs)


# ---------------------------------------------------------------------------
# Phase 4: Remove branches
# ---------------------------------------------------------------------------


def _try_remove_branches(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    spec = _try_remove_branches_from_rungs(spec, check, deadline, sub_idx=None)
    for si in range(len(spec.subroutines)):
        if _expired(deadline):
            break
        spec = _try_remove_branches_from_rungs(spec, check, deadline, sub_idx=si)
    return spec


def _try_remove_branches_from_rungs(
    spec: ProgramSpec,
    check: Callable[[ProgramSpec], bool],
    deadline: float,
    *,
    sub_idx: int | None,
) -> ProgramSpec:
    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
    for ri in range(len(rungs)):
        if _expired(deadline):
            break
        rung = rungs[ri]
        bi = len(rung.branches) - 1
        while bi >= 0:
            if _expired(deadline):
                break
            new_rung = replace(rung, branches=_remove_at(rung.branches, bi))
            candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
            if _safe_check(check, candidate):
                spec = candidate
                rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
                rung = rungs[ri]
            bi -= 1
    return spec


# ---------------------------------------------------------------------------
# Phase 5: Remove conditions
# ---------------------------------------------------------------------------


def _try_remove_conditions(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    spec = _try_remove_conds_from_rungs(spec, check, deadline, sub_idx=None)
    for si in range(len(spec.subroutines)):
        if _expired(deadline):
            break
        spec = _try_remove_conds_from_rungs(spec, check, deadline, sub_idx=si)
    return spec


def _try_remove_conds_from_rungs(
    spec: ProgramSpec,
    check: Callable[[ProgramSpec], bool],
    deadline: float,
    *,
    sub_idx: int | None,
) -> ProgramSpec:
    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
    for ri in range(len(rungs)):
        if _expired(deadline):
            break
        rung = rungs[ri]
        # rung conditions
        ci = len(rung.conditions) - 1
        while ci >= 0:
            if _expired(deadline):
                break
            new_rung = replace(rung, conditions=_remove_at(rung.conditions, ci))
            candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
            if _safe_check(check, candidate):
                spec = candidate
                rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
                rung = rungs[ri]
            ci -= 1
        # branch conditions
        for bi in range(len(rung.branches)):
            if bi >= len(rung.branches):
                break
            branch = rung.branches[bi]
            bci = len(branch.conditions) - 1
            while bci >= 0:
                if _expired(deadline):
                    break
                new_branch = replace(branch, conditions=_remove_at(branch.conditions, bci))
                new_branches = rung.branches[:bi] + [new_branch] + rung.branches[bi + 1 :]
                new_rung = replace(rung, branches=new_branches)
                candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
                if _safe_check(check, candidate):
                    spec = candidate
                    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
                    rung = rungs[ri]
                    branch = rung.branches[bi] if bi < len(rung.branches) else None
                    if branch is None:
                        break
                bci -= 1
    return spec


# ---------------------------------------------------------------------------
# Phase 6: Simplify composite conditions
# ---------------------------------------------------------------------------


def _try_simplify_conditions(
    spec: ProgramSpec, check: Callable[[ProgramSpec], bool], deadline: float
) -> ProgramSpec:
    spec = _try_simplify_conds_in_rungs(spec, check, deadline, sub_idx=None)
    for si in range(len(spec.subroutines)):
        if _expired(deadline):
            break
        spec = _try_simplify_conds_in_rungs(spec, check, deadline, sub_idx=si)
    return spec


def _try_simplify_conds_in_rungs(
    spec: ProgramSpec,
    check: Callable[[ProgramSpec], bool],
    deadline: float,
    *,
    sub_idx: int | None,
) -> ProgramSpec:
    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
    for ri in range(len(rungs)):
        if _expired(deadline):
            break
        rung = rungs[ri]
        # rung conditions
        for ci in range(len(rung.conditions)):
            if _expired(deadline):
                break
            for replacement in _composite_children(rung.conditions[ci]):
                new_conds = rung.conditions[:ci] + [replacement] + rung.conditions[ci + 1 :]
                new_rung = replace(rung, conditions=new_conds)
                candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
                if _safe_check(check, candidate):
                    spec = candidate
                    rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
                    rung = rungs[ri]
                    break
        # branch conditions
        for bi in range(len(rung.branches)):
            if bi >= len(rung.branches):
                break
            branch = rung.branches[bi]
            for bci in range(len(branch.conditions)):
                if _expired(deadline) or bci >= len(branch.conditions):
                    break
                for replacement in _composite_children(branch.conditions[bci]):
                    new_conds = (
                        branch.conditions[:bci] + [replacement] + branch.conditions[bci + 1 :]
                    )
                    new_branch = replace(branch, conditions=new_conds)
                    new_branches = rung.branches[:bi] + [new_branch] + rung.branches[bi + 1 :]
                    new_rung = replace(rung, branches=new_branches)
                    candidate = _replace_rung(spec, ri, new_rung, sub_idx=sub_idx)
                    if _safe_check(check, candidate):
                        spec = candidate
                        rungs = spec.rungs if sub_idx is None else spec.subroutines[sub_idx].rungs
                        rung = rungs[ri]
                        branch = rung.branches[bi]
                        break
    return spec


def _composite_children(cond: CondSpec) -> list[CondSpec]:
    if cond.kind in ("composite_and", "composite_or"):
        left, right = cond.operand
        return [left, right]
    return []
