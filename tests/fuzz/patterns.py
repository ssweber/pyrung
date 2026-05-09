"""Tier 1 wiring pattern templates for structured program generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .strategies import CondSpec, InstrSpec, RungSpec

if TYPE_CHECKING:
    from .pool import TagPool


def timer_acc_downstream_compare(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #1: on_delay(T, preset) + Rung(T.Acc >= K)."""
    if not pool.timers:
        return None
    timer = pool.timers[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(kind="on_delay", args={"timer": timer, "preset": 50, "reset": None})
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=timer.Acc, op=">=", operand=10)],
            instructions=[_default_output(pool)],
        ),
    ]


def copy_chain_into_compare(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #2: copy(T.Acc, numeric) + Rung(numeric >= K)."""
    if not pool.timers or not pool.writable_numeric():
        return None
    timer = pool.timers[0]
    dest = pool.writable_numeric()[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(kind="on_delay", args={"timer": timer, "preset": 100, "reset": None}),
                InstrSpec(kind="copy", args={"source": timer.Acc, "dest": dest}),
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=dest, op=">=", operand=25)],
            instructions=[_default_output(pool)],
        ),
    ]


def conditional_write_edge_read(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #3: Rung(A): out(B) + Rung(rise(B)) — cross-scan edge dependency."""
    if len(pool.all_bool()) < 1 or not pool.writable_bool():
        return None
    cond_tag = pool.all_bool()[0]
    target = pool.writable_bool()[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=cond_tag)],
            instructions=[InstrSpec(kind="out", args={"target": target})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="rise", tag=target)],
            instructions=[_default_output(pool)],
        ),
    ]


def dynamic_preset(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #4: copy(src, preset_tag) + on_delay(T, preset_tag)."""
    if not pool.timers or not pool.writable_numeric():
        return None
    timer = pool.timers[0]
    preset_tag = pool.writable_numeric()[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[InstrSpec(kind="copy", args={"source": 50, "dest": preset_tag})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="on_delay", args={"timer": timer, "preset": preset_tag, "reset": None}
                )
            ],
        ),
    ]


def timer_acc_zero_copy(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #5: copy(0, T.Acc) alongside one timer owner."""
    if not pool.timers:
        return None
    timer = pool.timers[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[InstrSpec(kind="copy", args={"source": 0, "dest": timer.Acc})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(kind="on_delay", args={"timer": timer, "preset": 100, "reset": None})
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=timer.Acc, op=">=", operand=10)],
            instructions=[_default_output(pool)],
        ),
    ]


def counter_acc_calc_boost(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #6: calc(C.Acc + K, C.Acc) alongside one counter owner."""
    if not pool.counters or not pool.writable_bool():
        return None
    counter = pool.counters[0]
    reset_tag = pool.writable_bool()[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="calc",
                    args={"source": counter.Acc, "op": "add", "literal": 5, "dest": counter.Acc},
                )
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="count_up",
                    args={"counter": counter, "preset": 10, "reset": reset_tag, "down": None},
                )
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=counter.Acc, op=">=", operand=5)],
            instructions=[_default_output(pool)],
        ),
    ]


def exclusive_inputs_across_scans(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #7: two external Bools with rise() in separate rungs."""
    if len(pool.bool_inputs) < 2 or not pool.writable_bool():
        return None
    in0, in1 = pool.bool_inputs[0], pool.bool_inputs[1]
    outputs = pool.writable_bool()
    out0 = outputs[0]
    out1 = outputs[min(1, len(outputs) - 1)]
    return [
        RungSpec(
            conditions=[CondSpec(kind="rise", tag=in0)],
            instructions=[InstrSpec(kind="out", args={"target": out0})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="rise", tag=in1)],
            instructions=[InstrSpec(kind="out", args={"target": out1})],
        ),
    ]


def count_down_constant_preset(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #8: count_down(C, 5).reset(R) + Rung(C.Acc <= -3)."""
    if not pool.counters or not pool.writable_bool():
        return None
    counter = pool.counters[0]
    reset_tag = pool.writable_bool()[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="count_down",
                    args={"counter": counter, "preset": 5, "reset": reset_tag},
                )
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=counter.Acc, op="<=", operand=-3)],
            instructions=[_default_output(pool)],
        ),
    ]


def bidirectional_counter(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #9: count_up(C, 10).down(D).reset(R)."""
    if not pool.counters or len(pool.writable_bool()) < 2:
        return None
    counter = pool.counters[0]
    bools = pool.writable_bool()
    down_tag = bools[0]
    reset_tag = bools[1]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="count_up",
                    args={"counter": counter, "preset": 10, "reset": reset_tag, "down": down_tag},
                )
            ],
        ),
    ]


def self_referencing_accumulator(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #10: calc(N + 1, N) using a numeric tag."""
    if not pool.writable_numeric():
        return None
    tag = pool.writable_numeric()[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(kind="calc", args={"source": tag, "op": "add", "literal": 1, "dest": tag})
            ],
        ),
    ]


def truthy_accumulator_contact(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #11: on_delay(T, 100) + Rung(T.Acc)."""
    if not pool.timers:
        return None
    timer = pool.timers[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(kind="on_delay", args={"timer": timer, "preset": 100, "reset": None})
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="truthy", tag=timer.Acc)],
            instructions=[_default_output(pool)],
        ),
    ]


def _default_output(pool: TagPool) -> InstrSpec:
    if pool.writable_bool():
        return InstrSpec(kind="out", args={"target": pool.writable_bool()[0]})
    return InstrSpec(kind="copy", args={"source": 1, "dest": pool.writable_numeric()[0]})


TIER1_PATTERNS = [
    timer_acc_downstream_compare,
    copy_chain_into_compare,
    conditional_write_edge_read,
    dynamic_preset,
    timer_acc_zero_copy,
    counter_acc_calc_boost,
    exclusive_inputs_across_scans,
    count_down_constant_preset,
    bidirectional_counter,
    self_referencing_accumulator,
    truthy_accumulator_contact,
]
