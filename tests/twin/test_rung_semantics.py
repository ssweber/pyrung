"""First three twin-harness tests: rung semantics."""

from __future__ import annotations

from pyrung.core import Rung, copy
from pyrung.twin import assert_all_passed, case, run


def prove_memory_is_immediate(slot):
    with Rung(slot.Cmd != 0):
        copy(42, slot.Scratch)
        copy(slot.Scratch, slot.Result1)


def prove_source_order(slot):
    with Rung(slot.Cmd != 0):
        copy(10, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(20, slot.Result2)


def prove_global_mutation(slot):
    with Rung(slot.Cmd != 0):
        copy(99, slot.Scratch)
    with Rung(slot.Cmd != 0):
        copy(slot.Scratch, slot.Result1)


cases = [
    case(
        "Memory writes are visible to the next instruction on the same scan",
        ladder=prove_memory_is_immediate,
        expect={"Result1": 42},
    ),
    case(
        "Rungs evaluate all conditions first, then execute instructions in source order",
        ladder=prove_source_order,
        expect={"Result1": 10, "Result2": 20},
    ),
    case(
        "Instructions mutate global memory",
        ladder=prove_global_mutation,
        expect={"Result1": 99},
    ),
]


def test_rung_semantics():
    results = run(cases)
    assert_all_passed(results)
