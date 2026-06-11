"""Copy-source prerequisite binding (the sm_copy_or_jump_state shape).

A PackML state register is written ONLY by ``copy(Requested, Current)`` —
the value never appears as a writable literal, so regression must bind the
copy SOURCE at the goal value to find the chain.  On the live Tumbler/Dryer
template this gap (plus a ``return_early()`` crash in the projected oracle,
covered in ``test_causal_prospective.py``) turned ``how(S_StateCurrent ==
4)`` into a false ``unsolvable`` certificate: the corridor parks at
Resetting(15) because completion is gated on production mode, and nothing
named the mode prerequisite (probe14/15, burnerloop findings).

Both halves of the regression get the binding:

- ``projected_cause`` (recovery oracle) — tripwires in
  ``tests/core/test_causal_prospective.py::TestProjectedCauseCopyWriters``.
- ``_unsatisfied_conditions`` (static establish path) — pinned here, so the
  chain is found at establish time instead of burning recovery rounds.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, call, copy, out, reset, subroutine
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk.priors import _unsatisfied_conditions
from pyrung.core.runner import PLC


def _jump_state_program():
    """Distilled template chain: mode handshake -> completion -> state copy.

    From cold (Cur=9, Mode=3):

    - pulse ``Adv``: Cur 9 -> 15 (Resetting) and parks — completion (R4)
      is gated on ``Mode == 1`` and no single input reaches Mode.
    - ground truth: simultaneous ``{ProdMode, ChgReq}`` (the consumed-
      same-scan mode handshake, same shape as test_walk_handshake) flips
      Mode to 1; completion then fires Req=4 and ``copy(Req, Cur)`` lands
      Idle within the scan.

    Cur=4 is producible only through the copy source — no writer carries
    it as a literal.
    """
    ProdMode = Bool("ProdMode", external=True)
    ChgReq = Bool("ChgReq", external=True)
    Adv = Bool("Adv", external=True)
    UnitMode = Int("UnitMode", default=5)
    ReqBool = Int("ReqBool")
    Mode = Int("Mode", default=3)
    Req = Int("Req")
    Cur = Int("Cur", default=9)
    CompleteBool = Int("CompleteBool")
    Target = Bool("Target")

    @subroutine("mode_sub")
    def mode_sub():
        with Rung(ProdMode):
            copy(1, UnitMode, oneshot=True)
        with Rung(UnitMode >= 1, UnitMode <= 3):
            copy(UnitMode, Mode)
        with Rung():
            copy(0, ReqBool)
        with Rung():
            copy(0, UnitMode)
        with Rung():
            reset(ChgReq)

    with Program() as prog:
        with Rung(ChgReq):
            copy(1, ReqBool, oneshot=True)
        with Rung(ReqBool == 1):
            call(mode_sub)
        with Rung(Adv, Cur == 9):
            copy(15, Req)
        with Rung(Mode == 1, Cur == 15):
            copy(1, CompleteBool)
        with Rung(CompleteBool == 1):
            copy(4, Req)
            copy(0, CompleteBool)
        with Rung(Req != 0):
            copy(Req, Cur)
            copy(0, Req)
        with Rung(Cur == 4):
            out(Target)

    return prog, Target


def test_ground_truth() -> None:
    prog, _target = _jump_state_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    plc.patch({"Adv": True})
    plc.step()
    assert plc.state.tags["Cur"] == 15
    plc.patch({"ProdMode": True, "ChgReq": True})
    plc.step()
    assert plc.state.tags["Mode"] == 1
    assert plc.state.tags["Cur"] == 4
    assert plc.state.tags["Target"] is True


def test_walk_solves_through_copy_source_chain() -> None:
    """Pre-fix this was a false ``unsolvable``: the corridor parks at 15,
    and with the copy source unbound nothing ever named the mode chain."""
    prog, target = _jump_state_program()
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(target)
    assert path.reachable
    patches: dict = {}
    for step in path.steps:
        if step.action:
            patches.update(step.action)
    assert patches.get("Adv") is True
    assert patches.get("ProdMode") is True
    assert patches.get("ChgReq") is True


def test_unsatisfied_conditions_binds_copy_source() -> None:
    """``copy(Req, Cur)`` with goal ``Cur == 4`` must spawn ``(Req, 4)`` —
    the data-flow half of writer regression (conditions alone name only
    ``Req != 0``-shaped gates, which carry no value)."""
    prog, _target = _jump_state_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)

    prereqs = _unsatisfied_conditions(
        "Cur",
        4,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    assert ("Req", 4) in prereqs


def test_indirect_copy_writer_walks_without_crashing() -> None:
    """A goal whose writer is ``copy(block[ptr], Dest)`` must yield an
    honest verdict, not a crash.  Pre-fix, ``_written_value_for_tag``
    classified the IndirectRef source as a "literal", and the first
    ``==``/``!=`` against the goal value built a deferred Condition that
    raised on truth-testing — surfaced on the live template the moment
    the writer-group ordering walked a goal inside
    ``sm_copy_or_jump_state``'s indirect machinery."""
    from pyrung.core.memory_block import Block
    from pyrung.core.tag import TagType

    blk = Block("DS", TagType.INT, 1, 10)
    Ptr = Int("Ptr", default=1)
    Gate = Bool("Gate", external=True)
    Dest = Int("Dest")
    Hit = Bool("Hit")

    with Program() as prog:
        with Rung(Gate):
            copy(blk[Ptr], Dest)
        with Rung(Dest == 4):
            out(Hit)

    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(Hit)
    assert path is not None
    # blk holds defaults (0) and nothing steers it, so the honest verdict
    # is unreachable — the point is the walk completes.
    assert not path.reachable


def test_transient_copy_source_not_bound_as_boundary_goal() -> None:
    """A *transient* copy source must stay out of the prereqs: a boundary
    goal for it is structurally unreachable (the poisoning the handshake
    work removed) — the bundles cover that route mid-scan instead."""
    prog, _target = _jump_state_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)

    # Mode's writer is copy(UnitMode, Mode); UnitMode is consumed-same-scan
    # (rest 0), so the binding (UnitMode, 1) must be filtered out.
    prereqs = _unsatisfied_conditions(
        "Mode",
        1,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    assert ("UnitMode", 1) not in prereqs
