"""Hypothesis strategies for generating fuzzer specs and building programs."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Any

import hypothesis.strategies as st
from hypothesis import assume

from pyrung.core import (
    And,
    Or,
    Program,
    Rung,
    calc,
    copy,
    count_down,
    count_up,
    fall,
    latch,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
)

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
def condition_specs(draw: st.DrawFn, pool: TagPool, *, depth: int = 0) -> CondSpec:
    conditions = pool.all_conditions()
    assume(len(conditions) > 0)

    bools = pool.all_bool()
    numerics = pool.all_numeric()
    int_or_dint = pool.int_tags + pool.dint_tags

    kinds_weights: list[tuple[str, int]] = [
        ("bit", 30),
        ("negated", 10),
        ("compare", 25 if numerics else 0),
        ("truthy", 7 if int_or_dint else 0),
        ("rise", 8 if bools else 0),
        ("fall", 5 if bools else 0),
        ("composite_and", 7 if depth < 2 else 0),
        ("composite_or", 5 if depth < 2 else 0),
    ]

    available = [k for k, w in kinds_weights if w > 0]
    if not available:
        available = ["bit", "negated"]

    kind = draw(st.sampled_from(available))

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
    elif kind == "truthy":
        tag = draw(st.sampled_from(int_or_dint))
        return CondSpec(kind="truthy", tag=tag)
    elif kind == "rise":
        tag = draw(st.sampled_from(bools))
        return CondSpec(kind="rise", tag=tag)
    elif kind == "fall":
        tag = draw(st.sampled_from(bools))
        return CondSpec(kind="fall", tag=tag)
    elif kind == "composite_and":
        c1 = draw(condition_specs(pool, depth=depth + 1))
        c2 = draw(condition_specs(pool, depth=depth + 1))
        return CondSpec(kind="composite_and", operand=(c1, c2))
    else:
        c1 = draw(condition_specs(pool, depth=depth + 1))
        c2 = draw(condition_specs(pool, depth=depth + 1))
        return CondSpec(kind="composite_or", operand=(c1, c2))


def build_condition(spec: CondSpec) -> Any:
    if spec.kind == "bit":
        return spec.tag
    elif spec.kind == "negated":
        return ~spec.tag
    elif spec.kind == "compare":
        return _COMPARE_OPS[spec.op](spec.tag, spec.operand)
    elif spec.kind == "truthy":
        return spec.tag
    elif spec.kind == "rise":
        return rise(spec.tag)
    elif spec.kind == "fall":
        return fall(spec.tag)
    elif spec.kind == "composite_and":
        c1, c2 = spec.operand
        return And(build_condition(c1), build_condition(c2))
    elif spec.kind == "composite_or":
        c1, c2 = spec.operand
        return Or(build_condition(c1), build_condition(c2))
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
    has_timers = len(pool.timers) > 0
    has_counters = len(pool.counters) > 0
    assume(has_bool or has_numeric)

    choices: list[tuple[str, int]] = []
    if has_bool:
        choices.extend([("out", 20), ("latch", 8), ("reset_bool", 8)])
    if has_numeric:
        choices.extend([("copy", 28), ("calc", 16)])
    if not has_bool and has_numeric:
        choices.append(("reset_numeric", 8))
    if has_timers:
        choices.extend([("on_delay", 8), ("off_delay", 4)])
    if has_counters:
        choices.extend([("count_up", 5), ("count_down", 3)])

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
    elif kind == "calc":
        dest = draw(st.sampled_from(writable_numeric))
        source = draw(st.sampled_from(pool.all_numeric()))
        op = draw(st.sampled_from(["add", "sub", "mul"]))
        literal = draw(int_values())
        return InstrSpec(
            kind="calc", args={"source": source, "op": op, "literal": literal, "dest": dest}
        )
    elif kind == "on_delay":
        timer = draw(st.sampled_from(pool.timers))
        use_tag_preset = has_numeric and draw(st.integers(0, 4)) == 0
        if use_tag_preset:
            preset = draw(st.sampled_from(pool.all_numeric()))
        else:
            preset = draw(timer_presets())
        has_reset = has_bool and draw(st.integers(0, 4)) == 0
        reset_tag = draw(st.sampled_from(writable_bool)) if has_reset else None
        return InstrSpec(
            kind="on_delay", args={"timer": timer, "preset": preset, "reset": reset_tag}
        )
    elif kind == "off_delay":
        timer = draw(st.sampled_from(pool.timers))
        use_tag_preset = has_numeric and draw(st.integers(0, 4)) == 0
        if use_tag_preset:
            preset = draw(st.sampled_from(pool.all_numeric()))
        else:
            preset = draw(timer_presets())
        return InstrSpec(kind="off_delay", args={"timer": timer, "preset": preset})
    elif kind == "count_up":
        counter = draw(st.sampled_from(pool.counters))
        preset = draw(counter_presets())
        assume(has_bool)
        reset_tag = draw(st.sampled_from(writable_bool))
        has_down = has_bool and draw(st.integers(0, 2)) == 0
        down_tag = draw(st.sampled_from(writable_bool)) if has_down else None
        return InstrSpec(
            kind="count_up",
            args={"counter": counter, "preset": preset, "reset": reset_tag, "down": down_tag},
        )
    else:
        counter = draw(st.sampled_from(pool.counters))
        preset = draw(counter_presets())
        assume(has_bool)
        reset_tag = draw(st.sampled_from(writable_bool))
        return InstrSpec(
            kind="count_down", args={"counter": counter, "preset": preset, "reset": reset_tag}
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
    elif kind == "on_delay":
        builder = on_delay(args["timer"], args["preset"])
        if args.get("reset") is not None:
            builder.reset(args["reset"])
    elif kind == "off_delay":
        off_delay(args["timer"], args["preset"])
    elif kind == "count_up":
        builder = count_up(args["counter"], args["preset"])
        if args.get("down") is not None:
            builder = builder.down(args["down"])
        builder.reset(args["reset"])
    elif kind == "count_down":
        count_down(args["counter"], args["preset"]).reset(args["reset"])


# ---------------------------------------------------------------------------
# Rung / Program specs
# ---------------------------------------------------------------------------


@dataclass
class RungSpec:
    conditions: list[CondSpec] = field(default_factory=list)
    instructions: list[InstrSpec] = field(default_factory=list)


_TERMINAL_KINDS = {"count_up", "count_down"}


def _is_terminal(spec: InstrSpec) -> bool:
    if spec.kind in _TERMINAL_KINDS:
        return True
    if spec.kind == "on_delay" and spec.args.get("reset") is not None:
        return True
    return False


@st.composite
def rung_specs(draw: st.DrawFn, pool: TagPool) -> RungSpec:
    n_conds = draw(st.integers(1, 2))
    n_instrs = draw(st.integers(1, 3))
    conditions = [draw(condition_specs(pool)) for _ in range(n_conds)]
    instructions = [draw(instruction_specs(pool)) for _ in range(n_instrs)]

    non_terminal = [i for i in instructions if not _is_terminal(i)]
    terminal = [i for i in instructions if _is_terminal(i)]
    if terminal:
        instructions = non_terminal + [terminal[0]]
    else:
        instructions = non_terminal if non_terminal else instructions

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

    from .patterns import TIER1_PATTERNS

    available = [p for p in TIER1_PATTERNS if p(pool) is not None]
    if available:
        n_patterns = draw(st.integers(1, min(3, len(available))))
        chosen = draw(
            st.lists(
                st.sampled_from(available), min_size=n_patterns, max_size=n_patterns, unique_by=id
            )
        )
        for pattern_fn in chosen:
            pattern_rungs = pattern_fn(pool)
            if pattern_rungs:
                pos = draw(st.integers(0, len(rungs)))
                rungs[pos:pos] = pattern_rungs

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
        choices.append(("always_false", 30))
        choices.append(("always_true", 15))
    if numerics:
        choices.append(("bounded", 20))
    if len(writable_bool) >= 2:
        choices.append(("mutual_exclusion", 10))
    if pool.timers:
        choices.append(("timer_never_fires", 15))
    if pool.counters:
        choices.append(("counter_bounded", 10))

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
    elif kind == "mutual_exclusion":
        pair = draw(st.lists(st.sampled_from(writable_bool), min_size=2, max_size=2))
        return PropertySpec(kind="mutual_exclusion", tags=pair)
    elif kind == "timer_never_fires":
        timer = draw(st.sampled_from(pool.timers))
        return PropertySpec(kind="timer_never_fires", tags=[timer.Done])
    else:
        counter = draw(st.sampled_from(pool.counters))
        bound = draw(st.integers(1, 50))
        return PropertySpec(kind="counter_bounded", tags=[counter.Acc], bound=bound)


def build_property(spec: PropertySpec) -> Any:
    if spec.kind == "always_false":
        return spec.tags[0] == False  # noqa: E712
    elif spec.kind == "always_true":
        return spec.tags[0] == True  # noqa: E712
    elif spec.kind == "bounded":
        return spec.tags[0] < spec.bound
    elif spec.kind == "mutual_exclusion":
        return Or(~spec.tags[0], ~spec.tags[1])
    elif spec.kind == "timer_never_fires":
        return spec.tags[0] == False  # noqa: E712
    elif spec.kind == "counter_bounded":
        return spec.tags[0] < spec.bound
    else:
        return spec.tags[0] == False  # noqa: E712
