"""Determinism regressions for the tumbler decision-skeleton serializer."""

from types import SimpleNamespace

import pytest

from pyrung.core.analysis.graph import PlanStep
from pyrung.core.analysis.pilot._ops import PilotRung
from pyrung.core.analysis.pilot.types import _HoldLogEntry
from tests.tumbler.skeleton import extract_skeleton

pytestmark = pytest.mark.tumbler


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
    label = ", ".join(tag for tag, _value in tags)
    return SimpleNamespace(
        kind="finished",
        data={
            "reached": True,
            "reason": "target reached",
            "knowledge": {
                "hold_log": (_HoldLogEntry(scan=10, tags=tuple(tags), source="investigation"),),
                "lever_notes": {},
                "skiff_decline": None,
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
