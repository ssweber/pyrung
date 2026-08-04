"""Determinism regressions for the tumbler decision-skeleton serializer."""

from types import SimpleNamespace

import pytest

from pyrung.core.analysis.graph import PlanStep
from pyrung.core.analysis.pilot.coast import CoastTriggerEvent
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.types import _HoldLogEntry
from tests.tumbler.skeleton import _jsonify_dataclass, extract_skeleton

pytestmark = pytest.mark.tumbler


def test_exact_causal_receipts_do_not_change_decision_skeleton() -> None:
    diagnostic = SimpleNamespace(kind="failed_effect_explained", data={"receipt": object()})
    finished = SimpleNamespace(kind="finished", data={"reached": True})

    assert extract_skeleton([diagnostic, finished]) == [{"kind": "finished", "reached": True}]


def test_coast_trigger_event_uses_exact_skeleton_marker() -> None:
    event = CoastTriggerEvent("target", "target", 17, (("State", 1, 2),))

    assert _jsonify_dataclass(event) == {
        "__type__": "CoastTriggerEvent",
        "name": "target",
        "kind": "target",
        "transitions": [["State", 1, 2]],
    }


def _trend_event(*, reverse: bool):
    latch_names = ["A_Alm14_DoorOpen_Trig", "A_Alm15_LintOpen_Trig"]
    holds = [
        PilotRung("x_DoorClosed", True, object()),
        PilotRung("x_LintDoorClosed", True, object()),
    ]
    sources = [*latch_names, "x_DoorClosed", "x_LintDoorClosed"]
    if reverse:
        latch_names.reverse()
        holds.reverse()
        sources.reverse()
    detail = {
        "kind": "latch-exposure",
        "detail": f"clear 2 active latches: {', '.join(latch_names)}",
        "holds": holds,
        "sources": sources,
    }
    return SimpleNamespace(
        kind="trend_regression",
        data={
            "from_trend": 2,
            "to_trend": 1,
            "channel_transitions": (),
            "regression_nogoods": (),
            "investigation": {
                "hypotheses": 1,
                "confirmed": 1,
                "rejected": 0,
                "unresolved": (),
                "hypothesis_detail": (detail,),
                "confirmed_detail": (detail,),
                "rejected_detail": (),
            },
        },
    )


def _finished_event(*, reverse: bool):
    tags = [("x_DoorClosed", True), ("x_LintDoorClosed", True)]
    if reverse:
        tags.reverse()
    holds = tuple(PilotRung(tag, value, object()) for tag, value in tags)
    label = ", ".join(tag for tag, _value in tags)
    return SimpleNamespace(
        kind="finished",
        data={
            "reached": True,
            "reason": "target reached",
            "knowledge": {
                "hold_log": (
                    _HoldLogEntry(
                        scan=10,
                        source="investigation",
                        pilot_rungs=holds,
                    ),
                ),
                "lever_notes": {},
                "avoid_names": (),
            },
            "plan_journal": (
                PlanStep(
                    kind="force",
                    scan=10,
                    scans=1,
                    inputs=tuple(tags),
                    label=label,
                    steady_holds=tuple(tag for tag, _value in tags),
                ),
            ),
        },
    )


def test_skeleton_scrubs_identity_and_canonicalizes_set_like_payloads() -> None:
    forward = extract_skeleton([_trend_event(reverse=False), _finished_event(reverse=False)])
    reversed_order = extract_skeleton([_trend_event(reverse=True), _finished_event(reverse=True)])

    assert forward == reversed_order
    rendered = repr(forward)
    assert "0x" not in rendered
    assert "<ADDR:1>" in rendered


def test_skeleton_address_tokens_preserve_shared_guard_identity() -> None:
    guard = object()
    detail = {
        "kind": "latch-exposure",
        "detail": "shared guard",
        "holds": [
            PilotRung("x_DoorClosed", True, guard),
            PilotRung("x_LintDoorClosed", True, guard),
        ],
        "sources": [],
    }
    event = SimpleNamespace(
        kind="trend_regression",
        data={
            "from_trend": 2,
            "to_trend": 1,
            "channel_transitions": (),
            "regression_nogoods": (),
            "investigation": {
                "hypotheses": 1,
                "confirmed": 1,
                "rejected": 0,
                "unresolved": (),
                "hypothesis_detail": (),
                "confirmed_detail": (detail,),
                "rejected_detail": (),
            },
        },
    )

    skeleton = extract_skeleton([event])
    holds = skeleton[0]["investigation"]["confirmed_detail"][0]["holds"]
    assert holds[0]["guard"] == holds[1]["guard"]
    assert holds[0]["guard"].endswith("at <ADDR:1>>")


def test_hypothesis_hold_order_ignores_process_local_guard_addresses() -> None:
    def event(reverse: bool):
        holds = [
            PilotRung("Sensor", False, object()),
            PilotRung("Sensor", True, object()),
        ]
        if reverse:
            holds.reverse()
        detail = {
            "kind": "liveness",
            "detail": "oscillate Sensor",
            "holds": holds,
            "sources": ["Sensor"],
        }
        return SimpleNamespace(
            kind="trend_regression",
            data={
                "from_trend": 2,
                "to_trend": 1,
                "channel_transitions": (),
                "regression_nogoods": (),
                "investigation": {
                    "hypotheses": 1,
                    "confirmed": 1,
                    "rejected": 0,
                    "unresolved": (),
                    "hypothesis_detail": (detail,),
                    "confirmed_detail": (detail,),
                    "rejected_detail": (),
                },
            },
        )

    forward = extract_skeleton([event(False)])
    reversed_order = extract_skeleton([event(True)])

    assert forward == reversed_order
    holds = forward[0]["investigation"]["confirmed_detail"][0]["holds"]
    assert [hold["value"] for hold in holds] == [False, True]
