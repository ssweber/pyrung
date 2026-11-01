"""Runner bracket hook: a hand-built synthesis overlay reproduces the dwell.

Increment 2 of the synthesis-is-rungs consolidation.  The PLC scans
``_synthesis.holds`` before user logic and ``_synthesis.plant`` after, in the
same ctx/commit.  Here we hand-build a ``plant`` TON/TOF pair (the shape the
harness factory will emit) and assert it reproduces the bool dwell — *without*
the harness, proving the bracket itself carries the semantics.
"""

from __future__ import annotations

from typing import Any

from pyrung import Bool, Int, Program, Rung, copy
from pyrung.core.condition import AllCondition, BitCondition, NormallyClosedCondition
from pyrung.core.runner import PLC
from pyrung.core.synthesis import (
    Synthesis,
    bool_feedback_rungs,
    conditional_hold_rung,
    copy_hold_rung,
)


def _prog() -> Program:
    Enable = Bool("Enable", external=True)
    Fb = Bool("Fb")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Fb):
            copy(1, Stage)
    return prog


def _plant_overlay(plc: PLC, *, on_ms: int, off_ms: int) -> Synthesis:
    En = plc._known_tags_by_name["Enable"]
    Fb = plc._known_tags_by_name["Fb"]
    rungs = bool_feedback_rungs(
        enable=BitCondition(En),
        fb_tag=Fb,
        ton_done=Bool("__cpl_ond__Fb"),
        ton_acc=Int("__cpl_on__Fb"),
        tof_acc=Int("__cpl_off__Fb"),
        on_delay_ms=on_ms,
        off_delay_ms=off_ms,
    )
    return Synthesis(plant=rungs)


def _run(plc: PLC, en_seq: list[bool]) -> list[bool]:
    out = []
    for en in en_seq:
        plc.patch({"Enable": en})
        plc.step()
        out.append(plc.state.tags["Fb"])
    return out


def test_plant_ton_reproduces_dwell() -> None:
    plc = PLC(_prog(), dt=0.1)
    plc._synthesis = _plant_overlay(plc, on_ms=200, off_ms=100)
    # on_delay 200ms = 2 scans, off_delay 100ms = 1 scan; En held 4 then 3 off.
    # __plant__ is the pre-logic input-read: it reads the *previous* commit's En,
    # so the committed feedback lags the command by one scan (feedback is an
    # input): rise at scan 3, three trues, fall at scan 6.
    assert _run(plc, [True] * 4 + [False] * 3) == [False, False, True, True, True, False, False]


def test_plant_glitch_is_suppressed() -> None:
    plc = PLC(_prog(), dt=0.1)
    plc._synthesis = _plant_overlay(plc, on_ms=300, off_ms=100)  # 3-scan on-delay
    # A 1-scan glitch (< on_delay) never sustains the TON → Fb stays False.
    assert _run(plc, [True] + [False] * 6) == [False] * 7


def test_empty_synthesis_is_inert() -> None:
    plc = PLC(_prog(), dt=0.1)
    plc._synthesis = Synthesis()  # no holds, no plant
    # Fb is never synthesized; the program never sets Stage.
    assert _run(plc, [True, True, True]) == [False, False, False]
    assert plc.state.tags["Stage"] == 0


def test_conditional_hold_branch_oscillates() -> None:
    # A two-rule liveness hold as ONE rung: both guards mean the state the
    # bracket starts from, so the polarities stay mutually exclusive and the tag
    # oscillates each scan.  The bracket folds them into one out(W) (two
    # writers of W in one rung would cancel); toggle() would not do here,
    # since it flips on a rising edge, not every scan.
    from pyrung.core.condition import CompareNe

    W = Bool("W")
    with Program() as prog:
        with Rung(W):
            copy(1, Int("Seen"))
    plc = PLC(prog, dt=0.1)
    plc._synthesis = Synthesis(
        holds=[
            conditional_hold_rung(
                dest=W, rules=[(True, CompareNe(W, True)), (False, CompareNe(W, False))]
            )
        ]
    )
    seq = []
    for _ in range(4):
        plc.step()
        seq.append(plc.state.tags["W"])
    assert seq == [True, False, True, False]


def test_single_rule_conditional_hold_self_releases() -> None:
    # One guarded copy rung = a self-releasing hold: drive True while W != True,
    # so W settles True and the guard stops firing (it no longer re-drives, but the
    # value persists).  This is the "with Rung(guard): copy(value)" shape.
    from pyrung.core.condition import CompareNe

    W = Bool("W")
    with Program() as prog:
        with Rung(W):
            copy(1, Int("Seen"))
    plc = PLC(prog, dt=0.1)
    plc._synthesis = Synthesis(holds=[copy_hold_rung(value=True, dest=W, guard=CompareNe(W, True))])
    seq = []
    for _ in range(3):
        plc.step()
        seq.append(plc.state.tags["W"])
    assert seq == [True, True, True]


def test_false_hold_turns_default_true_bool_off() -> None:
    W = Bool("W", default=True)
    with Program() as prog:
        with Rung(W):
            copy(1, Int("Seen"))
    plc = PLC(prog, dt=0.1)
    plc._synthesis = Synthesis(holds=[copy_hold_rung(value=False, dest=W)])

    plc.step()

    assert plc.state.tags["W"] is False


def test_holds_bracket_steers_input_before_program_reads_it() -> None:
    # A steady hold copies True into Enable each scan *before* user logic, so the
    # program sees the held input the same scan.  The pre-logic plant reads the
    # *previous* commit, so it arms off the held Enable the next scan (on_delay 0
    # → Fb on the scan after Enable first commits).
    plc = PLC(_prog(), dt=0.1)
    syn = _plant_overlay(plc, on_ms=0, off_ms=0)
    En = plc._known_tags_by_name["Enable"]
    syn.holds.append(copy_hold_rung(value=True, dest=En))
    plc._synthesis = syn
    # Never patch Enable — the hold drives it.  on_delay 0 → Fb on the next scan.
    plc.step()
    assert plc.state.tags["Enable"] is True
    plc.step()
    assert plc.state.tags["Fb"] is True


def _bracket_as_program_rung() -> Program:
    """A two-rule liveness bracket compiled as an ordinary rung of a program."""
    from pyrung.core.condition import CompareNe

    W = Bool("W")
    prog = Program()
    prog.rungs.append(
        conditional_hold_rung(
            dest=W, rules=[(True, CompareNe(W, True)), (False, CompareNe(W, False))]
        )
    )
    return prog


def test_bracket_rules_read_the_starting_state_on_every_backend() -> None:
    # A synthesis bracket runs like a user rung; it is laid out so its rules
    # still read the state it starts from.  The interpreter, the
    # observed executor path, the debugger and the compiled kernel agree.
    from pyrung.core import CompiledPLC
    from pyrung.core.executor import _NoopExecutionObserver, execute_program

    expected = [True, False, True, False]

    def run(plc: PLC | CompiledPLC) -> list[bool]:
        seq = []
        for _ in range(4):
            plc.step()
            seq.append(plc.current_state.tags["W"])
        return seq

    assert run(PLC(_bracket_as_program_rung())) == expected
    assert run(CompiledPLC(_bracket_as_program_rung())) == expected

    program = _bracket_as_program_rung()
    observed = PLC(program)
    seq = []
    for _ in range(4):
        ctx, dt = observed.debug.prepare_scan()
        execute_program(program, ctx, observer=_NoopExecutionObserver())
        observed.debug.commit_scan(ctx, dt)
        seq.append(observed.current_state.tags["W"])
    assert seq == expected

    debugged = PLC(_bracket_as_program_rung())
    seq = []
    for _ in range(4):
        list(debugged.debug.scan_steps_debug())
        seq.append(debugged.current_state.tags["W"])
    assert seq == expected


def test_a_user_rung_of_the_same_shape_solves_row_by_row() -> None:
    # The same two rules as user branches: the second reads the first's write,
    # so they cancel and W stays False (a CLICK does the same).
    from pyrung import branch, latch, reset

    W = Bool("W")
    with Program() as prog:
        with Rung():
            with branch(~W):
                latch(W)
            with branch(W):
                reset(W)
    plc = PLC(prog)
    seq = []
    for _ in range(3):
        plc.step()
        seq.append(plc.current_state.tags["W"])
    assert seq == [False, False, False]


def _backends(program_factory: Any) -> list[Any]:
    from pyrung.core import CompiledPLC

    return [PLC(program_factory()), CompiledPLC(program_factory())]


def _bracket_program(rung: Rung) -> Program:
    prog = Program()
    prog.rungs.append(rung)
    return prog


def test_non_bool_alternation_reads_the_starting_state_on_every_backend() -> None:
    # Two rules on one Int whose guards read it: a copy row per value would let
    # the second row read the first's write.  The bracket solves the guards
    # first, so Sel alternates 3, 0, 3, 0 as the rules say.
    from pyrung.core.condition import CompareEq, CompareNe
    from pyrung.core.synthesis import guarded_copy_rung

    def factory() -> Program:
        sel = Int("Sel")
        return _bracket_program(
            guarded_copy_rung([(sel, 3, CompareNe(sel, 3)), (sel, 0, CompareEq(sel, 3))])
        )

    for plc in _backends(factory):
        seq = []
        for _ in range(4):
            plc.step()
            seq.append(plc.current_state.tags["Sel"])
        assert seq == [3, 0, 3, 0]


def test_guarded_copy_last_active_rule_wins_from_the_starting_state() -> None:
    # A reads B and B reads A; every guard means the state the bracket starts
    # from, the last active rule for a destination wins, and a Bool with no
    # active rule returns to False.
    from pyrung.core.synthesis import guarded_copy_rung

    def reference(a: bool, b: bool) -> tuple[bool, bool]:
        new_a = False
        if not b:
            new_a = True
        if a and b:
            new_a = False
        new_b = bool(a)
        return new_a, new_b

    def factory() -> Program:
        a, b = Bool("A"), Bool("B")
        return _bracket_program(
            guarded_copy_rung(
                [
                    (a, True, NormallyClosedCondition(b)),
                    (b, True, BitCondition(a)),
                    (a, False, AllCondition(BitCondition(a), BitCondition(b))),
                ]
            )
        )

    for plc in _backends(factory):
        state = (False, False)
        for _ in range(6):
            plc.step()
            state = reference(*state)
            tags = plc.current_state.tags
            assert (tags["A"], tags["B"]) == state
