from __future__ import annotations

from types import SimpleNamespace

from devtools.pilot_wip_dark_run import (
    ShadowOption,
    WorkEvidence,
    current_work_evidence,
    run_dark_drive,
    select_shadow_option,
)
from pyrung import PLC, And, Bool, Or, Program, latch, out, rung
from pyrung.core.analysis.pilot.trace import TraceChoice, TraceNode


def _option(
    name: str,
    *,
    work: tuple[str, ...] = (),
    hard_class: str = "action",
    ordinary: tuple[int, ...] = (0,),
) -> ShadowOption:
    return ShadowOption(
        identity=("pulse", ((name, True),)),
        act=name,
        source=name,
        route_label=name,
        evidence=WorkEvidence(work),
        hard_class=hard_class,
        ordinary_key=ordinary,
    )


def test_underway_work_beats_a_cheaper_fresh_action() -> None:
    fresh = _option("fresh", ordinary=(0,))
    underway = _option("underway", work=("established:Mode=True",), ordinary=(99,))

    assert select_shadow_option([fresh, underway]) is underway


def test_owned_operation_beats_unrelated_underway_action() -> None:
    owned = _option("owned", hard_class="owned", ordinary=(99,))
    underway = _option("underway", work=("held:Mode=True",), ordinary=(0,))

    assert select_shadow_option([underway, owned]) is owned


def test_current_held_fact_is_continuation_evidence() -> None:
    tree = TraceNode(
        "Target",
        True,
        children=[TraceNode("Mode", True, satisfied=True)],
    )
    state = SimpleNamespace(
        rungs=(SimpleNamespace(dest="Mode", value=True),),
        pending_departure=None,
        committed_acts=(),
        gauge=None,
    )

    evidence = current_work_evidence(tree, None, state, {"Mode": True})

    assert evidence.reasons == ("held:Mode=True",)


def test_last_current_world_operation_can_establish_the_route_fact() -> None:
    tree = TraceNode("Target", True, children=[TraceNode("Start", True)])
    choice = TraceChoice(
        id="manual",
        label="Manual",
        route=("Manual",),
        via_hint=("ManualMode", True),
    )
    state = SimpleNamespace(
        rungs=(),
        pending_departure=None,
        committed_acts=(
            SimpleNamespace(
                context=SimpleNamespace(
                    before_snap={"ManualMode": False},
                    after_snap={"ManualMode": True},
                )
            ),
        ),
        gauge=None,
        # A reverted journey entry would live here, but the reader must ignore it.
        journey=(SimpleNamespace(),),
    )

    evidence = current_work_evidence(
        tree,
        choice,
        state,
        {"ManualMode": True},
    )

    assert evidence.reasons == ("established:ManualMode=True",)


def test_clobbered_fact_is_no_longer_work_underway() -> None:
    tree = TraceNode("Target", True, children=[TraceNode("Mode", True)])
    state = SimpleNamespace(
        rungs=(),
        pending_departure=None,
        committed_acts=(
            SimpleNamespace(
                context=SimpleNamespace(
                    before_snap={"Mode": False},
                    after_snap={"Mode": True},
                )
            ),
        ),
        gauge=None,
    )

    evidence = current_work_evidence(tree, None, state, {"Mode": False})

    assert evidence.reasons == ()


def test_no_admissible_act_means_no_shadow_selection() -> None:
    assert select_shadow_option([]) is None


def test_dark_drive_does_not_change_baseline_result() -> None:
    CmdA = Bool("WipDark_CmdA", external=True)
    CmdB = Bool("WipDark_CmdB", external=True)
    ModeA = Bool("WipDark_ModeA")
    ModeB = Bool("WipDark_ModeB")
    Target = Bool("WipDark_Target")

    with Program() as logic:
        with rung(CmdA):
            latch(ModeA)
        with rung(CmdB):
            latch(ModeB)
        with rung(ModeA):
            latch(Target)
        with rung(ModeB):
            latch(Target)

    plain = PLC(logic).how(Target, max_scans=100)
    shadowed, observer = run_dark_drive(
        PLC(logic),
        Target,
        max_scans=100,
        strict=True,
    )

    assert shadowed.reachable == plain.reachable
    assert shadowed.changes == plain.changes
    assert observer.rows
    assert all("shadow_error" not in row for row in observer.rows)


def test_explicit_via_constrains_shadow_candidates() -> None:
    CmdA = Bool("WipVia_CmdA", external=True)
    CmdB = Bool("WipVia_CmdB", external=True)
    ModeA = Bool("WipVia_ModeA")
    ModeB = Bool("WipVia_ModeB")
    Target = Bool("WipVia_Target")

    with Program() as logic:
        with rung(CmdA):
            latch(ModeA)
        with rung(CmdB):
            latch(ModeB)
        with rung(ModeA):
            latch(Target)
        with rung(ModeB):
            latch(Target)

    plan, observer = run_dark_drive(
        PLC(logic),
        Target,
        via=ModeB,
        max_scans=100,
        strict=True,
    )

    assert plan.reachable
    first = observer.rows[0]
    assert first["shadow_identity"] == ("pulse", ((CmdB.name, True),))
    assert all(
        ("pulse", ((CmdA.name, True),)) != tuple(candidate["identity"])
        for candidate in first["candidates"]
    )


def test_production_surfaces_productive_current_world_sibling() -> None:
    AutoUpPermissive = Bool("WipDark_AutoUpPermissive", readonly=True)
    Up = Bool("WipDark_Up")
    ManualUp = Bool("WipDark_ManualUp")
    Output = Bool("WipDark_Output")

    with Program() as logic:
        with rung(AutoUpPermissive):
            out(Up)
        with rung(Or(Up, And(ManualUp, ~Up))):
            out(Output)

    plan, observer = run_dark_drive(
        PLC(logic),
        Output,
        max_scans=200,
        strict=True,
    )

    assert plan.reachable
    first = observer.rows[0]
    expected = ("pulse", ((ManualUp.name, True),))
    assert first["baseline_identity"] == expected
    assert first["shadow_identity"] == expected
    assert first["agree"] is True
    assert all(row["baseline_result"] != "RouteUnproductive" for row in observer.rows)
