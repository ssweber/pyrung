"""Tier 1 wiring pattern templates for structured program generation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core import Char, Int

from .strategies import BranchSpec, CondSpec, InstrSpec, RungSpec

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


def init_guarded_single_writer(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #11a: ~InitDone gates exhaustive single-writer init block."""
    writable_bools = pool.writable_bool()
    writable_nums = pool.writable_numeric()
    if not writable_bools or not writable_nums:
        return None
    init_done = writable_bools[0]
    extra_bools = writable_bools[1:]
    init_nums = writable_nums[: min(2, len(writable_nums))]
    init_instrs: list[InstrSpec] = []
    for tag in init_nums:
        init_instrs.append(InstrSpec(kind="copy", args={"source": 0, "dest": tag}))
    for tag in extra_bools:
        init_instrs.append(InstrSpec(kind="reset", args={"target": tag}))
    init_instrs.append(InstrSpec(kind="latch", args={"target": init_done}))
    read_target = init_nums[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="negated", tag=init_done)],
            instructions=init_instrs,
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=read_target, op="==", operand=0)],
            instructions=[_default_output(pool)],
        ),
    ]


def timer_chain_advancement(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #11c: T2 enabled by T1.Done with State copy in between."""
    if len(pool.timers) < 2 or not pool.writable_numeric():
        return None
    t1, t2 = pool.timers[0], pool.timers[1]
    state = pool.writable_numeric()[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(kind="on_delay", args={"timer": t1, "preset": 50, "reset": None})
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=t1.Done)],
            instructions=[InstrSpec(kind="copy", args={"source": 2, "dest": state})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=state, op="==", operand=2)],
            instructions=[
                InstrSpec(kind="on_delay", args={"timer": t2, "preset": 50, "reset": None})
            ],
        ),
    ]


def multi_hop_copy_chain(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #15b: 3-hop copy A -> B -> C -> D + downstream compare."""
    writable_nums = pool.writable_numeric()
    if len(writable_nums) < 4:
        return None
    a, b, c, d = writable_nums[0], writable_nums[1], writable_nums[2], writable_nums[3]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[InstrSpec(kind="copy", args={"source": a, "dest": b})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[InstrSpec(kind="copy", args={"source": b, "dest": c})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[InstrSpec(kind="copy", args={"source": c, "dest": d})],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=d, op=">=", operand=5)],
            instructions=[_default_output(pool)],
        ),
    ]


def range_sum_into_compare(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #29: calc(block.select(start, end).sum(), Total) + Rung(Total != 0)."""
    if pool.int_block is None or not pool.writable_numeric() or not pool.writable_bool():
        return None
    blk = pool.int_block
    total = pool.writable_numeric()[0]
    start = blk.start
    end = min(blk.start + 1, blk.end)
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="range_sum_calc",
                    args={"block": blk, "start": start, "end": end, "dest": total},
                )
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=total, op="!=", operand=0)],
            instructions=[_default_output(pool)],
        ),
    ]


def band_collapse_pattern(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #30: range-sum drives a one-off band-tagged Int + downstream compare."""
    if pool.int_block is None or not pool.writable_bool():
        return None
    blk = pool.int_block
    band_total = Int("BandTotal", band={"ZERO": 0, "POSITIVE": ">0"})
    start = blk.start
    end = min(blk.start + 1, blk.end)
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="range_sum_calc",
                    args={"block": blk, "start": start, "end": end, "dest": band_total},
                )
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=band_total, op="!=", operand=0)],
            instructions=[_default_output(pool)],
        ),
    ]


def indirect_oob_source(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #31: copy(int_block[ptr], dest) where ptr can be OOB on source side."""
    if pool.int_block is None or not pool.int_tags or not pool.writable_numeric():
        return None
    blk = pool.int_block
    ptr = pool.int_tags[0]
    dest_candidates = [t for t in pool.writable_numeric() if t is not ptr]
    if not dest_candidates:
        return None
    dest = dest_candidates[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="indirect_copy",
                    args={
                        "block": blk,
                        "ptr": ptr,
                        "offset": 0,
                        "dest": dest,
                        "is_source": True,
                    },
                )
            ],
        ),
    ]


def identity_calc(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #32: calc(X + 0, Y) + Rung(Y == K) — identity calc keeps X observed."""
    writable_nums = pool.writable_numeric()
    all_nums = pool.all_numeric()
    if not writable_nums or not all_nums:
        return None
    y = writable_nums[0]
    x_candidates = [t for t in all_nums if t is not y]
    if not x_candidates:
        return None
    x = x_candidates[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=pool.all_conditions()[0])],
            instructions=[
                InstrSpec(
                    kind="calc",
                    args={"source": x, "op": "add", "literal": 0, "dest": y},
                )
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=y, op="==", operand=0)],
            instructions=[_default_output(pool)],
        ),
    ]


def char_state_machine(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #11b: Char tag drives state machine via copy("g", State) + Rung(State == "g")."""
    if not pool.char_tags or not pool.timers or not pool.writable_bool():
        return None
    state = pool.char_tags[0]
    timer = pool.timers[0]
    return [
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=state, op="==", operand="g")],
            instructions=[
                InstrSpec(kind="on_delay", args={"timer": timer, "preset": 50, "reset": None, "unit": "ms"})
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=timer.Done)],
            instructions=[
                InstrSpec(kind="copy", args={"source": "y", "dest": state, "oneshot": False})
            ],
        ),
        RungSpec(
            conditions=[CondSpec(kind="compare", tag=state, op="==", operand="y")],
            instructions=[_default_output(pool)],
        ),
    ]


def branch_under_rung(pool: TagPool) -> list[RungSpec] | None:
    """Pattern #15c: branch(cond) inside Rung — tests nested condition scoping."""
    if len(pool.writable_bool()) < 2 or len(pool.all_conditions()) < 2:
        return None
    conds = pool.all_conditions()
    bools = pool.writable_bool()
    return [
        RungSpec(
            conditions=[CondSpec(kind="bit", tag=conds[0])],
            instructions=[_default_output(pool)],
            branches=[
                BranchSpec(
                    conditions=[CondSpec(kind="bit", tag=conds[min(1, len(conds) - 1)])],
                    instructions=[InstrSpec(kind="out", args={"target": bools[1]})],
                ),
            ],
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
    init_guarded_single_writer,
    timer_chain_advancement,
    multi_hop_copy_chain,
    range_sum_into_compare,
    band_collapse_pattern,
    indirect_oob_source,
    identity_calc,
    char_state_machine,
    branch_under_rung,
]
