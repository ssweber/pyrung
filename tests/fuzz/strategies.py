"""Hypothesis strategies for generating fuzzer specs and building programs."""

from __future__ import annotations

import operator
import os
from dataclasses import dataclass, field
from typing import Any

import hypothesis.strategies as st
from hypothesis import assume

from pyrung.core import (
    And,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    blockcopy,
    branch,
    calc,
    call,
    copy,
    count_down,
    count_up,
    event_drum,
    fall,
    fill,
    forloop,
    latch,
    lro,
    lsh,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    receive,
    reset,
    return_early,
    rise,
    rro,
    rsh,
    search,
    shift,
    subroutine,
    time_drum,
    to_ascii,
    to_binary,
    to_text,
    to_value,
    unpack_to_bits,
    unpack_to_words,
)

from .pool import TagPool, tag_pools

_drum_counter = 0


def _next_drum_id() -> int:
    global _drum_counter  # noqa: PLW0603
    _drum_counter += 1
    return _drum_counter


# How strongly program_specs() biases pattern selection toward adjacency
# patterns (each adjacency pattern is entered into the selection pool this many
# times).  Override via FUZZ_ADJACENCY_WEIGHT for CI tuning; 1 disables the bias.
_ADJACENCY_PATTERN_WEIGHT = max(1, int(os.environ.get("FUZZ_ADJACENCY_WEIGHT", "5")))


# ---------------------------------------------------------------------------
# Value strategies
# ---------------------------------------------------------------------------


def int_values() -> st.SearchStrategy[int]:
    boundary = st.sampled_from([0, 1, -1, 10, 100, 32767, -32768, 32768, 65535])
    return st.one_of(boundary, boundary, st.integers(-100, 100))


def timer_presets() -> st.SearchStrategy[int]:
    boundary = st.sampled_from([0, 1, 10, 50, 100, 32767])
    return st.one_of(boundary, boundary, st.integers(0, 100))


def counter_presets() -> st.SearchStrategy[int]:
    boundary = st.sampled_from([0, 1, 5, 10])
    return st.one_of(boundary, boundary, st.integers(0, 10))


def real_values() -> st.SearchStrategy[float]:
    boundary = st.sampled_from([0.0, 1.0, -1.0, 0.5, 3.14, -32768.0, 32767.0])
    return st.one_of(
        boundary, boundary, st.floats(-100, 100, allow_nan=False, allow_infinity=False)
    )


def timer_units() -> st.SearchStrategy[str]:
    return st.sampled_from(
        [
            "ms",
            "sec",
            "min",
            "hour",
            "day",
            "Tms",
            "Ts",
            "Tm",
            "Th",
            "Td",
        ]
    )


def forloop_counts() -> st.SearchStrategy[int]:
    boundary = st.sampled_from([1, 5])
    return st.one_of(boundary, boundary, st.integers(1, 5))


def char_values() -> st.SearchStrategy[str]:
    return st.sampled_from(["a", "b", "g", "y", "0", "1", "A", "Z"])


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
    chars = pool.all_char()
    int_or_dint = pool.int_tags + pool.dint_tags

    kinds_weights: list[tuple[str, int]] = [
        ("bit", 30),
        ("negated", 10),
        ("compare", 25 if numerics else 0),
        ("compare_char", 5 if chars else 0),
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
    elif kind == "compare_char":
        tag = draw(st.sampled_from(chars))
        op = draw(st.sampled_from(["==", "!="]))
        value = draw(char_values())
        return CondSpec(kind="compare", tag=tag, op=op, operand=value)
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


def _block_range_args(draw: st.DrawFn, block: Any) -> dict[str, int]:
    start = draw(st.integers(block.start, block.end))
    end = draw(st.integers(start, block.end))
    return {"start": start, "end": end}


def _pick_source(draw: st.DrawFn, choices: list[Any], recent_dests: list[Any] | None) -> Any:
    """Pick a numeric source tag, biased toward a recently written dest.

    Reusing a recent dest as a source builds copy/calc chains organically across
    rungs — the feature adjacency the prover fuzzer most often finds bugs in.
    """
    # Identity membership: Tag.__eq__ is DSL-overloaded (builds an expression),
    # so `in` / `==` cannot be used to test pool membership.
    usable = [t for t in (recent_dests or []) if any(t is c for c in choices)]
    if usable and draw(st.integers(0, 1)) == 0:
        return draw(st.sampled_from(usable))
    return draw(st.sampled_from(choices))


@st.composite
def instruction_specs(
    draw: st.DrawFn,
    pool: TagPool,
    *,
    soundness_only: bool = False,
    recent_dests: list[Any] | None = None,
) -> InstrSpec:
    writable_bool = pool.writable_bool()
    writable_numeric = pool.writable_numeric()
    has_bool = len(writable_bool) > 0
    has_numeric = len(writable_numeric) > 0
    has_timers = len(pool.timers) > 0
    has_counters = len(pool.counters) > 0
    has_int_block = pool.int_block is not None
    has_bool_block = pool.bool_block is not None
    has_dint = len(pool.dint_tags) > 0
    has_int_or_word = len(pool.int_tags + pool.word_tags) > 0
    assume(has_bool or has_numeric)

    has_char = len(pool.char_tags) > 0
    has_real = len(pool.real_tags) > 0

    choices: list[tuple[str, int]] = []
    if has_bool:
        choices.extend([("out", 12), ("out_oneshot", 3), ("latch", 6), ("reset_bool", 6)])
    if has_numeric:
        choices.extend([("copy", 15), ("copy_oneshot", 4), ("calc", 8), ("calc_tag_tag", 4)])
    if has_numeric and (pool.word_tags or pool.int_tags):
        choices.append(("calc_shift", 3))
    if not has_bool and has_numeric:
        choices.append(("reset_numeric", 6))
    if has_char:
        choices.append(("copy_char", 4))
    if has_numeric and has_real:
        choices.append(("copy_float", 3))
    if has_char and has_numeric:
        choices.extend(
            [
                ("copy_to_value", 2),
                ("copy_to_ascii", 2),
                ("copy_to_binary", 2),
                ("copy_to_text", 2),
            ]
        )
    if has_timers:
        choices.extend([("on_delay", 6), ("off_delay", 3)])
    if has_counters:
        choices.extend([("count_up", 4), ("count_down", 2)])
    if has_int_block:
        choices.extend(
            [("fill", 3), ("fill_oneshot", 1), ("blockcopy", 2), ("blockcopy_oneshot", 1)]
        )
    if has_int_block and has_numeric:
        choices.extend([("indirect_copy", 4), ("range_sum_calc", 2)])
    if has_int_block and (pool.int_tags or pool.dint_tags) and has_bool:
        choices.append(("search", 2))
    if has_bool_block and has_bool:
        choices.append(("shift", 2))
    if has_bool_block and has_int_or_word:
        choices.extend([("pack_bits", 2), ("unpack_to_bits", 2)])
    if (
        has_int_block
        and has_dint
        and pool.int_block is not None
        and pool.int_block.end >= pool.int_block.start + 1
    ):
        choices.extend([("pack_words", 2), ("unpack_to_words", 2)])
    if len(writable_bool) >= 3 and pool.int_tags:
        choices.append(("event_drum", 2))
    if len(writable_bool) >= 3 and len(pool.int_tags) >= 2:
        choices.append(("time_drum", 2))
    if pool.char_block is not None and has_numeric:
        choices.append(("pack_text", 2))
    if soundness_only and has_numeric and has_bool and pool.int_tags:
        choices.append(("receive", 3))

    kinds = [c[0] for c in choices]
    kind = draw(st.sampled_from(kinds))

    if kind in ("out", "out_oneshot"):
        target = draw(st.sampled_from(writable_bool))
        oneshot = kind == "out_oneshot"
        return InstrSpec(kind="out", args={"target": target, "oneshot": oneshot})
    elif kind == "latch":
        target = draw(st.sampled_from(writable_bool))
        return InstrSpec(kind="latch", args={"target": target})
    elif kind == "reset_bool":
        target = draw(st.sampled_from(writable_bool))
        return InstrSpec(kind="reset", args={"target": target})
    elif kind == "reset_numeric":
        target = draw(st.sampled_from(writable_numeric))
        return InstrSpec(kind="reset", args={"target": target})
    elif kind in ("copy", "copy_oneshot"):
        dest = draw(st.sampled_from(writable_numeric))
        use_literal = draw(st.booleans())
        if use_literal or not writable_numeric:
            source = draw(int_values())
        else:
            source = _pick_source(draw, pool.all_numeric(), recent_dests)
        oneshot = kind == "copy_oneshot"
        return InstrSpec(kind="copy", args={"source": source, "dest": dest, "oneshot": oneshot})
    elif kind == "copy_char":
        dest = draw(st.sampled_from(pool.char_tags))
        source = draw(char_values())
        return InstrSpec(kind="copy", args={"source": source, "dest": dest, "oneshot": False})
    elif kind == "copy_float":
        dest = draw(st.sampled_from(pool.real_tags))
        source = draw(real_values())
        return InstrSpec(kind="copy", args={"source": source, "dest": dest, "oneshot": False})
    elif kind == "copy_to_value":
        source = draw(st.sampled_from(pool.char_tags))
        dest = draw(st.sampled_from(writable_numeric))
        return InstrSpec(
            kind="copy_convert",
            args={"source": source, "dest": dest, "converter": "to_value"},
        )
    elif kind == "copy_to_ascii":
        source = draw(st.sampled_from(pool.char_tags))
        dest = draw(st.sampled_from(writable_numeric))
        return InstrSpec(
            kind="copy_convert",
            args={"source": source, "dest": dest, "converter": "to_ascii"},
        )
    elif kind == "copy_to_binary":
        source = draw(st.sampled_from(pool.all_numeric()))
        dest = draw(st.sampled_from(pool.char_tags))
        return InstrSpec(
            kind="copy_convert",
            args={"source": source, "dest": dest, "converter": "to_binary"},
        )
    elif kind == "copy_to_text":
        source = draw(st.sampled_from(pool.all_numeric()))
        dest = draw(st.sampled_from(pool.char_tags))
        suppress_zero = draw(st.booleans())
        termination_code = draw(st.sampled_from([None, 0, 13]))
        return InstrSpec(
            kind="copy_convert",
            args={
                "source": source,
                "dest": dest,
                "converter": "to_text",
                "suppress_zero": suppress_zero,
                "termination_code": termination_code,
            },
        )
    elif kind == "calc":
        dest = draw(st.sampled_from(writable_numeric))
        source = _pick_source(draw, pool.all_numeric(), recent_dests)
        op = draw(st.sampled_from(["add", "sub", "mul", "mul", "floordiv", "mod", "pow"]))
        if op == "mul":
            literal = draw(st.one_of(st.sampled_from([0, 1, -1, 2]), int_values()))
        elif op == "mod":
            literal = draw(
                st.one_of(st.sampled_from([1, 2, 3, 10]), int_values().filter(lambda x: x != 0))
            )
        elif op == "pow":
            literal = draw(st.sampled_from([0, 1, 2, 3]))
        elif op == "floordiv":
            literal = draw(st.one_of(st.sampled_from([1, 2, -1, 0]), int_values()))
        else:
            literal = draw(int_values())
        return InstrSpec(
            kind="calc",
            args={"source": source, "op": op, "literal": literal, "dest": dest},
        )
    elif kind == "calc_tag_tag":
        dest = draw(st.sampled_from(writable_numeric))
        all_nums = pool.all_numeric()
        source1 = _pick_source(draw, all_nums, recent_dests)
        source2 = draw(st.sampled_from(all_nums))
        op = draw(st.sampled_from(["add", "sub", "mul", "mod", "bitand", "bitor", "bitxor"]))
        return InstrSpec(
            kind="calc_tag_tag",
            args={"source1": source1, "source2": source2, "op": op, "dest": dest},
        )
    elif kind == "calc_shift":
        dest = draw(st.sampled_from(writable_numeric))
        source = draw(st.sampled_from(pool.word_tags + pool.int_tags))
        shift_op = draw(st.sampled_from(["lsh", "rsh", "lro", "rro"]))
        count = draw(st.sampled_from([0, 1, 2, 4, 8, 15]))
        return InstrSpec(
            kind="calc_shift",
            args={"source": source, "shift_op": shift_op, "count": count, "dest": dest},
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
        unit = draw(timer_units()) if draw(st.integers(0, 3)) == 0 else "ms"
        return InstrSpec(
            kind="on_delay",
            args={"timer": timer, "preset": preset, "reset": reset_tag, "unit": unit},
        )
    elif kind == "off_delay":
        timer = draw(st.sampled_from(pool.timers))
        use_tag_preset = has_numeric and draw(st.integers(0, 4)) == 0
        if use_tag_preset:
            preset = draw(st.sampled_from(pool.all_numeric()))
        else:
            preset = draw(timer_presets())
        unit = draw(timer_units()) if draw(st.integers(0, 3)) == 0 else "ms"
        return InstrSpec(kind="off_delay", args={"timer": timer, "preset": preset, "unit": unit})
    elif kind == "count_up":
        counter = draw(st.sampled_from(pool.counters))
        preset = draw(counter_presets())
        assume(has_bool)
        reset_tag = draw(st.sampled_from(writable_bool))
        has_down = has_bool and draw(st.integers(0, 2)) == 0
        down_tag = draw(st.sampled_from(writable_bool)) if has_down else None
        return InstrSpec(
            kind="count_up",
            args={
                "counter": counter,
                "preset": preset,
                "reset": reset_tag,
                "down": down_tag,
            },
        )
    elif kind == "count_down":
        counter = draw(st.sampled_from(pool.counters))
        preset = draw(counter_presets())
        assume(has_bool)
        reset_tag = draw(st.sampled_from(writable_bool))
        return InstrSpec(
            kind="count_down",
            args={"counter": counter, "preset": preset, "reset": reset_tag},
        )
    elif kind in ("fill", "fill_oneshot"):
        blk = pool.int_block
        r = _block_range_args(draw, blk)
        value = draw(int_values())
        oneshot = kind == "fill_oneshot"
        return InstrSpec(
            kind="fill",
            args={
                "block": blk,
                "value": value,
                "start": r["start"],
                "end": r["end"],
                "oneshot": oneshot,
            },
        )
    elif kind in ("blockcopy", "blockcopy_oneshot"):
        blk = pool.int_block
        length = draw(st.integers(1, min(3, blk.end - blk.start + 1)))
        src_start = draw(st.integers(blk.start, blk.end - length + 1))
        dst_start = draw(st.integers(blk.start, blk.end - length + 1))
        oneshot = kind == "blockcopy_oneshot"
        return InstrSpec(
            kind="blockcopy",
            args={
                "block": blk,
                "src_start": src_start,
                "src_end": src_start + length - 1,
                "dst_start": dst_start,
                "dst_end": dst_start + length - 1,
                "oneshot": oneshot,
            },
        )
    elif kind == "search":
        blk = pool.int_block
        r = _block_range_args(draw, blk)
        op = draw(st.sampled_from(list(_COMPARE_OPS.keys())))
        value = draw(int_values())
        result_tag = draw(st.sampled_from(pool.int_tags + pool.dint_tags))
        found_tag = draw(st.sampled_from(writable_bool))
        return InstrSpec(
            kind="search",
            args={
                "block": blk,
                "start": r["start"],
                "end": r["end"],
                "op": op,
                "value": value,
                "result": result_tag,
                "found": found_tag,
            },
        )
    elif kind == "shift":
        blk = pool.bool_block
        r = _block_range_args(draw, blk)
        clock_tag = draw(st.sampled_from(writable_bool))
        reset_tag = draw(st.sampled_from(writable_bool))
        return InstrSpec(
            kind="shift",
            args={
                "block": blk,
                "start": r["start"],
                "end": r["end"],
                "clock": clock_tag,
                "reset": reset_tag,
            },
        )
    elif kind == "pack_bits":
        blk = pool.bool_block
        dest = draw(st.sampled_from(pool.int_tags + pool.word_tags))
        return InstrSpec(
            kind="pack_bits",
            args={
                "block": blk,
                "start": blk.start,
                "end": min(blk.start + 7, blk.end),
                "dest": dest,
            },
        )
    elif kind == "unpack_to_bits":
        blk = pool.bool_block
        source = draw(st.sampled_from(pool.int_tags + pool.word_tags))
        return InstrSpec(
            kind="unpack_to_bits",
            args={
                "block": blk,
                "start": blk.start,
                "end": min(blk.start + 7, blk.end),
                "source": source,
            },
        )
    elif kind == "pack_words":
        blk = pool.int_block
        start = draw(st.integers(blk.start, blk.end - 1))
        dest = draw(st.sampled_from(pool.dint_tags))
        return InstrSpec(
            kind="pack_words",
            args={"block": blk, "start": start, "end": start + 1, "dest": dest},
        )
    elif kind == "unpack_to_words":
        blk = pool.int_block
        start = draw(st.integers(blk.start, blk.end - 1))
        source = draw(st.sampled_from(pool.dint_tags))
        return InstrSpec(
            kind="unpack_to_words",
            args={"block": blk, "start": start, "end": start + 1, "source": source},
        )
    elif kind == "indirect_copy":
        blk = pool.int_block
        ptr = draw(st.sampled_from(pool.int_tags)) if pool.int_tags else blk[blk.start]
        use_offset = draw(st.booleans())
        offset = draw(st.integers(0, 2)) if use_offset else 0
        is_source = draw(st.booleans())
        if is_source:
            dest = draw(st.sampled_from(writable_numeric))
            return InstrSpec(
                kind="indirect_copy",
                args={
                    "block": blk,
                    "ptr": ptr,
                    "offset": offset,
                    "dest": dest,
                    "is_source": True,
                },
            )
        else:
            source = draw(int_values())
            return InstrSpec(
                kind="indirect_copy",
                args={
                    "block": blk,
                    "ptr": ptr,
                    "offset": offset,
                    "source": source,
                    "is_source": False,
                },
            )
    elif kind == "range_sum_calc":
        blk = pool.int_block
        r = _block_range_args(draw, blk)
        dest = draw(st.sampled_from(writable_numeric))
        return InstrSpec(
            kind="range_sum_calc",
            args={"block": blk, "start": r["start"], "end": r["end"], "dest": dest},
        )
    elif kind == "event_drum":
        did = _next_drum_id()
        n_outputs = draw(st.integers(1, min(3, len(writable_bool))))
        n_steps = draw(st.integers(2, 4))
        outputs = draw(
            st.lists(
                st.sampled_from(writable_bool),
                min_size=n_outputs,
                max_size=n_outputs,
                unique_by=lambda t: t.name,
            )
        )
        events = draw(
            st.lists(st.sampled_from(pool.all_conditions()), min_size=n_steps, max_size=n_steps)
        )
        pattern = [[draw(st.booleans()) for _ in range(n_outputs)] for _ in range(n_steps)]
        step_tag = Int(f"DrumStep{did}")
        done_tag = Bool(f"DrumDone{did}")
        reset_cond = draw(st.sampled_from(writable_bool))
        has_jog = draw(st.booleans())
        jog_cond = draw(st.sampled_from(writable_bool)) if has_jog else None
        has_jump = draw(st.booleans())
        jump_cond = draw(st.sampled_from(writable_bool)) if has_jump else None
        jump_step = draw(st.integers(1, n_steps)) if has_jump else None
        return InstrSpec(
            kind="event_drum",
            args={
                "outputs": outputs,
                "events": events,
                "pattern": pattern,
                "step": step_tag,
                "done": done_tag,
                "reset": reset_cond,
                "jog": jog_cond,
                "jump": jump_cond,
                "jump_step": jump_step,
            },
        )
    elif kind == "time_drum":
        did = _next_drum_id()
        n_outputs = draw(st.integers(1, min(3, len(writable_bool))))
        n_steps = draw(st.integers(2, 4))
        outputs = draw(
            st.lists(
                st.sampled_from(writable_bool),
                min_size=n_outputs,
                max_size=n_outputs,
                unique_by=lambda t: t.name,
            )
        )
        presets = [draw(timer_presets()) for _ in range(n_steps)]
        pattern = [[draw(st.booleans()) for _ in range(n_outputs)] for _ in range(n_steps)]
        step_tag = Int(f"DrumStep{did}")
        acc_tag = Int(f"DrumAcc{did}")
        done_tag = Bool(f"DrumDone{did}")
        reset_cond = draw(st.sampled_from(writable_bool))
        unit = draw(timer_units()) if draw(st.integers(0, 3)) == 0 else "ms"
        has_jog = draw(st.booleans())
        jog_cond = draw(st.sampled_from(writable_bool)) if has_jog else None
        has_jump = draw(st.booleans())
        jump_cond = draw(st.sampled_from(writable_bool)) if has_jump else None
        jump_step = draw(st.integers(1, n_steps)) if has_jump else None
        return InstrSpec(
            kind="time_drum",
            args={
                "outputs": outputs,
                "presets": presets,
                "unit": unit,
                "pattern": pattern,
                "step": step_tag,
                "acc": acc_tag,
                "done": done_tag,
                "reset": reset_cond,
                "jog": jog_cond,
                "jump": jump_cond,
                "jump_step": jump_step,
            },
        )
    elif kind == "receive":
        rid = _next_drum_id()
        dest = draw(st.sampled_from(writable_numeric))
        receiving = Bool(f"RxBusy{rid}")
        success = Bool(f"RxOK{rid}")
        error = Bool(f"RxErr{rid}")
        ex_code = Int(f"RxCode{rid}")
        return InstrSpec(
            kind="receive",
            args={
                "target": "device1",
                "remote_start": "DS1",
                "dest": dest,
                "receiving": receiving,
                "success": success,
                "error": error,
                "exception_response": ex_code,
            },
        )
    elif kind == "pack_text":
        blk = pool.char_block
        r = _block_range_args(draw, blk)
        dest = draw(st.sampled_from(writable_numeric))
        allow_ws = draw(st.booleans())
        return InstrSpec(
            kind="pack_text",
            args={
                "block": blk,
                "start": r["start"],
                "end": r["end"],
                "dest": dest,
                "allow_whitespace": allow_ws,
            },
        )
    raise AssertionError(f"unknown instruction kind: {kind}")


_SHIFT_FNS = {"lsh": lsh, "rsh": rsh, "lro": lro, "rro": rro}

_CALC_TAG_TAG_OPS = {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "mod": lambda a, b: a % b,
    "bitand": lambda a, b: a & b,
    "bitor": lambda a, b: a | b,
    "bitxor": lambda a, b: a ^ b,
}


def emit_instruction(spec: InstrSpec) -> None:
    kind = spec.kind
    args = spec.args
    if kind == "out":
        out(args["target"], oneshot=args.get("oneshot", False))
    elif kind == "latch":
        latch(args["target"])
    elif kind == "reset":
        reset(args["target"])
    elif kind == "copy":
        copy(args["source"], args["dest"], oneshot=args.get("oneshot", False))
    elif kind == "calc":
        source = args["source"]
        lit = args["literal"]
        op = args["op"]
        if op == "add":
            expr = source + lit
        elif op == "sub":
            expr = source - lit
        elif op == "mul":
            expr = source * lit
        elif op == "floordiv":
            expr = source // lit
        elif op == "mod":
            expr = source % lit
        elif op == "pow":
            expr = source**lit
        else:
            expr = source + lit
        calc(expr, args["dest"])
    elif kind == "calc_tag_tag":
        s1, s2 = args["source1"], args["source2"]
        expr = _CALC_TAG_TAG_OPS[args["op"]](s1, s2)
        calc(expr, args["dest"])
    elif kind == "calc_shift":
        fn = _SHIFT_FNS[args["shift_op"]]
        calc(fn(args["source"], args["count"]), args["dest"])
    elif kind == "on_delay":
        builder = on_delay(args["timer"], args["preset"], unit=args.get("unit", "ms"))
        if args.get("reset") is not None:
            builder.reset(args["reset"])
    elif kind == "off_delay":
        off_delay(args["timer"], args["preset"], unit=args.get("unit", "ms"))
    elif kind == "count_up":
        builder = count_up(args["counter"], args["preset"])
        if args.get("down") is not None:
            builder = builder.down(args["down"])
        builder.reset(args["reset"])
    elif kind == "count_down":
        count_down(args["counter"], args["preset"]).reset(args["reset"])
    elif kind == "fill":
        fill(
            args["value"],
            args["block"].select(args["start"], args["end"]),
            oneshot=args.get("oneshot", False),
        )
    elif kind == "blockcopy":
        blk = args["block"]
        blockcopy(
            blk.select(args["src_start"], args["src_end"]),
            blk.select(args["dst_start"], args["dst_end"]),
            oneshot=args.get("oneshot", False),
        )
    elif kind == "search":
        blk = args["block"]
        comparison = _COMPARE_OPS[args["op"]](blk.select(args["start"], args["end"]), args["value"])
        search(comparison, result=args["result"], found=args["found"])
    elif kind == "shift":
        blk = args["block"]
        shift(blk.select(args["start"], args["end"])).clock(args["clock"]).reset(args["reset"])
    elif kind == "pack_bits":
        blk = args["block"]
        pack_bits(blk.select(args["start"], args["end"]), args["dest"])
    elif kind == "unpack_to_bits":
        blk = args["block"]
        unpack_to_bits(args["source"], blk.select(args["start"], args["end"]))
    elif kind == "pack_words":
        blk = args["block"]
        pack_words(blk.select(args["start"], args["end"]), args["dest"])
    elif kind == "unpack_to_words":
        blk = args["block"]
        unpack_to_words(args["source"], blk.select(args["start"], args["end"]))
    elif kind == "indirect_copy":
        blk = args["block"]
        ref = blk[args["ptr"] + args["offset"]] if args["offset"] else blk[args["ptr"]]
        if args["is_source"]:
            copy(ref, args["dest"])
        else:
            copy(args["source"], ref)
    elif kind == "copy_convert":
        converter_name = args["converter"]
        if converter_name == "to_value":
            converter = to_value
        elif converter_name == "to_ascii":
            converter = to_ascii
        elif converter_name == "to_binary":
            converter = to_binary
        else:
            converter = to_text(
                suppress_zero=args.get("suppress_zero", True),
                termination_code=args.get("termination_code"),
            )
        copy(args["source"], args["dest"], convert=converter)
    elif kind == "receive":
        receive(
            target=args["target"],
            remote_start=args["remote_start"],
            dest=args["dest"],
            receiving=args["receiving"],
            success=args["success"],
            error=args["error"],
            exception_response=args["exception_response"],
        )
    elif kind == "pack_text":
        blk = args["block"]
        pack_text(
            blk.select(args["start"], args["end"]),
            args["dest"],
            allow_whitespace=args.get("allow_whitespace", False),
        )
    elif kind == "return_early":
        return_early()
    elif kind == "call":
        call(args["name"])
    elif kind == "event_drum":
        builder = event_drum(
            outputs=args["outputs"],
            events=args["events"],
            pattern=args["pattern"],
            current_step=args["step"],
            completion_flag=args["done"],
        ).reset(args["reset"])
        if args.get("jump") is not None:
            builder = builder.jump(args["jump"], step=args["jump_step"])
        if args.get("jog") is not None:
            builder.jog(args["jog"])
    elif kind == "time_drum":
        builder = time_drum(
            outputs=args["outputs"],
            presets=args["presets"],
            unit=args.get("unit", "ms"),
            pattern=args["pattern"],
            current_step=args["step"],
            accumulator=args["acc"],
            completion_flag=args["done"],
        ).reset(args["reset"])
        if args.get("jump") is not None:
            builder = builder.jump(args["jump"], step=args["jump_step"])
        if args.get("jog") is not None:
            builder.jog(args["jog"])
    elif kind == "range_sum_calc":
        blk = args["block"]
        calc(blk.select(args["start"], args["end"]).sum(), args["dest"])


# ---------------------------------------------------------------------------
# Rung / Program specs
# ---------------------------------------------------------------------------


@dataclass
class ForLoopSpec:
    count: Any  # int literal or Tag
    oneshot: bool = False


@dataclass
class BranchSpec:
    conditions: list[CondSpec] = field(default_factory=list)
    instructions: list[InstrSpec] = field(default_factory=list)


@dataclass
class SubroutineSpec:
    name: str
    rungs: list[RungSpec] = field(default_factory=list)


@dataclass
class RungSpec:
    conditions: list[CondSpec] = field(default_factory=list)
    instructions: list[InstrSpec] = field(default_factory=list)
    branches: list[BranchSpec] = field(default_factory=list)
    forloop: ForLoopSpec | None = None


_TERMINAL_KINDS = {"count_up", "count_down", "shift", "event_drum", "time_drum"}


def _is_terminal(spec: InstrSpec) -> bool:
    if spec.kind in _TERMINAL_KINDS:
        return True
    if spec.kind == "on_delay" and spec.args.get("reset") is not None:
        return True
    return False


_EXCLUSIVE_SPEC_EXTRACTORS: dict[str, tuple[str, str, str]] = {
    "on_delay": ("timer", "timer", "Acc"),
    "off_delay": ("timer", "timer", "Acc"),
    "count_up": ("counter", "counter", "Acc"),
    "count_down": ("counter", "counter", "Acc"),
}


def _exclusive_resource_key(spec: InstrSpec) -> tuple[str, str] | None:
    """Derive the exclusive resource key from an InstrSpec.

    Mirrors Instruction._exclusive_fields / exclusive_resources() but operates
    on the spec-level args dict (UDT instances) rather than instruction fields
    (destructured tags).
    """
    entry = _EXCLUSIVE_SPEC_EXTRACTORS.get(spec.kind)
    if entry is None:
        return None
    resource_type, arg_key, field = entry
    udt = spec.args[arg_key]
    return (resource_type, getattr(udt, field).name)


def _exclusive_owners_are_unique(rungs: list[RungSpec]) -> bool:
    seen: set[tuple[str, str]] = set()
    for rung in rungs:
        for instr in rung.instructions:
            key = _exclusive_resource_key(instr)
            if key is None:
                continue
            if key in seen:
                return False
            seen.add(key)
    return True


_CHAIN_DEST_KINDS = {"copy", "calc", "calc_tag_tag", "calc_shift"}


def _chain_dests(rung: RungSpec) -> list[Any]:
    """Numeric dest tags written by a rung's chainable instructions."""
    dests: list[Any] = []
    instr_groups = [rung.instructions, *(b.instructions for b in rung.branches)]
    for instrs in instr_groups:
        for instr in instrs:
            if instr.kind in _CHAIN_DEST_KINDS:
                dest = instr.args.get("dest")
                if dest is not None and hasattr(dest, "name"):
                    dests.append(dest)
    return dests


@st.composite
def rung_specs(
    draw: st.DrawFn,
    pool: TagPool,
    *,
    soundness_only: bool = False,
    min_conditions: int = 0,
    recent_dests: list[Any] | None = None,
) -> RungSpec:
    n_conds = draw(st.integers(min_conditions, 2))
    n_instrs = draw(st.integers(1, 3))
    conditions = [draw(condition_specs(pool)) for _ in range(n_conds)]
    instructions = [
        draw(instruction_specs(pool, soundness_only=soundness_only, recent_dests=recent_dests))
        for _ in range(n_instrs)
    ]

    non_terminal = [i for i in instructions if not _is_terminal(i)]
    terminal = [i for i in instructions if _is_terminal(i)]
    if terminal:
        instructions = non_terminal + [terminal[0]]
    else:
        instructions = non_terminal if non_terminal else instructions

    fl: ForLoopSpec | None = None
    if non_terminal and not terminal and draw(st.integers(0, 9)) == 0:
        use_tag = pool.int_tags and draw(st.integers(0, 3)) == 0
        if use_tag:
            count: Any = draw(st.sampled_from(pool.int_tags))
        else:
            count = draw(forloop_counts())
        fl = ForLoopSpec(count=count, oneshot=draw(st.booleans()))

    return RungSpec(conditions=conditions, instructions=instructions, forloop=fl)


@dataclass
class ProgramSpec:
    pool: TagPool
    rungs: list[RungSpec] = field(default_factory=list)
    subroutines: list[SubroutineSpec] = field(default_factory=list)


@st.composite
def program_specs(
    draw: st.DrawFn, *, soundness_only: bool = False, min_conditions: int = 0
) -> ProgramSpec:
    pool = draw(tag_pools())
    n_rungs = draw(st.integers(2, 8))
    rungs: list[RungSpec] = []
    recent_dests: list[Any] = []
    for _ in range(n_rungs):
        rung = draw(
            rung_specs(
                pool,
                soundness_only=soundness_only,
                min_conditions=min_conditions,
                recent_dests=recent_dests,
            )
        )
        rungs.append(rung)
        recent_dests = (recent_dests + _chain_dests(rung))[-6:]

    from .patterns import ADJACENCY_PATTERNS, TIER1_PATTERNS, PatternResult

    available = [p for p in TIER1_PATTERNS if p(pool) is not None]
    subs: list[SubroutineSpec] = []
    if available:
        # Weight adjacency patterns up so the fuzzer spends most of its budget
        # in the feature-interaction corners where prover soundness bugs live,
        # while incidental patterns keep weight 1 to preserve diversity.
        weighted = [
            p
            for p in available
            for _ in range(_ADJACENCY_PATTERN_WEIGHT if p in ADJACENCY_PATTERNS else 1)
        ]
        n_patterns = draw(st.integers(1, min(4, len(available))))
        chosen = draw(
            st.lists(
                st.sampled_from(weighted), min_size=n_patterns, max_size=n_patterns, unique_by=id
            )
        )
        for pattern_fn in chosen:
            result = pattern_fn(pool)
            if result is None:
                continue
            if isinstance(result, PatternResult):
                pattern_rungs = result.rungs
                subs.extend(result.subroutines)
            else:
                pattern_rungs = result
            if pattern_rungs:
                pos = draw(st.integers(0, len(rungs)))
                rungs[pos:pos] = pattern_rungs
    if pool.writable_bool() and draw(st.integers(0, 4)) == 0:
        n_subs = draw(st.integers(1, 2))
        for si in range(n_subs):
            sub_name = f"sub_{si}"
            n_sub_rungs = draw(st.integers(1, 3))
            sub_rungs = [draw(rung_specs(pool)) for _ in range(n_sub_rungs)]
            if draw(st.integers(0, 2)) == 0:
                guard_cond = draw(condition_specs(pool))
                early_rung = RungSpec(
                    conditions=[guard_cond],
                    instructions=[InstrSpec(kind="return_early", args={})],
                )
                sub_rungs.insert(0, early_rung)
            subs.append(SubroutineSpec(name=sub_name, rungs=sub_rungs))
            cond = draw(condition_specs(pool))
            call_rung = RungSpec(
                conditions=[cond],
                instructions=[InstrSpec(kind="call", args={"name": sub_name})],
            )
            pos = draw(st.integers(0, len(rungs)))
            rungs.insert(pos, call_rung)

    all_rungs = rungs
    for sub in subs:
        all_rungs = all_rungs + sub.rungs
    assume(_exclusive_owners_are_unique(all_rungs))
    return ProgramSpec(pool=pool, rungs=rungs, subroutines=subs)


def _emit_rung_body(rs: RungSpec) -> None:
    if rs.forloop is not None:
        with forloop(rs.forloop.count, oneshot=rs.forloop.oneshot):
            for instr in rs.instructions:
                emit_instruction(instr)
    else:
        for instr in rs.instructions:
            emit_instruction(instr)
        for bs in rs.branches:
            branch_conds = [build_condition(c) for c in bs.conditions]
            with branch(*branch_conds):
                for instr in bs.instructions:
                    emit_instruction(instr)


def build_program(spec: ProgramSpec) -> Program:
    with Program(strict=False) as logic:
        for rs in spec.rungs:
            conds = [build_condition(c) for c in rs.conditions]
            with Rung(*conds):
                _emit_rung_body(rs)
        for sub in spec.subroutines:
            with subroutine(sub.name):
                for rs in sub.rungs:
                    conds = [build_condition(c) for c in rs.conditions]
                    with Rung(*conds):
                        _emit_rung_body(rs)
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
