"""Reproducer: optimization soundness disagreement."""

from pyrung.core import (
    And, Block, Bool, Char, Counter, Dint, Int, Or, Program, Real, Rung,
    TagType, Timer, Word, blockcopy, branch, calc, call, copy, count_down, count_up,
    event_drum, fall, fill, forloop, latch, lro, lsh, off_delay, on_delay, out, pack_bits,
    pack_text, pack_words, receive, reset, return_early, rise, rro, rsh, search, shift,
    subroutine, time_drum,
    to_ascii, to_binary, to_text, to_value, unpack_to_bits, unpack_to_words,
)
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, prove


def test_reproducer():
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    In2 = Bool("In2", external=True)
    B0 = Bool("B0")
    B1 = Bool("B1")
    N0 = Int("N0")
    D0 = Dint("D0")
    D1 = Dint("D1")
    R0 = Real("R0")
    W0 = Word("W0")
    Ch0 = Char("Ch0")
    Ch1 = Char("Ch1")
    T0 = Timer.clone("T0")
    T1 = Timer.clone("T1")
    BandTotal = Int("BandTotal", band={'ZERO': 0, 'POSITIVE': '>0'})
    DS = Block("DS", TagType.INT, 1, 5)
    CB = Block("CB", TagType.BOOL, 1, 8)
    CH = Block("CH", TagType.CHAR, 1, 3)

    with Program(strict=False) as logic:
        with Rung():
            unpack_to_words(D0, DS.select(1, 2))
        with Rung():
            calc(DS.select(1, 2).sum(), BandTotal)
        with Rung(BandTotal != 0):
            out(B0)
        with Rung():
            copy(Ch1, D0, convert=to_ascii)
        with Rung():
            search(DS.select(2, 2) <= 39, result=D0, found=B1)

    # To add to test_prove.py, use: _assert_soundness(logic, Or(~B1, ~B0))
    optimized = prove(logic, Or(~B1, ~B0), max_states=10_000, depth_budget=20)
    unoptimized = prove(logic, Or(~B1, ~B0), max_states=10_000, depth_budget=20,
                        _skip_optimizations=True)

    # optimized=Proven, unoptimized=Counterexample
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        return
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}"
    )
