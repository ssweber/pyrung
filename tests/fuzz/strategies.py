"""Hypothesis strategies for generating fuzzer specs and building programs."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Any

import hypothesis.strategies as st
from hypothesis import assume

from pyrung.core import Or, Program, Rung, calc, copy, latch, out, reset

from .pool import TagPool, tag_pools

# ---------------------------------------------------------------------------
# Value strategies
# ---------------------------------------------------------------------------


def int_values() -> st.SearchStrategy[int]:
    return st.one_of(st.sampled_from([0, 1, -1, 10, 100]), st.integers(-100, 100))


def timer_presets() -> st.SearchStrategy[int]:
    return st.one_of(st.sampled_from([0, 1, 10, 50, 100]), st.integers(0, 100))


def counter_presets() -> st.SearchStrategy[int]:
    return st.one_of(st.sampled_from([0, 1, 5, 10]), st.integers(0, 10))


# ---------------------------------------------------------------------------
# Condition specs
# ---------------------------------------------------------------------------

_COMPARE_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass
class CondSpec:
    kind: str
    tag: Any = None
    op: str | None = None
    operand: Any = None


@st.composite
def condition_specs(draw: st.DrawFn, pool: TagPool) -> CondSpec:
    conditions = pool.all_conditions()
    assume(len(conditions) > 0)

    numerics = pool.all_numeric()
    int_or_dint = pool.int_tags + pool.dint_tags

    weights = [40, 15, 35 if numerics else 0, 10 if int_or_dint else 0]
    total = sum(weights)
    if total == 0:
        weights = [55, 15, 0, 0]
        total = 70

    kind = draw(
        st.sampled_from(
            [
                k
                for k, w in zip(["bit", "negated", "compare", "truthy"], weights, strict=True)
                if w > 0
            ]
        )
    )

    if kind == "bit":
        tag = draw(st.sampled_from(conditions))
        return CondSpec(kind="bit", tag=tag)
    elif kind == "negated":
        tag = draw(st.sampled_from(conditions))
        return CondSpec(kind="negated", tag=tag)
    elif kind == "compare":
        tag = draw(st.sampled_from(numerics))
        op = draw(st.sampled_from(list(_COMPARE_OPS.keys())))
        value = draw(int_values())
        return CondSpec(kind="compare", tag=tag, op=op, operand=value)
    else:
        tag = draw(st.sampled_from(int_or_dint))
        return CondSpec(kind="truthy", tag=tag)


def build_condition(spec: CondSpec) -> Any:
    if spec.kind == "bit":
        return spec.tag
    elif spec.kind == "negated":
        return ~spec.tag
    elif spec.kind == "compare":
        return _COMPARE_OPS[spec.op](spec.tag, spec.operand)
    else:
        return spec.tag


# ---------------------------------------------------------------------------
# Instruction specs
# ---------------------------------------------------------------------------


@dataclass
class InstrSpec:
    kind: str
    args: dict[str, Any] = field(default_factory=dict)


@st.composite
def instruction_specs(draw: st.DrawFn, pool: TagPool) -> InstrSpec:
    writable_bool = pool.writable_bool()
    writable_numeric = pool.writable_numeric()
    has_bool = len(writable_bool) > 0
    has_numeric = len(writable_numeric) > 0
    assume(has_bool or has_numeric)

    choices: list[tuple[str, int]] = []
    if has_bool:
        choices.extend([("out", 25), ("latch", 10), ("reset_bool", 10)])
    if has_numeric:
        choices.extend([("copy", 35), ("calc", 20)])
    if not has_bool and has_numeric:
        choices.append(("reset_numeric", 10))

    kinds = [c[0] for c in choices]
    kind = draw(st.sampled_from(kinds))

    if kind == "out":
        target = draw(st.sampled_from(writable_bool))
        return InstrSpec(kind="out", args={"target": target})
    elif kind == "latch":
        target = draw(st.sampled_from(writable_bool))
        return InstrSpec(kind="latch", args={"target": target})
    elif kind == "reset_bool":
        target = draw(st.sampled_from(writable_bool))
        return InstrSpec(kind="reset", args={"target": target})
    elif kind == "reset_numeric":
        target = draw(st.sampled_from(writable_numeric))
        return InstrSpec(kind="reset", args={"target": target})
    elif kind == "copy":
        dest = draw(st.sampled_from(writable_numeric))
        use_literal = draw(st.booleans())
        if use_literal or not writable_numeric:
            source = draw(int_values())
        else:
            source = draw(st.sampled_from(pool.all_numeric()))
        return InstrSpec(kind="copy", args={"source": source, "dest": dest})
    else:
        dest = draw(st.sampled_from(writable_numeric))
        source = draw(st.sampled_from(pool.all_numeric()))
        op = draw(st.sampled_from(["add", "sub", "mul"]))
        literal = draw(int_values())
        return InstrSpec(
            kind="calc", args={"source": source, "op": op, "literal": literal, "dest": dest}
        )


def emit_instruction(spec: InstrSpec) -> None:
    kind = spec.kind
    args = spec.args
    if kind == "out":
        out(args["target"])
    elif kind == "latch":
        latch(args["target"])
    elif kind == "reset":
        reset(args["target"])
    elif kind == "copy":
        copy(args["source"], args["dest"])
    elif kind == "calc":
        source = args["source"]
        lit = args["literal"]
        op = args["op"]
        if op == "add":
            expr = source + lit
        elif op == "sub":
            expr = source - lit
        else:
            expr = source * lit
        calc(expr, args["dest"])


# ---------------------------------------------------------------------------
# Rung / Program specs
# ---------------------------------------------------------------------------


@dataclass
class RungSpec:
    conditions: list[CondSpec] = field(default_factory=list)
    instructions: list[InstrSpec] = field(default_factory=list)


@st.composite
def rung_specs(draw: st.DrawFn, pool: TagPool) -> RungSpec:
    n_conds = draw(st.integers(1, 2))
    n_instrs = draw(st.integers(1, 3))
    conditions = [draw(condition_specs(pool)) for _ in range(n_conds)]
    instructions = [draw(instruction_specs(pool)) for _ in range(n_instrs)]
    return RungSpec(conditions=conditions, instructions=instructions)


@dataclass
class ProgramSpec:
    pool: TagPool
    rungs: list[RungSpec] = field(default_factory=list)


@st.composite
def program_specs(draw: st.DrawFn) -> ProgramSpec:
    pool = draw(tag_pools())
    n_rungs = draw(st.integers(2, 8))
    rungs = [draw(rung_specs(pool)) for _ in range(n_rungs)]
    return ProgramSpec(pool=pool, rungs=rungs)


def build_program(spec: ProgramSpec) -> Program:
    with Program(strict=False) as logic:
        for rs in spec.rungs:
            conds = [build_condition(c) for c in rs.conditions]
            with Rung(*conds):
                for instr in rs.instructions:
                    emit_instruction(instr)
    return logic


# ---------------------------------------------------------------------------
# Property specs
# ---------------------------------------------------------------------------


@dataclass
class PropertySpec:
    kind: str
    tags: list[Any] = field(default_factory=list)
    bound: int | None = None


@st.composite
def property_specs(draw: st.DrawFn, pool: TagPool) -> PropertySpec:
    writable_bool = pool.writable_bool()
    numerics = pool.all_numeric()

    choices: list[tuple[str, int]] = []
    if writable_bool:
        choices.append(("always_false", 40))
        choices.append(("always_true", 20))
    if numerics:
        choices.append(("bounded", 25))
    if len(writable_bool) >= 2:
        choices.append(("mutual_exclusion", 15))

    assume(len(choices) > 0)
    kind = draw(st.sampled_from([c[0] for c in choices]))

    if kind == "always_false":
        tag = draw(st.sampled_from(writable_bool))
        return PropertySpec(kind="always_false", tags=[tag])
    elif kind == "always_true":
        tag = draw(st.sampled_from(writable_bool))
        return PropertySpec(kind="always_true", tags=[tag])
    elif kind == "bounded":
        tag = draw(st.sampled_from(numerics))
        bound = draw(st.integers(1, 200))
        return PropertySpec(kind="bounded", tags=[tag], bound=bound)
    else:
        pair = draw(st.lists(st.sampled_from(writable_bool), min_size=2, max_size=2))
        return PropertySpec(kind="mutual_exclusion", tags=pair)


def build_property(spec: PropertySpec) -> Any:
    if spec.kind == "always_false":
        return spec.tags[0] == False  # noqa: E712
    elif spec.kind == "always_true":
        return spec.tags[0] == True  # noqa: E712
    elif spec.kind == "bounded":
        return spec.tags[0] < spec.bound
    else:
        return Or(~spec.tags[0], ~spec.tags[1])
