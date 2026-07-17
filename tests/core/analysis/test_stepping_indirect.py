"""Stepping classification must follow an indirect copy over a CONSTANT table.

The prover's ``_compute_stepping_tags`` copy-couples "stepping" (a tag actively
cycling through values) from a source tag to its copy destination.  A real
Click PackML program writes its state register through a jump-table *indirect*
copy — ``copy(JumpTable[StateRequested], State)`` — whose source is an
``IndirectRef``, not a named tag.  Before the fix that dropped ``State`` from
the stepping set: ``infer_pipeline_roles`` found no channel role, no compass
value-graph was built, and ``how()`` on such a target dead-ended instantly with
``no_candidates``.

The coupling is sound only when the table is *constant* (never written): then
``dest = table[index]`` is a pure function of the index, so ``dest`` steps iff
the index steps.  A writable table breaks that, so the fix must punt — the
negative test pins that.

Scope: the state register is written ONLY by the indirect jump-table copy over
a constant identity table.  Generic names, minimal shape (mirrors
``test_pilot_table_detour.py`` but smaller — a single constant enable table
stands in for the full mask/predicate layer).
"""

from __future__ import annotations

from pyrung import (
    PLC,
    Block,
    Bool,
    Int,
    Program,
    TagType,
    calc,
    copy,
    rung,
)

# Minimal three-state machine.  IDLE=0 so a nonzero StateRequested means
# "transition pending" (the apply rung's gate).
IDLE, RUN, DONE = 0, 1, 2
START_CMD = 2
MASK_BASE = 10  # constant-mask table offset

STATE_CHOICES = {0: "Idle", 1: "Run", 2: "Done"}


def _indirect_state_program(*, table_write: str | None) -> tuple[Program, dict[str, object]]:
    """State register ridden through a jump-table indirect copy.

    Two constant tables, both read by indirect copies:

    * a **jump table** ``JT[StateRequested]`` (identity) whose indirect copy is
      the state register's ONLY writer — the shape that used to drop ``State``
      from the prover's stepping set; and
    * a constant **enable table** ``MT[MASK_BASE + StateRequested]`` (all 1,
      always enabled) whose indirect copy gives ``detect_opaque_loop`` a second
      target so the self-looping state register lands in the opaque loop (a lone
      self-loop is discarded by ``upstream_slice``, exactly as the sibling
      table-detour fixture arms it via its mask copies).

    ``table_write`` poisons the jump table's constant-ness:

    * ``None`` — nothing written, table constant.
    * ``"inside"`` — a slot INSIDE the index's reachable region (``JT[1]``,
      StateRequested reaches 1) is written → coupling must punt.
    * ``"outside"`` — a slot OUTSIDE the reachable region (``JT[5]``,
      StateRequested only reaches 0..2) is written → the region is still
      constant, so coupling must still fire.
    """
    C_Start = Bool("Ind_C_Start", external=True)
    Scribble = Bool("Ind_Scribble", external=True)

    Cmd = Int("Ind_Cmd")
    CmdReq = Int("Ind_CmdReq")
    State = Int("Ind_State", default=IDLE, choices=STATE_CHOICES)
    StateRequested = Int("Ind_StateRequested")
    MaskIdx = Int("Ind_MaskIdx")
    EnblYes = Int("Ind_EnblYes")

    # Constant identity jump table JT[i] = i.  Range extends past the reachable
    # index region (0..2) so an out-of-region write (JT[5]) is expressible.
    JT = Block("Ind_JT", TagType.INT, 0, 5)
    for i in range(6):
        JT.slot(i, default=i)
    # Constant enable table — every slot 1 (always enabled).
    MT = Block("Ind_MT", TagType.INT, MASK_BASE, MASK_BASE + 3)
    for i in range(4):
        MT.slot(MASK_BASE + i, default=1)

    with Program(strict=False) as logic:
        # operator command producer
        with rung(C_Start):
            copy(START_CMD, Cmd)
            copy(1, CmdReq)

        # command -> state request (gated by the current state)
        with rung(CmdReq == 1, Cmd == START_CMD, State == IDLE):
            copy(RUN, StateRequested)
        # auto self-advance (gives StateRequested a second literal -> stepping)
        with rung(State == RUN):
            copy(DONE, StateRequested)

        # constant-table enable (indirect copy #2 — arms detect_opaque_loop)
        with rung():
            calc(StateRequested + MASK_BASE, MaskIdx)
        with rung():
            copy(MT[MaskIdx], EnblYes)

        if table_write == "inside":
            # Write a slot the index actually reaches → dest is not a pure
            # function of the index over its region, so coupling must punt.
            with rung(Scribble):
                copy(99, JT[1])
        elif table_write == "outside":
            # Write a slot the index never reaches → its region stays constant,
            # so coupling must still fire (this is the real ds-bank shape: a
            # shared bank where unrelated slots are written).
            with rung(Scribble):
                copy(99, JT[5])

        # --- the indirect jump-table hop: State's ONLY writer ---
        # StateRequested is the pointer directly (identity table), so
        # State <- JT[StateRequested] == StateRequested.
        with rung(StateRequested != 0, EnblYes == 1):
            copy(JT[StateRequested], State)
            copy(0, StateRequested)
            copy(0, Cmd)
            copy(0, CmdReq)

    tags: dict[str, object] = {
        "C_Start": C_Start,
        "State": State,
        "StateRequested": StateRequested,
        "Idle": IDLE,
        "Run": RUN,
        "Done": DONE,
    }
    return logic, tags


def test_stepping_couples_through_constant_indirect_table() -> None:
    """(a) prover stepping set, (b) opaque loop, (c) channel role, (d) how()
    no longer dead-ends at ``no_candidates`` — all for a state register written
    only by an indirect copy over a constant jump table."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.charts import detect_opaque_loop
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.pilot import _build_pilot_context, pilot_events
    from pyrung.core.analysis.prove.classify import _compute_stepping_tags
    from pyrung.core.analysis.steerable import compute_steerable

    logic, tags = _indirect_state_program(table_write=None)
    plc = PLC(logic, dt=0.010)
    pdg = build_program_graph(logic)
    state_name = tags["State"].name
    req_name = tags["StateRequested"].name

    # (a) The prover classifies the state register as stepping, coupled through
    #     the constant-table indirect copy to its (stepping) index.
    stepping = _compute_stepping_tags(logic, pdg)
    assert req_name in stepping, sorted(stepping)
    assert state_name in stepping, sorted(stepping)

    # (b) The indirect jump-table hop puts the state register in the opaque loop.
    opaque_loop = detect_opaque_loop(pdg, logic)
    assert state_name in opaque_loop, sorted(opaque_loop)

    # The pilot-facing consumer agrees (is_stepping is what engages the compass).
    _nd, _key, evidence, _sem = _build_pilot_context(logic, dict(plc.state.tags))
    assert evidence is not None
    assert evidence.is_stepping(state_name)

    # (c) The StateRequested -> State transition pipeline yields a channel role.
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    role = infer_pipeline_roles(state_name, pdg, logic, steerable, opaque_loop, evidence)
    assert role.channel_tag == state_name
    assert req_name in role.request_tags

    # (d) The failure mode changed: the compass engages and builds a nonempty
    #     candidate list instead of dead-ending at ``no_candidates``.
    events = list(pilot_events(plc, tags["State"] == tags["Run"], max_scans=60))
    built = [ev for ev in events if ev.kind == "candidates_built"]
    assert any(ev.data["candidates"] for ev in built), [
        (ev.kind, ev.data.get("stuck_reason"))
        for ev in events
        if ev.kind in ("candidates_built", "stuck")
    ]


def test_stepping_punts_when_indirect_region_is_writable() -> None:
    """Negative: when a slot INSIDE the index's reachable region is written, the
    coupling is unsound (dest is no longer a pure function of the index over its
    region), so stepping does NOT couple through the indirect copy."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import _compute_stepping_tags

    logic, tags = _indirect_state_program(table_write="inside")
    pdg = build_program_graph(logic)
    state_name = tags["State"].name
    req_name = tags["StateRequested"].name

    stepping = _compute_stepping_tags(logic, pdg)
    # The index register still steps on its own (literal writes)...
    assert req_name in stepping, sorted(stepping)
    # ...but the destination does NOT inherit it through a writable region.
    assert state_name not in stepping, sorted(stepping)


def test_stepping_couples_when_only_out_of_region_slot_is_written() -> None:
    """A write to a slot the index NEVER reaches must not defeat the coupling —
    real jump tables share a ``ds`` bank with unrelated written slots, so the
    never-written test is bounded by the index's reachable region, not the whole
    block.  Without this bound the fix would never fire on real machines."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import _compute_stepping_tags

    logic, tags = _indirect_state_program(table_write="outside")
    pdg = build_program_graph(logic)
    state_name = tags["State"].name

    stepping = _compute_stepping_tags(logic, pdg)
    assert state_name in stepping, sorted(stepping)
