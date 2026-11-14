from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyrung.core import Block, Bool, Program, Rung, TagType, latch, out, reset
from pyrung.core.analysis.pilot import drive_setup
from pyrung.core.analysis.pilot.compass import Compass, CompassKnowledge, NavigationCatalog
from pyrung.core.tag import metadata_edits


def _setup() -> drive_setup.DriveSetup:
    evidence = SimpleNamespace(affine_projections=lambda: {"Derived": "projection"})
    return drive_setup.DriveSetup(
        work=SimpleNamespace(
            tags={"SetupWork": object()},
            state=SimpleNamespace(tags={"SetupWork": 1}),
        ),
        program=object(),
        pdg=object(),
        steerable=frozenset({"Command"}),
        edge_tags={"Edge"},
        resting={"Command": False},
        anchor_scan=17,
        diag_snapshot={"State": 3},
        nd_domains={"Command": (False, True)},
        stateful_domains={"State": (0, 1, 2, 3)},
        key_config=object(),
        evidence=evidence,
        compass=Compass(
            NavigationCatalog(slices=(object(),)),
            CompassKnowledge(),
        ),
        opaque_loop=frozenset({"State"}),
        configured_inputs=frozenset({"Configured"}),
    )


def test_context_factory_carries_drive_setup_receipts_exactly(monkeypatch: Any) -> None:
    setup = _setup()
    pipeline_role = SimpleNamespace(trace_internal_tags=frozenset({"PipelineInternal"}))
    chart_role = SimpleNamespace(trace_internal_tags=frozenset({"ChartInternal"}))
    pipeline_graphs = (object(),)
    chart_graphs = (object(),)
    clear_only = frozenset({"Command"})
    calls: list[tuple[Any, ...]] = []

    def build_graphs(roles: tuple[Any, ...], *owners: Any) -> tuple[Any, ...]:
        calls.append((roles, *owners))
        return pipeline_graphs if roles == (pipeline_role,) else chart_graphs

    monkeypatch.setattr(
        drive_setup,
        "infer_opaque_pipeline_roles",
        lambda *owners: (calls.append(("pipeline", *owners)), (pipeline_role,))[1],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.evidence.discover_chart_roles",
        lambda *owners: (calls.append(("chart", *owners)), (chart_role,))[1],
    )
    monkeypatch.setattr(drive_setup, "_build_static_transition_graphs", build_graphs)
    monkeypatch.setattr(
        drive_setup,
        "compute_clear_only",
        lambda pdg, known_tags, program: (
            calls.append(("clear", pdg, known_tags, program)),
            clear_only,
        )[1],
    )

    ctx = drive_setup._make_pilot_context(
        setup,
        setup.work,
        "Target",
        True,
        "predicate",
        max_scans=23,
        avoid_pred="avoid",
    )

    assert ctx.pdg is setup.pdg
    assert ctx.program is setup.program
    assert ctx.steerable is setup.steerable
    assert ctx.edge_tags is setup.edge_tags
    assert ctx.resting is setup.resting
    assert ctx.nd_domains is setup.nd_domains
    assert ctx.evidence is setup.evidence
    assert ctx.key_config is setup.key_config
    assert ctx.opaque_loop is setup.opaque_loop
    assert ctx.configured_inputs is setup.configured_inputs
    assert ctx.domain_prior.nd_domains is setup.nd_domains
    assert ctx.domain_prior.stateful_domains is setup.stateful_domains
    assert ctx.domain_prior.func_deps == {"Derived": "projection"}
    assert ctx.compass.knowledge is setup.compass.knowledge
    assert ctx.compass.catalog.slices is setup.compass.catalog.slices
    assert ctx.compass.catalog.graphs is pipeline_graphs
    assert ctx.compass.catalog.chart_graphs is chart_graphs
    assert ctx.pipeline_roles == (pipeline_role,)
    assert ctx.chart_roles == (chart_role,)
    assert ctx.pipeline_internal_tags == frozenset({"PipelineInternal"})
    assert ctx.clear_only is clear_only
    assert ctx.route is None
    assert ctx.max_scans == 23
    assert ctx.avoid_pred == "avoid"
    assert ctx.target.tag == "Target"
    assert ctx.target.value is True
    assert ctx.target.predicate == "predicate"

    shared_owners = (
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        setup.evidence,
    )
    assert ("pipeline", *shared_owners) in calls
    assert ("chart", *shared_owners) in calls
    assert ("clear", setup.pdg, setup.work.tags, setup.program) in calls


def test_prepare_target_context_preserves_work_and_compass_defaults_and_overrides(
    monkeypatch: Any,
) -> None:
    setup = _setup()
    override_work = SimpleNamespace(
        tags={"OverrideWork": object()},
        state=SimpleNamespace(tags={"OverrideWork": 2}),
    )
    override_compass = Compass(knowledge=CompassKnowledge())
    contexts = (object(), object())
    routes = (object(), object())
    context_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    route_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    asked: list[tuple[str, dict[str, Any]]] = []

    def avoid(snapshot: dict[str, Any]) -> bool:
        asked.append(("avoid", snapshot))
        return False

    def other_avoid(snapshot: dict[str, Any]) -> bool:
        asked.append(("other-avoid", snapshot))
        return False

    def make_context(*args: Any, **kwargs: Any) -> Any:
        context_calls.append((args, kwargs))
        return contexts[len(context_calls) - 1]

    def prepare_route(*args: Any, **kwargs: Any) -> Any:
        route_calls.append((args, kwargs))
        return routes[len(route_calls) - 1]

    monkeypatch.setattr(drive_setup, "_make_pilot_context", make_context)
    monkeypatch.setattr(drive_setup._target_route, "prepare_target_route", prepare_route)

    default = drive_setup.prepare_target_context(
        setup,
        "Target",
        True,
        "predicate",
        max_scans=31,
        avoid_pred=avoid,
    )
    overridden = drive_setup.prepare_target_context(
        setup,
        "Target",
        False,
        "other-predicate",
        max_scans=37,
        avoid_pred=other_avoid,
        compass=override_compass,
        work=override_work,
    )

    assert default == (contexts[0], routes[0])
    assert overridden == (contexts[1], routes[1])
    assert context_calls[0] == (
        (setup, setup.work, "Target", True, "predicate"),
        {"max_scans": 31, "avoid_pred": avoid, "compass": None},
    )
    assert context_calls[1] == (
        (setup, override_work, "Target", False, "other-predicate"),
        {"max_scans": 37, "avoid_pred": other_avoid, "compass": override_compass},
    )
    # The start ask reads the work the search will start from, not the setup's.
    assert asked == [("avoid", {"SetupWork": 1}), ("other-avoid", {"OverrideWork": 2})]
    assert route_calls[0][0][0] is setup.work
    assert route_calls[1][0][0] is override_work
    for args, _kwargs in route_calls:
        assert args[3] is setup.pdg
        assert args[4] is setup.program
        assert args[5] is setup.steerable
        assert args[6] is setup.opaque_loop


# ---------------------------------------------------------------------------
# build_prover_context keeps its answer on the Program
# ---------------------------------------------------------------------------


def _slot_program() -> tuple[Program, Block]:
    cmd = Block("PCtxCmd", TagType.INT, 1, 2)
    running = Bool("PCtxRunning")
    with Program(strict=False) as logic:
        with Rung(cmd[1] == 5):
            latch(running)
        with Rung(cmd[2] == 1):
            reset(running)
        with Rung(running, cmd[1] == 0):
            out(Bool("PCtxLamp"))
    return logic, cmd


@pytest.fixture
def computed(monkeypatch: Any) -> list[Any]:
    """Each program build_prover_context runs the prover's passes for."""
    calls: list[Any] = []
    real = drive_setup._compute_prover_context

    def compute(program: Any) -> Any:
        calls.append(program)
        return real(program)

    monkeypatch.setattr(drive_setup, "_compute_prover_context", compute)
    return calls


def test_second_call_on_the_same_program_reuses_the_answer(computed: list[Any]) -> None:
    logic, _cmd = _slot_program()
    first = drive_setup.build_prover_context(logic)
    second = drive_setup.build_prover_context(logic)
    assert computed == [logic]
    assert first.key_config is not None
    assert second == first
    # Each caller gets its own dicts, so a change to one never reaches the next.
    assert first.nd_domains is not None
    first.nd_domains.clear()
    third = drive_setup.build_prover_context(logic)
    assert third == second
    assert third.nd_domains


def test_a_program_edit_drops_the_answer(computed: list[Any]) -> None:
    logic, _cmd = _slot_program()
    drive_setup.build_prover_context(logic)
    stop = Bool("PCtxStop", external=True)
    with logic:
        with Rung(stop):
            out(Bool("PCtxStopped"))
    assert logic._cached_prover_context is None
    after = drive_setup.build_prover_context(logic)
    assert computed == [logic, logic]
    assert after.nd_domains is not None and "PCtxStop" in after.nd_domains


def test_dropping_the_graph_cache_drops_the_answer(computed: list[Any]) -> None:
    logic, _cmd = _slot_program()
    drive_setup.build_prover_context(logic)
    logic._cached_graph = None
    drive_setup.build_prover_context(logic)
    assert computed == [logic, logic]


def test_a_slot_setting_made_later_drops_the_answer(computed: list[Any]) -> None:
    logic, cmd = _slot_program()
    before = drive_setup.build_prover_context(logic)
    assert before.nd_domains is not None and 9 not in before.nd_domains["PCtxCmd1"]
    cmd.slot(1, choices={0: "off", 5: "on", 9: "fast"})
    after = drive_setup.build_prover_context(logic)
    assert computed == [logic, logic]
    assert after.nd_domains is not None and 9 in after.nd_domains["PCtxCmd1"]


def test_each_later_change_to_a_tags_settings_is_counted() -> None:
    from pyrung.click import TagMap

    bank = Block("PCtxBank", TagType.INT, 1, 4)
    hardware = Block("PCtxHardware", TagType.INT, 1, 3)
    named = Block("PCtxNamed", TagType.INT, 1, 2)
    bank.slot(4, choices={0: "a"})
    changes = [
        lambda: bank.slot(1, max=10),
        lambda: bank.slot(2, 3, retentive=True),
        lambda: bank.slot(4).reset(),
        lambda: bank.reset(),
        lambda: Bool("PCtxMapped").map_to(hardware[3]),
        lambda: named.map_to(hardware.select(1, 2)),
        lambda: TagMap([]),
    ]
    for change in changes:
        before = metadata_edits()
        change()
        assert metadata_edits() > before
