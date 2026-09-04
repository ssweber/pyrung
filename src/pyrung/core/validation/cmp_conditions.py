"""Comparison-semantics validators: domains, monotone/reset/order/zero/stepper operands.

Nine rules, one pass over every comparison in every rung condition (main +
subroutines + branches, both the ``Compare*`` leaf family and the expression-tree
``ExprCompare``):

* ``CMP_ALWAYS_FALSE`` / ``CMP_ALWAYS_TRUE`` — a comparison has one truth value
  over complete declared or producer-derived domains. **warning** / **info**.

* ``CMP_EQ_ON_MONOTONE`` — ``==`` / ``!=`` against a self-advancing register
  (``Timer.Acc``, a counter accumulator).  The register steps by ``rate_per_scan``
  each scan and can jump *over* the compared value between scans, so the equality
  may never latch.  **warning**.  The ``== 0`` / ``!= 0`` floor check is edge-safe and
  exempt.

* ``CMP_TRUE_AT_RESET`` — an ordered comparison that is TRUE at the accumulator's
  reset value (``Acc = 0``) and FALSE at the crossing: the exact complement of a
  completion check, firing a spurious pulse on every state entry where ``Acc``
  resets.  Gated to up-from-zero accumulators with the comparand matching the
  configured preset — zero false positives.  **warning**.

* ``CMP_STATIC_ON_LEFT`` — the operand-order convention: the moving value on the
  left, the threshold on the right.  This is always an **advisory** because reversing
  both the operands and operator preserves the program's behavior.  Confidence only
  sharpens the wording.  ``==`` and ``!=`` are exempt because their order conveys no
  direction.  A monotone register on the right that is true at reset escalates
  through ``CMP_TRUE_AT_RESET`` instead.

* ``CMP_OPERAND_NO_WRITER`` — a numeric tag used directly in a comparison has no
  ladder writer.  Explicit defaults, external inputs, physical inputs, read-only
  constants, and numeric ``==``/``!= 0``/``1`` Boolean conventions are exempt.
  The missing source may be intentional, so this is an **advisory**.

* ``CMP_PRESET_STAYS_ZERO`` — the same high-confidence check for a tag-valued
  timer/counter preset.  Literal zero remains an intentional, supported elapsed-
  accumulator idiom and is exempt.  **warning**.

* ``CMP_STEPPER_VALUE_NOT_SET`` — a discrete stepping tag is tested for
  equality with a numeric value that none of its fully understood direct or
  indirect producers can establish.  Unknown writer paths punt rather than
  speculate.  **warning**.

* ``CMP_REPEATED_STATE_VALUE`` — repeated equality checks against the same
  discrete numeric values have become hard to read or maintain as raw numbers.
  Suggests a named read-only reference or one mapped Bool status tag.  **advisory**.

"Dynamic" (belongs on the left) is any program-written tag, self-advancing register,
or inline computed expression; "static" is a literal, an ``S.`` constant, or any
never-written tag (an external sensor and an HMI setpoint both land here — the rule
does not classify measurement vs threshold, it grades the finding by confidence).
Calc-derived provenance is recovered via shared affine analysis and sharpens the
message without changing the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.affine import extract_forward_affine
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
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation._common import (
    RungLoc,
    _collect_write_sites,
    _resolve_tag_names,
    iter_rungs,
    site_frame,
    walk_instructions,
)
from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin
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
    from pyrung.core.validation.context import ValidationContext
    from pyrung.core.validation.severity import Severity

CMP_EQ_ON_MONOTONE = "CMP_EQ_ON_MONOTONE"
CMP_TRUE_AT_RESET = "CMP_TRUE_AT_RESET"
CMP_STATIC_ON_LEFT = "CMP_STATIC_ON_LEFT"
CMP_OPERAND_NO_WRITER = "CMP_OPERAND_NO_WRITER"
CMP_PRESET_STAYS_ZERO = "CMP_PRESET_STAYS_ZERO"
CMP_STEPPER_VALUE_NOT_SET = "CMP_STEPPER_VALUE_NOT_SET"
CMP_REPEATED_STATE_VALUE = "CMP_REPEATED_STATE_VALUE"
CMP_ALWAYS_FALSE = "CMP_ALWAYS_FALSE"
CMP_ALWAYS_TRUE = "CMP_ALWAYS_TRUE"

_NUMERIC_TAG_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.REAL, TagType.WORD})
_DISCRETE_NUMERIC_TAG_TYPES = frozenset({TagType.INT, TagType.DINT, TagType.WORD})

# Repeated-state advice is deliberately statistical. Keep every tuning knob here
# so the rule can be made quieter or more eager without hunting through its logic.
_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS = 4
_REPEATED_STATE_BREADTH_MIN_VALUES = 2
_REPEATED_STATE_BREADTH_MIN_RUNGS_PER_VALUE = 2
_REPEATED_STATE_DISPERSION_MIN_RUNGS = 2
_REPEATED_STATE_CROSS_SCOPE_MIN_SCOPES = 2
_REPEATED_STATE_MIN_INTERVENING_RUNGS = 8
_REPEATED_STATE_BOOLISH_VALUES = frozenset({0, 1})

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

_COMPARE_VALUE: dict[str, Any] = {
    "==": lambda left, right: left == right,
    "!=": lambda left, right: left != right,
    "<": lambda left, right: left < right,
    "<=": lambda left, right: left <= right,
    ">": lambda left, right: left > right,
    ">=": lambda left, right: left >= right,
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

    rung_loc: RungLoc
    op: str
    left: _Operand
    right: _Operand
    cond: Condition  # identity is the dedup key across the three passes
    rung_conds: tuple[Condition, ...]  # the enclosing rung's conditions, for `with rung(...)`

    @property
    def loc(self) -> str:
        return self.rung_loc.compact


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


def _operand_domain(
    operand: _Operand,
    domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    if operand.kind == "literal":
        return (operand.value,)
    if operand.kind == "tag" and operand.name is not None:
        return domains.get(operand.name)
    return None


def _constant_result(
    cmp: _Compare,
    domains: dict[str, tuple[Any, ...]],
) -> bool | None:
    """Return a comparison's sole truth value, or None when it can vary."""
    if cmp.left.kind == "tag" and cmp.right.kind == "tag" and cmp.left.name == cmp.right.name:
        domain = domains.get(cmp.left.name)
        if domain is None:
            return None
        outcomes: set[bool] = set()
        for value in domain:
            try:
                outcomes.add(bool(_COMPARE_VALUE[cmp.op](value, value)))
            except (TypeError, ValueError):
                return None
            if len(outcomes) > 1:
                return None
        return next(iter(outcomes)) if outcomes else None
    left = _operand_domain(cmp.left, domains)
    right = _operand_domain(cmp.right, domains)
    if left is None or right is None:
        return None
    outcomes: set[bool] = set()
    for left_value in left:
        for right_value in right:
            try:
                outcomes.add(bool(_COMPARE_VALUE[cmp.op](left_value, right_value)))
            except (TypeError, ValueError):
                return None
            if len(outcomes) > 1:
                return None
    return next(iter(outcomes)) if outcomes else None


def _format_domain_values(domain: tuple[Any, ...], tag: Tag | None = None) -> str:
    """Render a closed domain compactly: ``0..90``, ``1, 3, 5``, or an elided list.

    A small ``choices`` domain keeps its labels (``0 ('Idle'), 1 ('Run')``) so the
    reader sees the named states rather than bare numbers.
    """
    values = list(domain)
    if tag is not None and tag.choices and len(values) <= 8:
        return ", ".join(_choice_value(tag, v) for v in values)
    ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    if len(ints) == len(values) and len(values) > 1:
        ordered = sorted(ints)
        if ordered[-1] - ordered[0] == len(ordered) - 1:
            return f"{ordered[0]}..{ordered[-1]}"
        values = ordered
    if len(values) <= 8:
        return ", ".join(str(v) for v in values)
    shown = (*values[:5], "...", *values[-2:])
    return f"{', '.join(str(v) for v in shown)} ({len(values)} values)"


def _writer_locations(name: str, graph: Any) -> str:
    """``Main:R2, Main:R7`` for the rungs that write *name* (``...`` past three)."""
    from pyrung.core.validation._common import compact_location

    nodes = sorted(graph.writers_of.get(name) or ())
    locs = [
        compact_location(n.scope, n.subroutine, n.rung_index, n.branch_path)
        for n in (graph.rung_nodes[i] for i in nodes)
    ]
    if len(locs) > 3:
        locs = [*locs[:3], "..."]
    return ", ".join(locs)


def _domain_provenance(tag: Tag, domain: tuple[Any, ...], graph: Any) -> tuple[str, str]:
    """``(why the domain is closed, how to widen it)`` for one operand tag.

    The domain is closed either by a declaration on the tag (``choices``,
    ``min``/``max``, Bool) or because every ladder writer is understood.  The
    hint names which, so the engineer knows whether to fix the declaration or the
    writers.
    """
    from pyrung.core.analysis.value_domains import declared_value_domain

    name = operand_name(tag)
    values = _format_domain_values(domain, tag)
    if declared_value_domain(tag) is not None:
        if tag.type is TagType.BOOL:
            return f"{name} is a Bool", "change the comparison"
        if tag.choices:
            return (
                f"{name}'s choices are declared on the tag",
                f"change the comparison or add the value to {name}'s choices",
            )
        return (
            f"{name} is declared min={tag.min}, max={tag.max}",
            f"change the comparison or widen {name}'s min/max",
        )
    if tag.readonly:
        return f"{name} is readonly and always {values}", "change the comparison"
    writers = _writer_locations(tag.name, graph)
    if not writers:
        return (
            f"{name} is never written, so it keeps its default {values}",
            f"change the comparison, write {name} from the ladder, or mark it external",
        )
    return (
        f"{name} is only written at {writers}",
        f"change the comparison or write the expected value to {name}",
    )


def _constant_display(
    cmp: _Compare,
    result: bool,
    domains: dict[str, tuple[Any, ...]],
    graph: Any,
) -> FindingDisplay:
    """Explain a one-valued comparison by naming each operand's closed domain."""
    verdict = "always true" if result else "always false"
    seen: set[str] = set()
    reasons: list[str] = []
    fixes: list[str] = []
    labels: list[str] = []
    for operand in (cmp.left, cmp.right):
        tag = _operand_tag(operand)
        if tag is None or tag.name in seen or tag.name not in domains:
            continue
        seen.add(tag.name)
        domain = domains[tag.name]
        reason, fix = _domain_provenance(tag, domain, graph)
        reasons.append(reason)
        fixes.append(fix)
        labels.append(f"{operand_name(tag)} is only {_format_domain_values(domain, tag)}")
    label = f"{verdict}: {'; '.join(labels)}" if labels else verdict
    if result:
        hint = "remove the redundant comparison"
    else:
        hint = fixes[0] if len(fixes) == 1 else "change the comparison or the operands' domains"
    if reasons:
        hint = f"{'; '.join(reasons)}; {hint}"
    return FindingDisplay(
        code=CMP_ALWAYS_TRUE if result else CMP_ALWAYS_FALSE,
        severity="info" if result else "warning",
        frames=(_cmp_frame(cmp, label),),
        hint=hint,
    )


def _iter_compares(program: Program) -> Iterator[_Compare]:
    """Yield every comparison in every rung condition, both compare families."""
    for loc, rung in iter_rungs(program):
        conds = tuple(rung._conditions)
        for cond in conds:
            yield from _compares_in(loc, cond, conds)


def _compares_in(
    loc: RungLoc, cond: Condition, rung_conds: tuple[Condition, ...]
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

    Reuses shared affine analysis so an identity ``copy`` is
    excluded while a ``dest = src ± k`` / calc expression is admitted.
    """
    from pyrung.core.instruction.calc import CalcInstruction

    names: set[str] = set()
    for instr in walk_instructions(program):
        affine = extract_forward_affine(instr)
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
    live measurement from a threshold; that ambiguity is reflected in the wording
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
# Operand sources and zero-preset establishment
# ---------------------------------------------------------------------------


def _operand_tag(op: _Operand) -> Tag | None:
    """Return the direct tag represented by *op*, if it is one."""
    if isinstance(op.raw, Tag):
        return op.raw
    if isinstance(op.raw, TagExpr):
        return op.raw.tag
    return None


def _tag_stays_zero(tag: Tag, graph: Any) -> bool:
    """Whether *tag* provably remains at numeric zero inside the ladder.

    Pyrung gives every numeric tag a real zero initialization value.  The defect
    this rule targets is narrower than ordinary "use before initialization": the
    tag has no ladder writer at all, so that zero never changes.  A configured
    default, declared external source, physical input, or read-only zero constant
    carries explicit intent and is left alone.
    """
    if (
        tag.type not in _NUMERIC_TAG_TYPES
        or tag.default != 0
        or getattr(tag, "has_explicit_default", False)
    ):
        return False
    if tag.external or tag.readonly or graph.is_physical_input(tag.name):
        return False
    return not graph.writers_of.get(tag.name)


def _tag_has_no_writer(tag: Tag, graph: Any) -> bool:
    """Whether *tag* is an undeclared numeric source with no ladder writer."""
    if tag.type not in _NUMERIC_TAG_TYPES or getattr(tag, "has_explicit_default", False):
        return False
    if tag.external or tag.readonly or graph.is_physical_input(tag.name):
        return False
    return not graph.writers_of.get(tag.name)


def _no_writer_operand(cmp: _Compare, graph: Any) -> tuple[_Operand, Tag] | None:
    """Prefer the conventional right-hand bound, then check the left operand."""
    if _is_boolish_numeric_compare(cmp):
        return None
    for operand in (cmp.right, cmp.left):
        tag = _operand_tag(operand)
        if tag is not None and _tag_has_no_writer(tag, graph):
            return operand, tag
    return None


def _is_boolish_numeric_compare(cmp: _Compare) -> bool:
    """Whether a numeric comparison is being used as a Boolean convention."""
    if cmp.op not in ("==", "!="):
        return False
    for tag_operand, literal_operand in ((cmp.left, cmp.right), (cmp.right, cmp.left)):
        tag = _operand_tag(tag_operand)
        value = literal_operand.value
        if (
            tag is not None
            and tag.type in _NUMERIC_TAG_TYPES
            and literal_operand.kind == "literal"
            and not isinstance(value, bool)
            and value in (0, 1)
        ):
            return True
    return False


def _preset_sites(program: Program, graph: Any) -> list[tuple[Any, Tag, Any]]:
    """``(write_site, preset_tag, instruction)`` for zero timer/counter presets."""
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    accumulating = (
        OnDelayInstruction,
        OffDelayInstruction,
        CountUpInstruction,
        CountDownInstruction,
    )
    sites: list[tuple[Any, Tag, Any]] = []
    seen: set[int] = set()
    for site in _collect_write_sites(program):
        instr = site.instruction
        if not isinstance(instr, accumulating) or id(instr) in seen:
            continue
        # _collect_write_sites emits Done and Acc sites.  Keep the accumulator
        # site as the instruction anchor and discard the duplicate Done site.
        if site.target_name != instr.accumulator.name:
            continue
        preset = instr.preset
        if isinstance(preset, Tag) and _tag_stays_zero(preset, graph):
            sites.append((site, preset, instr))
            seen.add(id(instr))
    return sites


def _preset_display(site: Any, preset: Tag) -> FindingDisplay:
    name = operand_name(preset)
    return FindingDisplay(
        code=CMP_PRESET_STAYS_ZERO,
        severity="warning",
        frames=(site_frame(site, caret_token=name, caret_label="preset stays 0"),),
        hint=f"set {name} to the intended delay or count, or mark it external",
    )


def _no_writer_operand_display(cmp: _Compare, operand: _Operand, tag: Tag) -> FindingDisplay:
    header = with_rung_line(cmp.rung_conds)
    token = _render(operand)
    span = caret_of(header, token)
    frame = Frame(
        location=cmp.loc,
        lines=(header,),
        caret=(0, span[0], span[1]) if span else None,
        caret_label="no ladder writer" if span else "",
    )
    name = operand_name(tag)
    return FindingDisplay(
        code=CMP_OPERAND_NO_WRITER,
        severity="advisory",
        frames=(frame,),
        hint=(
            f"set {name} in the ladder, or mark it external if an HMI, device, or test supplies it"
        ),
    )


def _stepper_value_display(
    cmp: _Compare,
    tag_operand: _Operand,
    literal_operand: _Operand,
    domain: tuple[Any, ...],
) -> FindingDisplay:
    if len(domain) <= 8:
        values = ", ".join(str(value) for value in domain)
    else:
        shown = (*domain[:5], "...", *domain[-2:])
        values = f"{', '.join(str(value) for value in shown)} ({len(domain)} values)"
    header = with_rung_line(cmp.rung_conds)
    literal = _render(literal_operand)
    span = caret_of(header, literal)
    frame = Frame(
        location=cmp.loc,
        lines=(header,),
        caret=(0, span[0], span[1]) if span else None,
        caret_label=f"{_render(tag_operand)} never set to {literal}" if span else "",
    )
    return FindingDisplay(
        code=CMP_STEPPER_VALUE_NOT_SET,
        severity="warning",
        frames=(frame,),
        hint=(
            f"{_render(tag_operand)} is established as: {values}; "
            "check the comparison or add the missing copy"
        ),
    )


def _unset_stepper_value(
    cmp: _Compare,
    stepping: frozenset[str],
    produced_domains: dict[str, tuple[Any, ...]],
) -> tuple[_Operand, _Operand, tuple[Any, ...]] | None:
    """Return a fully evidenced missing equality value, otherwise punt.

    Ordered comparisons are intentionally excluded: ``State >= 3`` can be
    useful even when a discrete state register jumps from 2 to 4.  ``!=`` is
    also excluded because defensive exclusions are commonly intentional and a
    warning there would be more pedantic than useful.
    """
    if cmp.op != "==":
        return None
    for tag_operand, literal_operand in ((cmp.left, cmp.right), (cmp.right, cmp.left)):
        if (
            tag_operand.kind != "tag"
            or tag_operand.name not in stepping
            or literal_operand.kind != "literal"
            or isinstance(literal_operand.value, bool)
            or not isinstance(literal_operand.value, (int, float))
        ):
            continue
        domain = produced_domains.get(tag_operand.name)
        if domain is not None and literal_operand.value not in domain:
            return tag_operand, literal_operand, domain
    return None


# ---------------------------------------------------------------------------
# Repeated state-value comparisons
# ---------------------------------------------------------------------------

_RungKey = tuple[str, str | None, int]
_ScopeKey = tuple[str, str | None]


def _rung_key(cmp: _Compare) -> _RungKey:
    loc = cmp.rung_loc
    return loc.scope, loc.subroutine, loc.rung_index


def _scope_key(cmp: _Compare) -> _ScopeKey:
    loc = cmp.rung_loc
    return loc.scope, loc.subroutine


def _state_literal_pair(cmp: _Compare) -> tuple[Tag, int] | None:
    """Return a direct discrete-tag equality and its integer literal."""
    if cmp.op != "==":
        return None
    for tag_operand, literal_operand in ((cmp.left, cmp.right), (cmp.right, cmp.left)):
        tag = _operand_tag(tag_operand)
        value = literal_operand.value
        if (
            tag is not None
            and tag.type in _DISCRETE_NUMERIC_TAG_TYPES
            and literal_operand.kind == "literal"
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            return tag, value
    return None


def _dedupe_top_level_rungs(compares: list[_Compare]) -> list[_Compare]:
    """Keep one comparison per top-level rung, collapsing parallel branches."""
    seen: set[_RungKey] = set()
    unique: list[_Compare] = []
    for cmp in compares:
        key = _rung_key(cmp)
        if key not in seen:
            seen.add(key)
            unique.append(cmp)
    return unique


def _scope_label(scope: _ScopeKey) -> str:
    return "Main" if scope[0] == "main" else scope[1] or "subroutine"


def _state_value_dispersion_reasons(
    value: int,
    sites: list[_Compare],
    all_sites: dict[int, list[_Compare]],
    tag: Tag,
) -> list[str]:
    if len(sites) < _REPEATED_STATE_DISPERSION_MIN_RUNGS:
        return []

    scopes = {_scope_key(cmp) for cmp in sites}
    if len(scopes) >= _REPEATED_STATE_CROSS_SCOPE_MIN_SCOPES:
        names = ", ".join(_scope_label(scope) for scope in sorted(scopes))
        return [f"{_choice_value(tag, value)} is compared in {names}"]

    scope = next(iter(scopes))
    rung_indexes = sorted(cmp.rung_loc.rung_index for cmp in sites)
    reasons: list[str] = []
    largest_gap = max(
        (right - left - 1 for left, right in zip(rung_indexes, rung_indexes[1:], strict=False)),
        default=0,
    )
    if largest_gap >= _REPEATED_STATE_MIN_INTERVENING_RUNGS:
        reasons.append(
            f"{_choice_value(tag, value)} is compared again after {largest_gap} intervening rungs "
            f"(limit: {_REPEATED_STATE_MIN_INTERVENING_RUNGS})"
        )

    first, last = rung_indexes[0], rung_indexes[-1]
    interleaved = sorted(
        other_value
        for other_value, other_sites in all_sites.items()
        if other_value != value
        and any(
            _scope_key(cmp) == scope and first < cmp.rung_loc.rung_index < last
            for cmp in other_sites
        )
    )
    if interleaved:
        values = ", ".join(_choice_value(tag, other) for other in interleaved)
        reasons.append(
            f"{_choice_value(tag, value)} is compared on both sides of another value ({values})"
        )
    return reasons


def _choice_value(tag: Tag, value: int) -> str:
    label = tag.choices.get(value) if tag.choices is not None else None
    return f"{value} ({label!r})" if label is not None else str(value)


def _repeated_state_findings(compares: list[_Compare]) -> list[CmpConditionFinding]:
    """One advisory per tag whose raw state comparisons cross a tuning threshold."""
    grouped: dict[str, tuple[Tag, dict[int, list[_Compare]]]] = {}
    for cmp in compares:
        pair = _state_literal_pair(cmp)
        if pair is None:
            continue
        tag, value = pair
        _stored_tag, by_value = grouped.setdefault(tag.name, (tag, {}))
        by_value.setdefault(value, []).append(cmp)

    findings: list[CmpConditionFinding] = []
    for tag_name, (tag, raw_by_value) in grouped.items():
        if set(raw_by_value) <= _REPEATED_STATE_BOOLISH_VALUES:
            continue

        by_value = {value: _dedupe_top_level_rungs(sites) for value, sites in raw_by_value.items()}
        repeated = {
            value: sites
            for value, sites in by_value.items()
            if len(sites) >= _REPEATED_STATE_BREADTH_MIN_RUNGS_PER_VALUE
        }
        qualifying: set[int] = {
            value
            for value, sites in by_value.items()
            if len(sites) >= _REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS
        }
        if len(repeated) >= _REPEATED_STATE_BREADTH_MIN_VALUES:
            qualifying.update(repeated)
        for value, sites in by_value.items():
            dispersion_reasons = _state_value_dispersion_reasons(value, sites, by_value, tag)
            if dispersion_reasons:
                qualifying.add(value)
        if not qualifying:
            continue

        ordered_values = sorted(qualifying)
        descriptions = [
            f"{_choice_value(tag, value)} on {len(by_value[value])} rungs"
            for value in ordered_values
        ]
        problem = f"{tag_name} compares {'; '.join(descriptions)}."
        frames = tuple(
            _cmp_frame(cmp, "repeated raw value")
            for value in ordered_values
            for cmp in by_value[value]
        )
        display = FindingDisplay(
            code=CMP_REPEATED_STATE_VALUE,
            severity="advisory",
            frames=frames,
            problem=problem,
            hint="name the value with a read-only tag, or map it once to a Bool status tag",
        )
        findings.append(
            CmpConditionFinding(CMP_REPEATED_STATE_VALUE, tag_name, display, "advisory")
        )
    return findings


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CmpConditionFinding(_FindingTextMixin):
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
    hint = (
        f"use < or > to say which side of {_render(comparand)} should be true"
        if cmp.op == "!="
        else f"use {_render(reg)} {order} {_render(comparand)}{_done_hint(profile)}"
    )
    return FindingDisplay(
        code=CMP_EQ_ON_MONOTONE,
        severity="warning",
        frames=(_cmp_frame(cmp, f"can skip past {_render(comparand)}"),),
        hint=hint,
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
    label = f"true when {_render(reg)} is 0"
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
    """An advisory to put the moving value on the conventional left side.

    The rewrite is behavior-preserving in every confidence tier.  Certainty about
    the moving operand changes the explanation, not the severity.
    """
    if cmp.op in ("==", "!=") or not (
        _is_static(cmp.left, written, acc) and _is_dynamic(cmp.right, written, acc)
    ):
        return None

    flip = f"{_render(cmp.right)} {_FLIP[cmp.op]} {_render(cmp.left)}"

    if cmp.right.kind == "tag" and cmp.right.name in acc:
        changing = _render(cmp.right)
        display = FindingDisplay(
            code=CMP_STATIC_ON_LEFT,
            severity="advisory",
            frames=(_cmp_frame(cmp, f"{changing} changes, but it is on the right"),),
            hint=f"put the changing value on the left: {flip}",
        )
        return CmpConditionFinding(CMP_STATIC_ON_LEFT, cmp.loc, display, "advisory")

    changing = _render(cmp.right)
    label = (
        f"{changing} is calculated and may be what changes"
        if _is_calc(cmp.right, calc)
        else f"{changing} may be what changes"
    )
    display = FindingDisplay(
        code=CMP_STATIC_ON_LEFT,
        severity="advisory",
        frames=(_cmp_frame(cmp, label),),
        hint=f"if {changing} changes, put it on the left: {flip}",
    )
    return CmpConditionFinding(CMP_STATIC_ON_LEFT, cmp.loc, display, "advisory")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_cmp_conditions(
    program: Program,
    *,
    _context: ValidationContext | None = None,
) -> CmpConditionReport:
    """Validate comparison semantics and zero-valued operands/presets.

    Each comparison is reported at most once.  The preset rule is instruction-
    scoped and can coexist with a distinct comparison error, but it owns the usual
    ``Acc >= same_preset`` completion comparison so the same zero is not reported
    twice.
    """
    from pyrung.core.analysis.prove.classify import (
        _compute_stepping_tags,
    )
    from pyrung.core.validation.context import ValidationContext

    acc = _acc_index(program)
    written = _written_names(program)
    calc = _calc_derived_names(program)
    context = _context or ValidationContext(program)
    graph = context.graph
    stepping = _compute_stepping_tags(program, graph)
    produced_domains = context.produced_domains
    closed_domains = context.closed_domains

    compares = list(_iter_compares(program))
    claimed: set[int] = set()
    findings: list[CmpConditionFinding] = []

    preset_sites = _preset_sites(program, graph)
    zero_preset_pairs = {
        (instr.accumulator.name, preset.name) for _site, preset, instr in preset_sites
    }
    for site, preset, _instr in preset_sites:
        findings.append(
            CmpConditionFinding(
                CMP_PRESET_STAYS_ZERO,
                preset.name,
                _preset_display(site, preset),
                "warning",
            )
        )

    # 1. Equality against a self-advancing register (warning). The reset-floor check
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
                CMP_EQ_ON_MONOTONE,
                cmp.loc,
                _eq_display(cmp, reg, comparand, profile),
                "warning",
            )
        )
        claimed.add(id(cmp.cond))

    # 2. A numeric operand has no declared source.  A comparison that mirrors a
    #    zero timer/counter preset belongs to the more specific preset finding.
    for cmp in compares:
        if id(cmp.cond) in claimed:
            continue
        no_writer = _no_writer_operand(cmp, graph)
        if no_writer is None:
            continue
        operand, tag = no_writer
        operand_names = {
            op.name for op in (cmp.left, cmp.right) if op.kind == "tag" and op.name is not None
        }
        mirrors_zero_preset = any(
            {acc_name, preset_name} == operand_names for acc_name, preset_name in zero_preset_pairs
        )
        if not mirrors_zero_preset:
            findings.append(
                CmpConditionFinding(
                    CMP_OPERAND_NO_WRITER,
                    tag.name,
                    _no_writer_operand_display(cmp, operand, tag),
                    "advisory",
                )
            )
        claimed.add(id(cmp.cond))

    # 3. A stepping tag compared for equality with a value no complete producer
    #    path can establish.  This owns the comparison before stylistic checks.
    for cmp in compares:
        if id(cmp.cond) in claimed:
            continue
        missing = _unset_stepper_value(cmp, stepping, produced_domains)
        if missing is None:
            continue
        tag_operand, literal_operand, domain = missing
        findings.append(
            CmpConditionFinding(
                CMP_STEPPER_VALUE_NOT_SET,
                tag_operand.name or cmp.loc,
                _stepper_value_display(cmp, tag_operand, literal_operand, domain),
                "warning",
            )
        )
        claimed.add(id(cmp.cond))

    # 4. A comparison whose complete operand domains yield one truth value.
    #    Preserve the more specific stays-zero and stepper diagnostics above.
    for cmp in compares:
        if id(cmp.cond) in claimed:
            continue
        result = _constant_result(cmp, closed_domains)
        if result is None:
            continue
        code = CMP_ALWAYS_TRUE if result else CMP_ALWAYS_FALSE
        severity: Severity = "info" if result else "warning"
        findings.append(
            CmpConditionFinding(
                code, cmp.loc, _constant_display(cmp, result, closed_domains, graph), severity
            )
        )
        claimed.add(id(cmp.cond))

    # 5. True-at-reset completion check (warning), either operand order.
    for cmp in compares:
        if id(cmp.cond) in claimed or cmp.op not in ("<", "<=", ">", ">="):
            continue
        finding = _true_at_reset_finding(cmp, acc)
        if finding is not None:
            findings.append(finding)
            claimed.add(id(cmp.cond))

    # 6. Operand-order convention (advisory) for whatever remains.
    for cmp in compares:
        if id(cmp.cond) in claimed:
            continue
        finding = _static_on_left_finding(cmp, written, acc, calc)
        if finding is not None:
            findings.append(finding)

    # 7. Program-level readability advice is independent of the per-comparison
    #    ownership above: an otherwise valid comparison can still be repeated.
    findings.extend(_repeated_state_findings(compares))

    return CmpConditionReport(findings=tuple(findings))
