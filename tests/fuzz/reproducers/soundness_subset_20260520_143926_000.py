"""Reproducer: optimization-subset soundness disagreement."""

from dataclasses import replace

from pyrung.core import (
    PLC, And, Block, Bool, Char, CompiledPLC, Counter, Dint, Int, Or, Program, Real, Rung,
    TagType, Timer, Word, blockcopy, branch, calc, call, copy, count_down, count_up,
    event_drum, fall, fill, forloop, latch, lro, lsh, off_delay, on_delay, out, pack_bits,
    pack_text, pack_words, receive, reset, return_early, rise, rro, rsh, search, shift,
    subroutine, time_drum,
    to_ascii, to_binary, to_text, to_value, unpack_to_bits, unpack_to_words,
)
from pyrung.core.analysis.prove import Intractable, prove
from pyrung.core.analysis.prove.passes import _OptConfig


def test_reproducer():
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    In2 = Bool("In2", external=True)
    In3 = Bool("In3", external=True)
    B0 = Bool("B0")
    ExtN0 = Int("ExtN0", external=True, min=-10, max=10)
    ExtN1 = Int("ExtN1", external=True, choices={0: 'Off', 1: 'On', 2: 'Auto'})
    D0 = Dint("D0")
    D1 = Dint("D1")
    R0 = Real("R0")
    W0 = Word("W0")
    Ch0 = Char("Ch0")
    C0 = Counter.clone("C0")
    C1 = Counter.clone("C1")
    DS = Block("DS", TagType.INT, 1, 7)
    CB = Block("CB", TagType.BOOL, 1, 8)
    CH = Block("CH", TagType.CHAR, 1, 3)

    with Program(strict=False) as logic:
        with Rung():
            out(B0)
        with Rung():
            calc(C0.Acc + 5, C0.Acc)
        with Rung():
            count_up(C0, 10).reset(B0)
        with Rung():
            count_up(C1, 5).reset(C1.Done)
        with Rung(C1.Done):
            out(B0)

    # disagreeing optimization subset: ['accumulator_absorption']
    # candidate=Counterexample, baseline=Proven
    candidate_cfg = replace(
        _OptConfig.sound_baseline(),
        accumulator_absorption=True,
    )
    baseline = prove(logic, C1.Acc < 41, max_states=10_000, depth_budget=20,
                     _opt_config=_OptConfig.sound_baseline())
    candidate = prove(logic, C1.Acc < 41, max_states=10_000, depth_budget=20,
                      _opt_config=candidate_cfg)

    if isinstance(baseline, Intractable) or isinstance(candidate, Intractable):
        return
    assert type(candidate) is type(baseline), (
        f"candidate={type(candidate).__name__}, baseline={type(baseline).__name__}"
    )
