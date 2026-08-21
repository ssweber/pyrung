from __future__ import annotations

from types import SimpleNamespace

import pyrung.core.analysis.pilot.evidence as evidence_module
import pyrung.core.analysis.pilot.route_options as route_options_module
from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
from pyrung.core.analysis.pilot.overlay import (
    PilotOverlayExecution,
    PilotRung,
    PilotRungExecution,
    PilotRungExecutionState,
)
from pyrung.core.analysis.pilot.pipeline_graph import StaticTransitionEdge
from pyrung.core.analysis.pilot.route_options import _live_chart_completion_edge


class _ProducerGuard:
    def _evaluate_conditions(self, snapshot) -> bool:
        return bool(snapshot.get_tag("ProducerGuard"))


def _route_edge(
    *,
    co_actions: tuple[tuple[str, object], ...] = (),
) -> StaticTransitionEdge:
    action = ("Command", True)
    role = PipelineRoles("State")
    route = TransitionRoute(
        destination_tag=role.channel_tag,
        destination_value=1,
        request_tag=None,
        request_value=None,
        source_constraints=((role.channel_tag, 0),),
        enablers=(action, *co_actions),
        action_tags=frozenset((action[0], *(tag for tag, _value in co_actions))),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(0,),
    )
    return StaticTransitionEdge(
        role=role,
        from_value=0,
        to_value=1,
        action=action,
        request_tag=None,
        request_value=None,
        source_constraints=route.source_constraints,
        enablers=route.enablers,
        route=route,
        co_actions=co_actions,
    )


def _completion_context(
    monkeypatch,
    *,
    edge_tags: frozenset[str] = frozenset(),
    oneshot_edges: frozenset[tuple[object, ...]] = frozenset(),
) -> SimpleNamespace:
    monkeypatch.setattr(
        evidence_module,
        "selected_chart_producer_guard_rungs",
        lambda *_args: (_ProducerGuard(),),
    )
    return SimpleNamespace(
        pdg=object(),
        program=object(),
        edge_tags=edge_tags,
        oneshot_edges=oneshot_edges,
    )


def _effective_overlay(*pairs: tuple[str, object]) -> PilotOverlayExecution:
    return PilotOverlayExecution(
        tuple(
            PilotRungExecution(
                PilotRung(tag, value, object()),
                PilotRungExecutionState.EFFECTIVE,
            )
            for tag, value in pairs
        )
    )


def test_chart_completion_accepts_a_rung_owned_command_without_rebuilding_overlay(
    monkeypatch,
) -> None:
    edge = _route_edge()
    frame = SimpleNamespace(snap={"State": 0, "Command": False, "ProducerGuard": True})
    overlay = _effective_overlay(("Command", True))
    monkeypatch.setattr(
        route_options_module,
        "_pilot_rung_execution_receipt",
        lambda *_args: (_ for _ in ()).throw(AssertionError("receipt was rebuilt")),
    )

    completion = _live_chart_completion_edge(
        edge,
        frame,
        SimpleNamespace(pilot_rungs=()),
        _completion_context(monkeypatch),
        overlay=overlay,
    )

    assert completion is not None
    assert completion.action is None


def test_chart_completion_accepts_a_snapshot_owned_command(monkeypatch) -> None:
    edge = _route_edge()
    frame = SimpleNamespace(snap={"State": 0, "Command": True, "ProducerGuard": True})

    completion = _live_chart_completion_edge(
        edge,
        frame,
        SimpleNamespace(pilot_rungs=()),
        _completion_context(monkeypatch),
    )

    assert completion is not None
    assert completion.action is None


def test_chart_completion_rejects_a_missing_co_action(monkeypatch) -> None:
    edge = _route_edge(co_actions=(("EdgeGate", True),))
    frame = SimpleNamespace(
        snap={
            "State": 0,
            "Command": True,
            "EdgeGate": False,
            "ProducerGuard": True,
        }
    )

    assert (
        _live_chart_completion_edge(
            edge,
            frame,
            SimpleNamespace(pilot_rungs=()),
            _completion_context(monkeypatch),
        )
        is None
    )


def test_chart_completion_requires_true_producer_guards(monkeypatch) -> None:
    edge = _route_edge(co_actions=(("EdgeGate", True),))
    state = SimpleNamespace(pilot_rungs=())
    ctx = _completion_context(monkeypatch)
    effective = {
        "State": 0,
        "Command": True,
        "EdgeGate": True,
        "ProducerGuard": True,
    }

    assert (
        _live_chart_completion_edge(edge, SimpleNamespace(snap=effective), state, ctx) is not None
    )
    assert (
        _live_chart_completion_edge(
            edge,
            SimpleNamespace(snap={**effective, "ProducerGuard": False}),
            state,
            ctx,
        )
        is None
    )


def test_chart_completion_rejects_a_spent_pulse_command(monkeypatch) -> None:
    edge = _route_edge()
    frame = SimpleNamespace(snap={"State": 0, "Command": True, "ProducerGuard": True})
    ctx = _completion_context(monkeypatch, oneshot_edges=frozenset((edge.identity,)))

    assert (
        _live_chart_completion_edge(
            edge,
            frame,
            SimpleNamespace(pilot_rungs=()),
            ctx,
        )
        is None
    )
