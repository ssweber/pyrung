"""Exact occurrence-scoped counterfactual execution."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, branch, call, copy, subroutine
from pyrung.core.context import RungId
from pyrung.core.executor import ConditionViewCapture
from pyrung.core.intrascan_counterfactual import (
    CounterfactualPatch,
    OccurrenceBoundary,
    execute_counterfactual_program,
)
from pyrung.core.synthesis import Synthesis, copy_hold_rung


def _program(*, calls: int = 1) -> tuple[Program, Bool, Int]:
    link = Bool("RouteLink", external=True)
    step = Int("RouteStep", default=20)

    @subroutine("route_errors")
    def route_errors() -> None:
        with Rung(~link, step <= 20):
            copy(98, step)
        with Rung(link, step == 98):
            copy(10, step)

    with Program(strict=False) as program:
        for _ in range(calls):
            with Rung():
                call(route_errors)
    return program, link, step


def _boundary(*, caller_rung: int = 0, call_invocation: int = 0) -> OccurrenceBoundary:
    return OccurrenceBoundary(
        rung_id=RungId("route_errors", 1),
        execution_kind="subroutine",
        caller_rung=caller_rung,
        call_stack=("route_errors",),
        depth=1,
        call_invocation=call_invocation,
    )


def _execute_disposable(
    plc: PLC,
    patches: tuple[CounterfactualPatch, ...],
    *,
    capture: ConditionViewCapture | None = None,
):
    ctx, dt = plc._prepare_scan(synthesis_observer=capture)
    assert plc.program is not None
    receipt = execute_counterfactual_program(plc.program, ctx, patches, capture=capture)
    result = ctx.commit(dt=dt)
    if capture is not None:
        capture.exit_tags = result.tags
    return result, receipt


def test_counterfactual_patch_proves_one_later_consumer_could_complete() -> None:
    program, link, step = _program()
    plc = PLC(program)
    plc._synthesis = Synthesis(holds=[copy_hold_rung(value=False, dest=link)])
    patch = CounterfactualPatch(link.name, True, ~link, _boundary())

    hypothetical, receipt = _execute_disposable(plc, (patch,))

    assert hypothetical.tags[step.name] == 10
    assert hypothetical.tags[link.name] is True
    assert receipt.applied_exactly_once(patch)
    assert tuple(
        (item.run_order, item.before, item.after) for item in receipt.applications_for(patch)
    ) == ((2, False, True),)
    # Counterfactual execution cannot become ordinary runner history or state.
    assert plc.state.scan_id == 0
    assert plc.state.tags[step.name] == 20
    assert plc.state.tags[link.name] is False


def test_counterfactual_patch_is_an_exact_precondition_write_in_its_witness() -> None:
    program, link, step = _program()
    plc = PLC(program)
    plc._synthesis = Synthesis(holds=[copy_hold_rung(value=False, dest=link)])
    capture = ConditionViewCapture()

    (result, receipt) = _execute_disposable(
        plc,
        (CounterfactualPatch(link.name, True, ~link, _boundary()),),
        capture=capture,
    )

    first, second = tuple(run for run in capture.runs if run.rung_id.subroutine == "route_errors")
    application = receipt.applications[0]
    assert application.run_order == next(
        index for index, run in enumerate(capture.runs) if run is second
    )
    assert first.view.get_tag(link.name) is False
    assert second.view.get_tag(link.name) is True
    assert tuple(
        (write.before, write.after)
        for write in second.direct_write_occurrences
        if write.name == link.name
    ) == ((False, True),)
    assert result.tags[step.name] == 10
    assert receipt.applied_exactly_once(receipt.applications[0].patch)


def test_counterfactual_patch_uses_exact_call_invocation() -> None:
    program, link, step = _program(calls=2)
    plc = PLC(program)
    plc._synthesis = Synthesis(holds=[copy_hold_rung(value=False, dest=link)])
    patch = CounterfactualPatch(
        link.name,
        True,
        ~link,
        _boundary(caller_rung=1, call_invocation=1),
    )

    hypothetical, receipt = _execute_disposable(plc, (patch,))

    assert hypothetical.tags[step.name] == 10
    assert receipt.applied_exactly_once(patch)


def test_unmatched_counterfactual_boundary_is_explicitly_not_a_witness() -> None:
    program, link, _step = _program()
    plc = PLC(program)
    patch = CounterfactualPatch(
        link.name,
        True,
        ~link,
        _boundary(call_invocation=99),
    )

    _result, receipt = _execute_disposable(plc, (patch,))

    assert receipt.applications_for(patch) == ()
    assert not receipt.applied_exactly_once(patch)


def test_counterfactual_patch_targets_one_exact_sibling_branch_snapshot() -> None:
    link = Bool("BranchLink", default=True)
    step = Int("BranchStep", default=40)

    with Program(strict=False) as program:
        with Rung(link):
            with branch(step == 98):
                copy(10, step)
            with branch(step <= 20):
                copy(94, step)

    plc = PLC(program)
    capture = ConditionViewCapture()
    patch = CounterfactualPatch(
        step.name,
        98,
        step != 98,
        OccurrenceBoundary(
            rung_id=RungId(None, 0),
            execution_kind="branch",
            caller_rung=0,
            call_stack=(),
            depth=1,
            call_invocation=None,
            # The historical execution coordinate may move when earlier
            # control flow changes; the static branch path is relocatable.
            run_order=999,
            branch_path=(0,),
        ),
    )

    result, receipt = _execute_disposable(plc, (patch,), capture=capture)

    branches = tuple(run for run in capture.runs if run.kind == "branch")
    assert receipt.applied_exactly_once(patch)
    assert tuple(run.view.get_tag(step.name) for run in branches) == (98, 98)
    assert result.tags[step.name] == 10
