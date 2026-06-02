"""Reproducer: optimization soundness disagreement."""

from pyrung.core import (
    And, Block, Bool, Char, Counter, Dint, Int, Or, Program, Real, Rung,
    TagType, Timer, Word, blockcopy, branch, calc, call, copy, count_down, count_up,
    event_drum, fall, fill, forloop, latch, lro, lsh, off_delay, on_delay, out, pack_bits,
    pack_text, pack_words, receive, reset, return_early, rise, rro, rsh, search, shift,
    subroutine, time_drum,
    to_ascii, to_binary, to_text, to_value, unpack_to_bits, unpack_to_words,
)
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, always


def test_reproducer():
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    In2 = Bool("In2", external=True)
    B0 = Bool("B0")
    B1 = Bool("B1")
    B2 = Bool("B2")
    ExtN0 = Int("ExtN0", external=True, choices={1: 'A', 2: 'B'})
    W0 = Word("W0")
    Ch0 = Char("Ch0")
    Ch1 = Char("Ch1")
    C0 = Counter.clone("C0")
    C1 = Counter.clone("C1")
    DS = Block("DS", TagType.INT, 1, 5)
    CB = Block("CB", TagType.BOOL, 1, 8)

    with Program(strict=False) as logic:
        with Rung():
            copy(DS[DS[1]], W0)
        with Rung():
            calc(lsh(W0, 2), W0)
        with Rung(In0):
            copy(1, W0)
        with Rung():
            copy(Ch0, W0, convert=to_ascii)

    # To add to test_prove.py, use: _assert_soundness(logic, W0 < 33)
    optimized = always(logic, W0 < 33, max_states=10_000, depth_budget=20)
    unoptimized = always(logic, W0 < 33, max_states=10_000, depth_budget=20,
                        _skip_optimizations=True)

    # optimized=Proven, unoptimized=Counterexample
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        return
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}"
    )
