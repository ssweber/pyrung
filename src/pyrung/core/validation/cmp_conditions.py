"""Comparison-semantics validators: CMP_EQ_ON_MONOTONE / CMP_TRUE_AT_RESET /
CMP_STATIC_ON_LEFT.

Three rules, one pass over every comparison in every rung condition (main +
subroutines + branches, both the ``Compare*`` leaf family and the expression-tree
``ExprCompare``):

* ``CMP_EQ_ON_MONOTONE`` — ``==`` / ``!=`` against a self-advancing register
  (``Timer.Acc``, a counter accumulator).  The register steps by ``rate_per_scan``
  each scan and can jump *over* the compared value between scans, so the equality
  may never latch.  **error**.  The ``== 0`` / ``!= 0`` floor check is edge-safe and
  exempt.

* ``CMP_TRUE_AT_RESET`` — an ordered comparison that is TRUE at the accumulator's
  reset value (``Acc = 0``) and FALSE at the crossing: the exact complement of a
  completion check, firing a spurious pulse on every state entry where ``Acc``
  resets.  Gated to up-from-zero accumulators with the comparand matching the
  configured preset — zero false positives.  **warning**.

* ``CMP_STATIC_ON_LEFT`` — the operand-order convention: the moving value on the
  left, the threshold on the right.  Severity tracks how sure we are, not one fixed
  level: ``==`` / ``!=`` is cosmetic → **info**; an ordered comparison whose dynamic
  side is a self-advancing register is a **KNOWN** order issue (the accumulator is
  provably the mover) → **warning**; an ordered comparison between two ordinary tags
  is a **MAYBE** (a live measurement and a threshold are indistinguishable to the
  analyzer) → **advisory**, kept out of the ``errors()`` / ``warnings()`` gate.  A
  monotone register on the right that is true at reset escalates through
  ``CMP_TRUE_AT_RESET`` instead.

"Dynamic" (belongs on the left) is any program-written tag, self-advancing register,
or inline computed expression; "static" is a literal, an ``S.`` constant, or any
never-written tag (an external sensor and an HMI setpoint both land here — the rule
does not classify measurement vs threshold, it grades the finding by confidence).
Calc-derived provenance is recovered via the prover's ``_extract_forward_affine`` —
the same affine extractor the functional-dependency pass uses — and sharpens the
message without changing the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
)
from pyrung.core.expression import (
    BinaryExpr,
    ExprCompare,
    Expression,
    LiteralExpr,
    TagExpr,
    UnaryExpr,
)
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation._common import (
    _resolve_tag_names,
    iter_rungs,
    walk_instructions,
)
from pyrung.core.validation.display import FindingDisplay, Frame
from pyrung.core.validation.render import (
    caret_of,
    operand_name,
    with_rung_line,
)
from pyrung.core.validation.render import (
    render_expr as render_source_expr,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyrung.core.condition import Condition
    from pyrung.core.instruction.advance import AdvanceProfile
    from pyrung.core.program import Program
    from pyrung.core.validation.severity import Severity

CMP_EQ_ON_MONOTONE = "CMP_EQ_ON_MONOTONE"
CMP_TRUE_AT_RESET = "CMP_TRUE_AT_RESET"
CMP_STATIC_ON_LEFT = "CMP_STATIC_ON_LEFT"

_SYMBOL_BY_CLASS: dict[type, str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
}

# Operator with the two operands swapped (``a < b`` ⟺ ``b > a``).
_FLIP: dict[str, str] = {
    "==": "==",
    "!=": "!=",
    "<": ">",
    ">": "<",
    "<=": ">=",
    ">=": "<=",
}

# ---------------------------------------------------------------------------
# Normalised comparison model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Operand:
    """One side of a comparison, classified for both compare families.

    ``kind`` ∈ ``"tag"`` | ``"literal"`` | ``"computed"`` | ``"opaque"``.  ``name``
    is the tag name (``kind == "tag"``), ``value`` the literal (``kind ==
    "literal"``), ``raw`` the source object for rendering computed expressions.
    """

    kind: str
    name: str | None
    value: Any
    raw: Any


@dataclass(frozen=True)
class _Compare:
    """A single comparison lifted out of a rung condition."""

    loc: str
    op: str
    left: _Operand
    right: _Operand
    cond: Condition  # identity is the dedup key across the three passes
    rung_conds: tuple[Condition, ...]  # the enclosing rung's conditions, for `with rung(...)`


def _operand_of(value: Any) -> _Operand:
    """Classify a ``Compare*`` operand (a Tag, ImmediateRef, Expression, or literal)."""
    if isinstance(value, ImmediateRef):
        value = value.value
    if isinstance(value, Tag):
        return _Operand("tag", value.name, None, value)
    if isinstance(value, Expression):
        return _operand_of_expr(value)
    if isinstance(value, (int, float)):
        return _Operand("literal", None, value, value)
    return _Operand("opaque", None, None, value)


def _operand_of_expr(expr: Expression) -> _Operand:
    """Classify an ``ExprCompare`` side (an expression tree)."""
    if isinstance(expr, TagExpr):
        return _Operand("tag", expr.tag.name, None, expr)
    if isinstance(expr, LiteralExpr):
        return _Operand("literal", None, expr.value, expr)
    if isinstance(expr, (BinaryExpr, UnaryExpr)):
        return _Operand("computed", None, None, expr)
    return _Operand("opaque", None, None, expr)


def _render(op: _Operand) -> str:
    if op.kind == "tag":
        if isinstance(op.raw, Tag):
            return operand_name(op.raw)
        if isinstance(op.raw, TagExpr):
            return operand_name(op.raw.tag)
        return op.name or "?"
    if op.kind == "literal":
        return str(op.value)
    if op.kind == "computed":
        return _render_expr(op.raw)
    return "?"


def _render_expr(expr: Any) -> str:
    return render_source_expr(expr)


def _render_compare(cmp: _Compare) -> str:
    return f"{_render(cmp.left)} {cmp.op} {_render(cmp.right)}"


def _iter_compares(program: Program) -> Iterator[_Compare]:
    """Yield every comparison in every rung condition, both compare families."""
    for loc, rung in iter_rungs(program):
        conds = tuple(rung._conditions)
        for cond in conds:
            yield from _compares_in(loc.compact, cond, conds)


def _compares_in(
    loc: str, cond: Condition, rung_conds: tuple[Condition, ...]
) -> Iterator[_Compare]:
    if isinstance(cond, (AllCondition, AnyCondition)):
        for sub in cond.conditions:
            yield from _compares_in(loc, sub, rung_conds)
    elif isinstance(cond, (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)):
        yield _Compare(
            loc,
            _SYMBOL_BY_CLASS[type(cond)],
            _operand_of(cond.tag),
            _operand_of(cond.value),
            cond,
            rung_conds,
        )
    elif isinstance(cond, ExprCompare):
        yield _Compare(
            loc,
            cond.symbol,
            _operand_of_expr(cond.left),
            _operand_of_expr(cond.right),
            cond,
            rung_conds,
        )


# ---------------------------------------------------------------------------
# Program-level indexes
# ---------------------------------------------------------------------------


def _acc_index(program: Program) -> dict[str, AdvanceProfile]:
    """Map scalar advance coordinates to their instruction profiles."""
    index: dict[str, AdvanceProfile] = {}
    for instr in walk_instructions(program):
        profile = instr.advance_profile()
        if profile is not None and profile.accumulator is not None and profile.linear is not None:
            index[profile.accumulator.name] = profile
    return index


def _write_target_names(instr: Any) -> set[str]:
    """Tag names an instruction writes, unwrapping list-valued fields (drums, ``_outs``)."""
    names: set[str] = set()
    for field in getattr(type(instr), "_writes", ()):
        target = getattr(instr, field, None)
        targets = target if isinstance(target, (list, tuple)) else (target,)
        for one in targets:
            names.update(_resolve_tag_names(one))
    return names


def _written_names(program: Program) -> set[str]:
    """Every tag name written by any instruction (its declared ``_writes`` fields)."""
    names: set[str] = set()
    for instr in walk_instructions(program):
        names |= _write_target_names(instr)
    return names


def _calc_derived_names(program: Program) -> set[str]:
    """Tag names that are the product of a calculation (calc expr or affine transform).

    Reuses the prover's ``_extract_forward_affine`` — the affine extractor the
    functional-dependency projection pass builds on — so an identity ``copy`` is
    excluded while a ``dest = src ± k`` / calc expression is admitted.
    """
    from pyrung.core.analysis.prove.classify import _extract_forward_affine
    from pyrung.core.instruction.calc import CalcInstruction

    names: set[str] = set()
    for instr in walk_instructions(program):
        affine = _extract_forward_affine(instr)
        non_identity = affine is not None and (affine[1] != 1 or affine[2] != 0)
        if isinstance(instr, CalcInstruction) or non_identity:
            names |= _write_target_names(instr)
    return names


# ---------------------------------------------------------------------------
# Operand classification
# ---------------------------------------------------------------------------


def _is_dynamic(op: _Operand, written: set[str], acc: dict[str, AdvanceProfile]) -> bool:
    """True when the operand is a value the program produces: written or self-advancing."""
    if op.kind == "computed":
        return True
    if op.kind == "tag":
        return op.name in written or op.name in acc
    return False


def _is_static(op: _Operand, written: set[str], acc: dict[str, AdvanceProfile]) -> bool:
    """True when the operand is not program-produced: a literal or a never-written tag.

    Deliberately coarse: an external sensor input and an HMI setpoint both land here
    because neither is written by the ladder.  The rule does not pretend to tell a
    live measurement from a threshold — that ambiguity is carried by *severity*
    (see :func:`_static_on_left_finding`), not by trying to classify it away.
    """
    if op.kind == "literal":
        return True
    if op.kind == "tag":
        return op.name not in written and op.name not in acc
    return False


def _is_calc(op: _Operand, calc: set[str]) -> bool:
    if op.kind == "computed":
        return True
    return op.kind == "tag" and op.name in calc


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CmpConditionFinding:
    """A comparison-semantics finding (monotone/reset/operand-order)."""

    code: str
    target_name: str
    display: FindingDisplay
    severity: Severity

    @property
    def message(self) -> str:
        return self.display.as_text()


@dataclass(frozen=True)
class CmpConditionReport:
    findings: tuple[CmpConditionFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No comparison-semantics findings."
        return f"{len(self.findings)} comparison-semantics finding(s)."


# ---------------------------------------------------------------------------
# Rule 1 — CMP_EQ_ON_MONOTONE
# ---------------------------------------------------------------------------


def _monotone_side(
    cmp: _Compare, acc: dict[str, AdvanceProfile]
) -> tuple[_Operand, _Operand, AdvanceProfile] | None:
    """Return ``(register_operand, comparand, profile)`` when one side is an
    accumulator, else ``None``.  Prefers the left side if both are registers."""
    for reg, other in ((cmp.left, cmp.right), (cmp.right, cmp.left)):
        if reg.kind == "tag" and reg.name in acc:
            return reg, other, acc[reg.name]
    return None


def _done_hint(profile: AdvanceProfile) -> str:
    if profile.done is None:
        return ""
    return f" (or the '{operand_name(profile.done)}' done bit)"


def _eq_display(
    cmp: _Compare, reg: _Operand, comparand: _Operand, profile: AdvanceProfile
) -> FindingDisplay:
    assert profile.linear is not None
    order = ">=" if profile.linear.direction > 0 else "<="
    return FindingDisplay(
        code=CMP_EQ_ON_MONOTONE,
        severity="error",
        frames=(_cmp_frame(cmp, f"can skip past {_render(comparand)}"),),
        hint=f"use {_render(reg)} {order} {_render(comparand)}{_done_hint(profile)}",
    )


def _cmp_frame(cmp: _Compare, label: str = "") -> Frame:
    """A frame showing the comparison inside its ``with rung(...):`` header.

    The caret underlines the whole comparison and carries *label* — the problem in
    short form (``true at reset``, ``can skip past 5``).
    """
    header = with_rung_line(cmp.rung_conds)
    span = caret_of(header, _render_compare(cmp))
    return Frame(
        location=cmp.loc,
        lines=(header,),
        caret=(0, span[0], span[1]) if span else None,
        caret_label=label if span else "",
    )


# ---------------------------------------------------------------------------
# Rule 2 — CMP_TRUE_AT_RESET (shared by the STATIC_ON_LEFT escalation)
# ---------------------------------------------------------------------------


def _matches_preset(comparand: _Operand, preset: Any) -> bool:
    """True when the comparand is the accumulator's configured preset."""
    if isinstance(preset, Tag):
        return comparand.kind == "tag" and comparand.name == preset.name
    if isinstance(preset, (int, float)):
        return comparand.kind == "literal" and abs(comparand.value) == abs(preset)
    return False


def _true_at_reset_finding(
    cmp: _Compare, acc: dict[str, AdvanceProfile]
) -> CmpConditionFinding | None:
    """A CMP_TRUE_AT_RESET finding when *cmp* is TRUE at ``Acc = 0``, else ``None``.

    Gated to up-from-zero accumulators (``direction > 0``) whose comparand matches
    the configured preset — the botched-completion-check shape.  The ``Acc``-below-
    threshold orientation (``Acc < preset`` / ``Acc <= preset`` in register-left
    form) is exactly the predicate that is true from the scan the timer starts and
    false at the crossing.
    """
    side = _monotone_side(cmp, acc)
    if side is None:
        return None
    reg, comparand, profile = side
    if profile.linear is None or profile.linear.direction <= 0:
        return None
    reg_is_left = cmp.left is reg
    reg_left_op = cmp.op if reg_is_left else _FLIP[cmp.op]
    if reg_left_op not in ("<", "<="):  # must read "Acc below threshold"
        return None
    from pyrung.core.crossing import Eq

    if profile.done is None:
        return None
    boundary_step = profile.plan(Eq(profile.done.name, frozenset((True,))), {})
    boundary = boundary_step.until if boundary_step is not None else None
    preset = getattr(boundary, "bound", None)
    if getattr(boundary, "bound_is_tag", False):
        preset_matches = comparand.kind == "tag" and comparand.name == str(preset)
    else:
        preset_matches = _matches_preset(comparand, preset)
    if not preset_matches:
        return None
    return CmpConditionFinding(
        CMP_TRUE_AT_RESET,
        cmp.loc,
        _true_at_reset_display(cmp, reg, comparand, profile),
        "warning",
    )


def _true_at_reset_display(
    cmp: _Compare, reg: _Operand, comparand: _Operand, profile: AdvanceProfile
) -> FindingDisplay:
    label = "true at reset"
    return FindingDisplay(
        code=CMP_TRUE_AT_RESET,
        severity="warning",
        frames=(_cmp_frame(cmp, label),),
        hint=f"did you mean {_render(reg)} >= {_render(comparand)}{_done_hint(profile)}?",
    )


# ---------------------------------------------------------------------------
# Rule 3 — CMP_STATIC_ON_LEFT
# ---------------------------------------------------------------------------


def _static_on_left_finding(
    cmp: _Compare,
    written: set[str],
    acc: dict[str, AdvanceProfile],
    calc: set[str],
) -> CmpConditionFinding | None:
    """The operand-order finding, at a severity set by how sure we are.

    ``==`` / ``!=`` is cosmetic (same predicate either way) → **info**.  An ordered
    comparison whose dynamic side is a self-advancing register is a **KNOWN** order
    issue — we can prove the accumulator is the moving value → **warning**.  An
    ordered comparison between two ordinary tags is a **MAYBE** — we cannot tell a
    live measurement from a threshold → **advisory**, with hedged wording.
    """
    if not (_is_static(cmp.left, written, acc) and _is_dynamic(cmp.right, written, acc)):
        return None

    flip = f"{_render(cmp.right)} {_FLIP[cmp.op]} {_render(cmp.left)}"

    if cmp.op in ("==", "!="):
        display = FindingDisplay(
            code=CMP_STATIC_ON_LEFT,
            severity="info",
            frames=(_cmp_frame(cmp, "fixed value on the left"),),
            hint=f"{_render(cmp.left)} {cmp.op} {_render(cmp.right)} -> {flip}",
        )
        return CmpConditionFinding(CMP_STATIC_ON_LEFT, cmp.loc, display, "info")

    if cmp.right.kind == "tag" and cmp.right.name in acc:
        # KNOWN: the accumulator is provably the moving register.
        display = FindingDisplay(
            code=CMP_STATIC_ON_LEFT,
            severity="warning",
            frames=(_cmp_frame(cmp, f"{_render(cmp.right)} is what changes"),),
            hint=f"{_render_compare(cmp)} -> {flip}",
        )
        return CmpConditionFinding(CMP_STATIC_ON_LEFT, cmp.loc, display, "warning")

    # MAYBE: two ordinary tags — cannot prove which is measurement vs threshold.
    label = (
        f"{_render(cmp.right)} is calculated; likely the mover"
        if _is_calc(cmp.right, calc)
        else "which side is the moving value?"
    )
    display = FindingDisplay(
        code=CMP_STATIC_ON_LEFT,
        severity="advisory",
        frames=(_cmp_frame(cmp, label),),
        hint=f"put the changing side on the left: {flip}",
    )
    return CmpConditionFinding(CMP_STATIC_ON_LEFT, cmp.loc, display, "advisory")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_cmp_conditions(program: Program) -> CmpConditionReport:
    """Validate comparison semantics: monotone equality, reset-true, operand order.

    One pass, three codes.  Each comparison is reported at most once: a monotone
    equality (error) or a reset-true completion check (warning) claims the
    comparison, and the operand-order convention (info/warning) reports only what
    those two more specific rules leave uncovered — the escalation the spec calls
    for, without double-reporting.
    """
    acc = _acc_index(program)
    written = _written_names(program)
    calc = _calc_derived_names(program)

    compares = list(_iter_compares(program))
    claimed: set[int] = set()
    findings: list[CmpConditionFinding] = []

    # 1. Equality against a self-advancing register (error). The reset-floor check
    #    (== 0 / != 0) is edge-safe and exempt.
    for cmp in compares:
        if cmp.op not in ("==", "!="):
            continue
        side = _monotone_side(cmp, acc)
        if side is None:
            continue
        reg, comparand, profile = side
        if comparand.kind == "literal" and comparand.value == 0:
            continue
        findings.append(
            CmpConditionFinding(
                CMP_EQ_ON_MONOTONE, cmp.loc, _eq_display(cmp, reg, comparand, profile), "error"
            )
        )
        claimed.add(id(cmp.cond))

    # 2. True-at-reset completion check (warning), either operand order.
    for cmp in compares:
        if id(cmp.cond) in claimed or cmp.op not in ("<", "<=", ">", ">="):
            continue
        finding = _true_at_reset_finding(cmp, acc)
        if finding is not None:
            findings.append(finding)
            claimed.add(id(cmp.cond))

    # 3. Operand-order convention (info/warning) for whatever remains.
    for cmp in compares:
        if id(cmp.cond) in claimed:
            continue
        finding = _static_on_left_finding(cmp, written, acc, calc)
        if finding is not None:
            findings.append(finding)

    return CmpConditionReport(findings=tuple(findings))
