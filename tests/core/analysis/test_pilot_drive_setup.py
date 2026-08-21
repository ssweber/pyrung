from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung.core.analysis.pilot import drive_setup
from pyrung.core.analysis.pilot.compass import Compass, CompassKnowledge, NavigationCatalog


def _setup() -> drive_setup.DriveSetup:
    evidence = SimpleNamespace(affine_projections=lambda: {"Derived": "projection"})
    return drive_setup.DriveSetup(
        work=SimpleNamespace(_known_tags_by_name={"SetupWork": object()}),
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
        "oneshot_rearm_edges",
        lambda graphs, pdg, program: (
            calls.append(("oneshot", graphs, pdg, program)),
            frozenset(),
        )[1],
    )
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
    assert ctx.oneshot_edges == frozenset()
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
    assert ("clear", setup.pdg, setup.work._known_tags_by_name, setup.program) in calls
    assert ("oneshot", (*pipeline_graphs, *chart_graphs), setup.pdg, setup.program) in calls


def test_prepare_target_context_preserves_work_and_compass_defaults_and_overrides(
    monkeypatch: Any,
) -> None:
    setup = _setup()
    override_work = SimpleNamespace(_known_tags_by_name={"OverrideWork": object()})
    override_compass = Compass(knowledge=CompassKnowledge())
    contexts = (object(), object())
    routes = (object(), object())
    context_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    route_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

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
        avoid_pred="avoid",
    )
    overridden = drive_setup.prepare_target_context(
        setup,
        "Target",
        False,
        "other-predicate",
        max_scans=37,
        avoid_pred="other-avoid",
        compass=override_compass,
        work=override_work,
    )

    assert default == (contexts[0], routes[0])
    assert overridden == (contexts[1], routes[1])
    assert context_calls[0] == (
        (setup, setup.work, "Target", True, "predicate"),
        {"max_scans": 31, "avoid_pred": "avoid", "compass": None},
    )
    assert context_calls[1] == (
        (setup, override_work, "Target", False, "other-predicate"),
        {"max_scans": 37, "avoid_pred": "other-avoid", "compass": override_compass},
    )
    assert route_calls[0][0][0] is setup.work
    assert route_calls[1][0][0] is override_work
    for args, _kwargs in route_calls:
        assert args[3] is setup.pdg
        assert args[4] is setup.program
        assert args[5] is setup.steerable
        assert args[6] is setup.opaque_loop
