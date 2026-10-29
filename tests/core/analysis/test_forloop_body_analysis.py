"""Static analysis of forloop body rungs, break_loop() and continue_loop()."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    Timer,
    branch,
    break_loop,
    calc,
    call,
    copy,
    count_up,
    forloop,
    latch,
    on_delay,
    out,
    reset,
    return_early,
    subroutine,
)
from pyrung.core.analysis.causal._rung_writes import build_scan_rung_write_projection
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.theory_orientation import _counterfactual_boundary
from pyrung.core.analysis.prove import Counterexample, Intractable, always
from pyrung.core.analysis.return_guards import return_early_guard_exprs
from pyrung.core.context import RungId
from pyrung.core.intrascan_counterfactual import (
    CounterfactualPatch,
    OccurrenceBoundary,
    UnaddressableBoundaryError,
    execute_counterfactual_program,
)
from pyrung.core.validation._common import _collect_write_sites
from pyrung.core.validation.stuck_bits import _loop_exit_skippable, validate_stuck_bits

Enable = Bool("Enable")
Gate = Bool("Gate")
Stop = Bool("Stop")
Rst = Bool("Rst")
Y = Bool("Y")
A = Int("A")
Ctr = Counter.clone("Ctr")


def _body_rung_program() -> Program:
    with Program() as logic:
        with Rung(Enable):
            with forloop(3):
                calc(A + 1, A)
                with Rung(Stop):
                    break_loop()
                with Rung(Gate):
                    out(Y)
    return logic


def test_pdg_node_reads_body_rung_contacts():
    graph = build_program_graph(_body_rung_program())
    node = graph.rung_nodes[0]
    assert {"Enable", "Stop", "Gate"} <= node.condition_reads
    # The body-rung out() follows its own contact, not the loop rung's.
    assert "Y" in node.writes
    assert "Y" not in node.ote_writes
    assert 0 in graph.readers_of["Gate"]


def test_access_events_place_body_rung_reads_in_body_order():
    graph = build_program_graph(_body_rung_program())
    # Gate is read by the loop rung's node before anything writes it.
    chain = graph.def_use_chains["Gate"]
    assert chain[0].defined_at is None
    assert 0 in chain[0].read_by


def test_write_sites_carry_body_rung_contacts():
    sites = [s for s in _collect_write_sites(_body_rung_program()) if s.target_name == "Y"]
    assert len(sites) == 1
    names = {repr(c) for c in sites[0].conditions}
    assert any("Enable" in n for n in names)
    assert any("Gate" in n for n in names)


def test_loop_exit_makes_every_loop_instruction_unprovable_for_stuck_bits():
    logic = _body_rung_program()
    skippable = _loop_exit_skippable(logic)
    loop = logic.rungs[0]._instructions[0]
    assert {id(i) for i in loop.instructions} <= skippable


def test_bare_loop_out_is_not_a_level_follower():
    # A false For rung skips its body (CLICK), so the coil holds its value.
    with Program() as logic:
        with Rung(Enable):
            with forloop(2):
                out(Y)

    node = build_program_graph(logic).rung_nodes[0]
    assert "Y" in node.writes
    assert "Y" not in node.ote_writes


def test_loop_count_tag_gates_the_body():
    # A count of 0 runs no iteration, so the count joins the node's guard.
    with Program() as logic:
        with Rung():
            with forloop(A):
                out(Y)

    graph = build_program_graph(logic)
    assert "A" in graph.rung_nodes[0].condition_reads
    assert not graph.unconditional_write_before_read("Y")


def test_prover_sees_a_loop_coil_hold_while_the_loop_rung_is_false():
    lamp = Bool("Lamp")
    with Program(strict=False) as logic:
        with Rung(Enable):
            with forloop(2):
                out(Y)
        with Rung(~Enable, Y):
            out(lamp)

    assert isinstance(always(logic, ~lamp), Counterexample)


def test_prover_sees_a_zero_count_skip_the_write():
    count = Int("Count", external=True, min=0, max=1)
    value = Bool("Value", external=True)
    kept = Bool("Kept")
    stale = Bool("Stale")
    with Program(strict=False) as logic:
        with Rung():
            with forloop(count):
                copy(value, kept)
        with Rung(kept, ~value):
            out(stale)

    assert isinstance(always(logic, ~stale), Counterexample)


def test_stuck_bits_reports_a_loop_coil_whose_loop_rung_can_be_false():
    with Program(strict=False) as logic:
        with Rung(Enable):
            with forloop(2):
                out(Y)

    findings = [f for f in validate_stuck_bits(logic).findings if f.target_name == "Y"]
    assert [f.kind for f in findings] == ["high"]


def test_stuck_bits_accepts_a_loop_coil_on_an_unconditional_loop():
    with Program(strict=False) as logic:
        with Rung():
            with forloop(2):
                out(Y)

    assert not [f for f in validate_stuck_bits(logic).findings if f.target_name == "Y"]


@pytest.mark.parametrize("body_rung", [False, True])
def test_stuck_bits_reports_a_loop_coil_on_an_unconditional_oneshot_loop(body_rung: bool):
    # A oneshot loop runs its body on the rung's rising edge only: on a
    # contactless rung, the first scan.  Y is evaluated then, and holds after.
    with Program(strict=False) as logic:
        with Rung():
            with forloop(2, oneshot=True):
                if body_rung:
                    with Rung(Gate):
                        out(Y)
                else:
                    out(Y)

    findings = [f for f in validate_stuck_bits(logic).findings if f.target_name == "Y"]
    assert [f.kind for f in findings] == ["high"]


def test_return_inside_body_rung_guards_later_rungs():
    with Program() as logic:
        with subroutine("work"):
            with Rung(Enable):
                with forloop(2):
                    with Rung(Gate):
                        return_early()
            with Rung():
                out(Y)
        with Rung():
            call("work")

    graph = build_program_graph(logic)
    node = next(n for n in graph.rung_nodes if n.subroutine == "work" and n.rung_index == 1)
    # The return fires only when the loop rung and the body rung both hold.
    guards = return_early_guard_exprs(logic, node)
    assert len(guards) == 1
    rendered = repr(guards[0])
    assert "Enable" in rendered and "Gate" in rendered
    assert {"Enable", "Gate"} <= node.guard_reads


def test_prover_names_body_rungs_as_intractable():
    with Program() as logic:
        with Rung(Enable):
            with forloop(3):
                with Rung(Gate):
                    calc(A + 1, A)
        with Rung(A > 5):
            out(Y)

    result = always(logic, ~Y)
    assert isinstance(result, Intractable)
    assert "forloop body rung" in result.reason


def test_prover_still_models_a_bare_loop():
    with Program() as logic:
        with Rung(Enable):
            with forloop(3):
                count_up(Ctr, 30).reset(Rst)

    result = always(logic, ~Ctr.Done, max_states=1000)
    assert isinstance(result, Counterexample)
    # Three counts per scan: ten enabled scans reach the preset.
    assert sum(step.scans for step in result.trace) == 10


def test_how_keeps_its_prover_context_with_body_rungs():
    from pyrung.core.analysis.pilot.drive_setup import build_prover_context

    with Program() as logic:
        with Rung(Enable):
            with forloop(3):
                with Rung(Stop):
                    break_loop()
                with Rung(Gate):
                    latch(Y)
        with Rung(Rst):
            reset(Y)
        with Rung(Y):
            out(Bool("Lamp"))
    plc = PLC(logic)
    prover = build_prover_context(logic, dict(plc.state.tags))
    assert prover.nd_domains
    assert prover.key_config is not None


@pytest.mark.parametrize("conditioned", [False, True])
def test_how_counts_a_counter_inside_a_loop(conditioned: bool):
    # PILOT-GOTCHAS X7: body instructions had no enable condition, so the
    # counter's advance profile asked for no hold and how() stopped.
    with Program(strict=False) as logic:
        with Rung(Enable):
            with forloop(3):
                if conditioned:
                    with Rung(Gate):
                        count_up(Ctr, 30).reset(Rst)
                else:
                    count_up(Ctr, 30).reset(Rst)

    plan = PLC(logic).how(Ctr.Done)
    assert plan.status.name == "REACHED"


def test_timer_in_body_rung_folds_like_stepping():
    timer = Timer.clone("LoopT")
    done = Bool("LoopDone")

    def build() -> Program:
        with Program() as logic:
            with Rung(Enable):
                with forloop(2):
                    with Rung(Gate):
                        on_delay(timer, 500)
            with Rung(timer.Done):
                out(done)
        return logic

    folded = PLC(build(), dt=0.010)
    stepped = PLC(build(), dt=0.010)
    for plc in (folded, stepped):
        plc.patch({"Enable": True, "Gate": True})
    folded.run_for(2.0)
    for _ in range(200):
        stepped.step()
    assert folded.current_state.scan_id == stepped.current_state.scan_id
    assert dict(folded.current_state.tags) == dict(stepped.current_state.tags)


def _loop_in_branch_program() -> Program:
    with Program() as logic:
        with Rung(Enable):
            with branch(Gate):
                with forloop(2):
                    with Rung(Stop):
                        out(Y)
    return logic


def test_body_rung_writes_carry_the_branch_that_holds_the_loop():
    logic = _loop_in_branch_program()
    plc = PLC(logic)
    plc.patch({"Enable": True, "Gate": True, "Stop": True})
    plc.step()
    scan_id = plc.history.scan_ids()[-1]
    projection = build_scan_rung_write_projection(
        plc.history, scan_id, plc._replay_rung_runs_at(scan_id)
    )
    assert projection is not None
    body_writes = [write for write in projection.writes if write.occurrence.name == "Y"]
    assert len(body_writes) == 2
    assert {write.branch_path for write in body_writes} == {(0,)}
    # The program graph folds the body rung into that branch's node.
    (writer,) = [node for node in build_program_graph(logic).rung_nodes if "Y" in node.writes]
    assert writer.branch_path == (0,)


def test_counterfactual_refuses_a_body_rung_boundary():
    # Every body rung of a loop, on every iteration, shares this boundary.
    logic = _loop_in_branch_program()
    plc = PLC(logic)
    patch = CounterfactualPatch(
        "A",
        1,
        Enable,
        OccurrenceBoundary(
            rung_id=RungId(None, 0),
            execution_kind="loop_body",
            caller_rung=0,
            call_stack=(),
            depth=2,
            call_invocation=None,
            branch_path=(0,),
        ),
    )
    ctx, _dt = plc._prepare_scan()
    with pytest.raises(UnaddressableBoundaryError):
        execute_counterfactual_program(logic, ctx, (patch,))


@pytest.mark.parametrize(("kind", "addressable"), [("rung", True), ("loop_body", False)])
def test_pilot_asks_no_counterfactual_at_a_body_rung_read(kind: str, addressable: bool):
    read = SimpleNamespace(
        kind="read",
        execution_kind=kind,
        rung=(None, 0),
        caller_rung=0,
        call_stack=(),
        depth=1,
        call_invocation=None,
        run_order=3,
        branch_path=(),
    )
    assert (_counterfactual_boundary(read) is not None) is addressable
