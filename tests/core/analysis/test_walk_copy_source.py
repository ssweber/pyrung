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


def _jump_table_program(expr_index: bool = False):
    """Jump-table writer (the ``sm__where2jump`` shape, distilled).

    The table is configuration data (``default_factory``: slot ``a`` holds
    ``100 + a``); the index register moves only via ``copy(3, Ptr)`` gated
    on ``Sel``.  The goal value is producible only by landing the index on
    the inverting value first — no writer carries it as a literal, and the
    source is statically unresolvable (``blk[Ptr]`` / ``blk[Ptr + 2]``).

    - plain (``expr_index=False``): goal ``Cur == 103`` ⇒ slot 3 ⇒ Ptr=3
    - expr  (``expr_index=True``):  goal ``Cur == 105`` ⇒ slot 5 ⇒ Ptr=3
    """
    from pyrung.core.memory_block import Block
    from pyrung.core.tag import TagType

    blk = Block("JT", TagType.INT, 1, 10, default_factory=lambda a: 100 + a)
    Ptr = Int("Ptr", default=1)
    Sel = Bool("Sel", external=True)
    Go = Bool("Go", external=True)
    Cur = Int("Cur")
    Hit = Bool("Hit")
    goal = 105 if expr_index else 103
    source = blk[Ptr + 2] if expr_index else blk[Ptr]

    with Program() as prog:
        with Rung(Sel):
            copy(3, Ptr)
        with Rung(Go):
            copy(source, Cur)
        with Rung(Cur == goal):
            out(Hit)

    return prog, Hit, goal


def test_walk_chases_indirect_jump_table_index() -> None:
    """Idx-chasing (Open #11): ``copy(blk[Ptr], Cur)`` with goal ``Cur ==
    103`` must invert the table on the live snapshot and sub-goal the
    *index register* — pre-fix the indirect writer was skipped outright
    and the walk returned a false unreachable."""
    prog, hit, _goal = _jump_table_program()
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(hit)
    assert path.reachable
    patches: dict = {}
    for step in path.steps:
        if step.action:
            patches.update(step.action)
    assert patches.get("Sel") is True
    assert patches.get("Go") is True


def test_walk_chases_indirect_expr_index() -> None:
    """Same chase through an expression index (``blk[Ptr + 2]``): the
    address is evaluated per candidate on a snapshot overlay, so pointer
    arithmetic needs no symbolic inversion."""
    prog, hit, _goal = _jump_table_program(expr_index=True)
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(hit)
    assert path.reachable
    patches: dict = {}
    for step in path.steps:
        if step.action:
            patches.update(step.action)
    assert patches.get("Sel") is True
    assert patches.get("Go") is True


def test_unsatisfied_conditions_binds_inverted_index() -> None:
    """The static extractor must spawn ``(Ptr, 3)`` for goal ``Cur == 103``
    — the index binding is the data-flow half of indirect-copy regression,
    exactly as ``(Req, 4)`` is for the direct copy."""
    prog, _hit, goal = _jump_table_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)

    prereqs = _unsatisfied_conditions(
        "Cur",
        goal,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    assert ("Ptr", 3) in prereqs


def _scratch_pointer_program():
    """The full ``sm__where2jump`` shape: the pointer is calc-defined
    scratch one rung before the indirect copy reads it.

    ``calc(Req + 2, Scratch); copy(blk[Scratch], Cur)`` — the chase must
    hop from the scratch to ``Req`` (the register the program drives) and
    fold the ``+2`` into the address.  Goal ``Cur == 205`` ⇒ slot 5 ⇒
    ``Req == 3``.
    """
    from pyrung import calc
    from pyrung.core.memory_block import Block
    from pyrung.core.tag import TagType

    blk = Block("JT3", TagType.INT, 1, 20, default_factory=lambda a: 200 + a)
    Req = Int("Req")
    Scratch = Int("Scratch")
    Sel = Bool("Sel", external=True)
    Go = Bool("Go", external=True)
    Cur = Int("Cur")
    Hit = Bool("Hit")

    with Program() as prog:
        with Rung(Sel):
            copy(3, Req)
        with Rung():
            calc(Req + 2, Scratch)
        with Rung(Go):
            copy(blk[Scratch], Cur)
        with Rung(Cur == 205):
            out(Hit)

    return prog, Hit


def test_walk_chases_through_calc_scratch_pointer() -> None:
    """End-to-end through the calc-defined scratch pointer.  The pipeline
    classifies such scratch as slice-elided scan-local (not a functional
    dependent), so this exercises the calc-expression fallback hop."""
    prog, hit = _scratch_pointer_program()
    plc = PLC(prog, dt=0.010)
    plc.step()

    path = plc.how(hit)
    assert path.reachable
    patches: dict = {}
    for step in path.steps:
        if step.action:
            patches.update(step.action)
    assert patches.get("Sel") is True
    assert patches.get("Go") is True


def test_chase_follows_functional_dep_projection() -> None:
    """When the pipeline DOES project the scratch (``func_deps``), the
    chase follows the projection — pinned with a hand-built map so the
    path is covered independently of pass ordering."""
    prog, _hit = _scratch_pointer_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)

    prereqs = _unsatisfied_conditions(
        "Cur",
        205,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
        func_deps={"Scratch": ("Req", 2)},
    )
    assert ("Req", 3) in prereqs


def test_unsatisfied_conditions_binds_scratch_hop_statically() -> None:
    """Without projections the same binding must come from the sole
    writer's calc expression (the live-template path: scratch is
    slice-elided, so no projection exists)."""
    prog, _hit = _scratch_pointer_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)

    prereqs = _unsatisfied_conditions(
        "Cur",
        205,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    assert ("Req", 3) in prereqs


def test_index_candidates_include_copy_source_snapshot_values() -> None:
    """The PackML idiom writes the index register only via
    ``copy(REF, idx)`` — no literals — so candidates must include the copy
    sources' snapshot values, or the chase is blind on exactly the
    template shape (probe20: ``S_StateRequested`` ← ``sm__STATE*REF``)."""
    from pyrung import calc
    from pyrung.core.memory_block import Block
    from pyrung.core.tag import TagType

    blk = Block("JT4", TagType.INT, 1, 30)
    blk.slot(18, default=7)  # table slot 18 holds the goal value
    Ref = Int("Ref", default=16)  # commissioned config: idx can take 16
    Idx = Int("Idx")
    Scratch = Int("Scratch")
    Arm = Bool("Arm", external=True)
    Go = Bool("Go", external=True)
    Cur = Int("Cur")
    Hit = Bool("Hit")

    with Program() as prog:
        with Rung(Arm):
            copy(Ref, Idx)
        with Rung():
            calc(Idx + 2, Scratch)
        with Rung(Go):
            copy(blk[Scratch], Cur)
        with Rung(Cur == 7):
            out(Hit)

    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)

    # Goal Cur == 7 ⇒ slot 18 ⇒ Scratch == 18 ⇒ Idx == 16, available only
    # as Ref's snapshot value (Idx has no literal writers).
    prereqs = _unsatisfied_conditions(
        "Cur",
        7,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    assert ("Idx", 16) in prereqs

    path = plc.how(Hit)
    assert path.reachable
    patches: dict = {}
    for step in path.steps:
        if step.action:
            patches.update(step.action)
    assert patches.get("Arm") is True
    assert patches.get("Go") is True


def test_multiple_inverting_values_become_alternative_groups() -> None:
    """Two table slots holding the goal value: each inverting index value
    rides in its own per-writer group (the register holds one value at a
    time — alternatives, never conjuncts), and the union carries exactly
    one binding."""
    from pyrung.core.analysis.walk.priors import _unsatisfied_condition_groups
    from pyrung.core.memory_block import Block
    from pyrung.core.tag import TagType

    blk = Block("JT2", TagType.INT, 1, 10, default_factory=lambda a: 44 if a in (3, 7) else a)
    Ptr = Int("Ptr", default=1)
    SelA = Bool("SelA", external=True)
    SelB = Bool("SelB", external=True)
    Go = Bool("Go", external=True)
    Cur = Int("Cur")
    Hit = Bool("Hit")

    with Program() as prog:
        with Rung(SelA):
            copy(3, Ptr)
        with Rung(SelB):
            copy(7, Ptr)
        with Rung(Go):
            copy(blk[Ptr], Cur)
        with Rung(Cur == 44):
            out(Hit)

    plc = PLC(prog, dt=0.010)
    plc.step()
    pdg = build_program_graph(plc._program)

    union, groups = _unsatisfied_condition_groups(
        "Cur",
        44,
        dict(plc.state.tags),
        pdg,
        plc._program,
        known=plc._known_tags_by_name,
    )
    assert [p for p in union if p[0] == "Ptr"] == [("Ptr", 3)]
    binding_groups = [g for g in groups if any(t == "Ptr" for t, _v in g)]
    assert {v for g in binding_groups for t, v in g if t == "Ptr"} == {3, 7}
    for g in binding_groups:
        assert sum(1 for t, _v in g if t == "Ptr") == 1


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
