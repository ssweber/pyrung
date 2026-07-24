"""PILOT decision-rationale recordings (recording only — zero behavior change).

Two rich decisions PILOT used to compute and throw away are now carried on the
event stream and on :class:`Plan`:

1. **Writer-ranking rationale** — the traced node stashes the FULL ``_rank_writers``
   ordering (winner + losers, each with its availability/bucket/clobber) plus the
   ranked writers the walk actively skipped.
2. **Knowledge onto Plan** — ``journey`` / ``hold_log`` / ``lever_notes`` /
   ``avoid_names`` are threaded off the drive's ``_PilotState`` onto the returned
   :class:`Plan`.
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    copy,
    on_delay,
    out,
)
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot._ops import PilotRung
from pyrung.core.analysis.pilot.recording import _build_plan_journal
from pyrung.core.analysis.pilot.types import (
    MotionKind,
    _CommittedAct,
    _HoldLogEntry,
    _Step,
    _StepContext,
)


def _all_nodes(tree):
    """Breadth-first walk over a TraceNode tree."""
    out_nodes = [tree]
    i = 0
    while i < len(out_nodes):
        out_nodes.extend(out_nodes[i].children)
        i += 1
    return out_nodes


def test_edge_operation_journal_uses_owned_pulse_not_release() -> None:
    """One edge act owns both physical steps and one semantic recording."""
    release = _Step(inputs={"Cmd": False}, scan_before=10, scan_after=11)
    pulse = _Step(inputs={"Cmd": True}, scan_before=11, scan_after=13)
    act = _CommittedAct(
        steps=(release, pulse),
        context=_StepContext(
            candidate={"Cmd": True},
            motion=MotionKind.INTERVENTION,
        ),
    )
    state = SimpleNamespace(
        committed_acts=(act,),
        lever_notes={},
        hold_log=(),
        correction_receipts=(),
    )

    journal = _build_plan_journal(state, None, frozenset(), frozenset())

    assert len(journal) == 1
    assert journal[0].kind == "pulse"
    assert journal[0].scan == 10
    assert journal[0].scans == 3
    assert journal[0].inputs == (("Cmd", True),)


def test_plan_manual_edit_is_hidden_only_by_matching_effective_owner() -> None:
    """Dormant and overwritten rules cannot claim a command input."""
    In = Bool("JournalOwnerIn", external=True)
    Scope = Bool("JournalOwnerScope", external=True)
    dormant = PilotRung(In.name, True, Scope)
    eligible = PilotRung(In.name, True, ~Scope)
    effective = PilotRung(In.name, False, ~Scope)

    def _journal(value: bool):
        step = _Step(inputs={In.name: value}, scan_before=10, scan_after=11)
        state = SimpleNamespace(
            committed_acts=(
                _CommittedAct(
                    steps=(step,),
                    context=_StepContext(
                        candidate={In.name: value},
                        motion=MotionKind.INTERVENTION,
                        before_snap={Scope.name: False, In.name: False},
                    ),
                ),
            ),
            lever_notes={},
            hold_log=(_HoldLogEntry(1, "investigation", (dormant, eligible, effective)),),
            correction_receipts=(),
        )
        return _build_plan_journal(state, None, frozenset(), frozenset())

    assert [step.inputs for step in _journal(True) if step.kind == "pulse"] == [((In.name, True),)]
    assert not [step for step in _journal(False) if step.kind == "pulse"]


# ---------------------------------------------------------------------------
# Recording 1 — candidate payload reports provenance, not a second policy score
# ---------------------------------------------------------------------------


def test_candidates_built_payload_carries_provenance_without_policy_scores() -> None:
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")
    with Program() as logic:
        with Rung(x_Go):
            out(y_Out)

    plc = PLC(logic, dt=0.010)
    built: list = []
    plan = pilot_how(
        plc,
        y_Out,
        on_event=lambda ev: built.append(ev) if ev.kind == "candidates_built" else None,
    )
    assert plan.reachable, plan.reason

    seen_candidate = False
    for ev in built:
        for cand in ev.data["candidates"]:
            seen_candidate = True
            assert set(("provenance", "wake", "prescribed")) <= set(cand)
            assert not {"avail_tier", "over_wake", "compass_score", "scored"} & set(cand)
    assert seen_candidate


# ---------------------------------------------------------------------------
# Recording 2 — writer-ranking rationale on the traced node
# ---------------------------------------------------------------------------


def _multi_writer_program() -> tuple[Program, Int]:
    """A single value (``Req == 6``) produced by two guarded writers — a Start-gated
    one whose guard is satisfied from cold, and an Unhold-gated one whose guard is
    not.  ``_rank_writers`` ranks both; only one is chosen."""
    Start = Bool("Start", external=True)
    Unhold = Bool("Unhold", external=True)
    Src = Int("Src", default=4)  # IDLE — the Start writer's guard holds from cold
    Req = Int("Req")
    State = Int("State", default=4)

    with Program(strict=False) as prog:
        with Rung(Start, Src == 4):
            copy(6, Req)
        with Rung(Unhold, Src == 11):
            copy(6, Req)
        with Rung(Req == 6):
            copy(6, State)

    return prog, State


def test_writer_ranking_names_winner_and_losers() -> None:
    prog, State = _multi_writer_program()
    plc = PLC(prog, dt=0.010)

    trees: list = []
    plc.how(
        State == 6,
        on_event=lambda ev: trees.append(ev.data["tree"]) if ev.kind == "iteration" else None,
    )
    assert trees, "expected at least one iteration event carrying a tree"

    # Find the two-writer node (Req == 6) and check its full ranking.
    ranked_nodes = [
        n
        for tree in trees
        for n in _all_nodes(tree)
        if n.writer_ranking is not None and len(n.writer_ranking) >= 2
    ]
    assert ranked_nodes, "expected a node whose writer_ranking names winner + losers"

    node = ranked_nodes[0]
    ranking = node.writer_ranking
    # Winner is first in the ranking and is the writer actually chosen.
    assert ranking[0].ri == node.writer_rung
    # Losers are present (more than the winner) with their sort dimensions recorded.
    assert len(ranking) >= 2
    assert len({rank.ri for rank in ranking}) == len(ranking), "distinct writers"
    for rank in ranking:
        assert isinstance(rank.ri, int)
        assert isinstance(rank.bucket, int)
        assert isinstance(rank.clobber, int)
        assert rank.availability is not None


def test_writer_skips_records_avoid_shadowed() -> None:
    """A ranked writer skipped because its subtree forces the avoided predicate is
    named ``avoid_shadowed`` in ``writer_skips`` — the silent avoid-fallback
    decision the ranker/loop used to lose."""
    Cmd = Bool("SkCmd", external=True)
    Alt = Bool("SkAlt", external=True)
    Step = Int("SkStep", default=1)
    Filling = Bool("SkFilling")
    with Program(strict=False) as prog:
        # Two writers of Step==2: the Cmd-gated one (avoided) and the Alt-gated one.
        with Rung(Cmd):
            copy(2, Step)
        with Rung(Alt):
            copy(2, Step)
        with Rung(Step == 2):
            out(Filling)

    plc = PLC(prog, dt=0.010)
    trees: list = []
    plc.how(
        Filling,
        avoid=Cmd,
        on_event=lambda ev: trees.append(ev.data["tree"]) if ev.kind == "iteration" else None,
    )
    skips = [skip for tree in trees for n in _all_nodes(tree) for skip in n.writer_skips]
    assert any(reason == "avoid_shadowed" for _ri, reason in skips), skips


# ---------------------------------------------------------------------------
# Recording 3 — Knowledge threaded onto Plan
# ---------------------------------------------------------------------------


def test_plan_carries_journey_on_reachable_target() -> None:
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")
    with Program() as logic:
        with Rung(x_Go):
            out(y_Out)

    plc = PLC(logic, dt=0.010)
    plan = pilot_how(plc, y_Out)
    assert plan.reachable, plan.reason
    assert len(plan.journey) >= 1, "reachable Plan should carry a non-empty journey"
    # The new Knowledge fields exist with sane defaults.
    assert isinstance(plan.hold_log, tuple)
    assert isinstance(plan.lever_notes, dict)
    assert isinstance(plan.avoid_names, tuple)


def _momentary_no_alternate() -> tuple[Program, Bool, Bool]:
    """``Filling`` reachable only by pressing the momentary command ``Cmd`` — no
    alternate route, so ``avoid=Cmd`` declines and names it."""
    Cmd = Bool("Cmd", external=True)
    Step = Int("Step", default=1)
    Filling = Bool("Filling")
    with Program(strict=False) as prog:
        with Rung(Cmd):
            copy(2, Step)
        with Rung(Step == 2):
            out(Filling)
    return prog, Filling, Cmd


def test_plan_carries_avoid_names_on_declined_run() -> None:
    prog, Filling, Cmd = _momentary_no_alternate()
    plc = PLC(prog, dt=0.010)
    plan = plc.how(Filling, avoid=Cmd, max_scans=1000)
    assert not plan.reachable
    # The avoid-decline evidence is now on the Plan, not only in the reason string.
    assert "Cmd" in plan.avoid_names, plan.avoid_names


def test_plan_reachable_run_leaves_avoid_names_empty() -> None:
    """A clean reachable drive with no avoid predicate carries no avoid evidence —
    the recording is faithful, not fabricated."""
    Cmd = Bool("Cmd2", external=True)
    Done = Bool("Done2")
    Dwell = Timer.clone("Dwell2")
    with Program(strict=False) as prog:
        with Rung(Cmd):
            on_delay(Dwell, 20, "ms")
        with Rung(Dwell.Done):
            out(Done)

    plc = PLC(prog, dt=0.010)
    plan = pilot_how(plc, Done, max_scans=1000)
    assert plan.reachable, plan.reason
    assert plan.avoid_names == ()
