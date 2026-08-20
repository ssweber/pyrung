"""Attempt-transition knowledge retention policy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung.core.analysis.pilot.attempt_transition import record_attempt
from pyrung.core.analysis.pilot.compass import CompassObservation
from pyrung.core.analysis.pilot.navigation_contracts import ActPolicy, ActSource, Pulse


class _RecordingCompass:
    def __init__(self) -> None:
        self.applied: tuple[Any, ...] = ()

    def apply(self, observations: Any) -> tuple[_RecordingCompass, bool]:
        self.applied = tuple(observations)
        return self, bool(self.applied)


def _record(*, accepted: bool) -> tuple[Any, ...]:
    action = ("PulseAction", True)
    context = (("State", 0), ("Unrelated", False))
    observations = (
        CompassObservation(
            "edge",
            "State",
            action,
            0,
            1,
            ("world",),
            context,
            (action,),
        ),
        CompassObservation(
            "contradict",
            "Unrelated",
            action,
            False,
            None,
            ("world",),
            context,
            (action,),
        ),
    )
    attempt = SimpleNamespace(
        trial=object() if accepted else None,
        observations=observations,
        nogood_pairs=frozenset(),
        avoid_names=(),
    )
    compass = _RecordingCompass()
    record_attempt(
        attempt,
        SimpleNamespace(key=("world",)),
        SimpleNamespace(avoid_names=set()),
        SimpleNamespace(compass=compass),
        SimpleNamespace(),
        Pulse(ActPolicy(ActSource.TRACE, (action,), (action,))),
    )
    return compass.applied


def test_verified_pulse_retains_causal_edges_without_unrelated_tombstones() -> None:
    retained = _record(accepted=True)

    assert [observation.kind for observation in retained] == ["edge"]


def test_rejected_pulse_retains_negative_empirical_evidence() -> None:
    retained = _record(accepted=False)

    assert [observation.kind for observation in retained] == ["edge", "contradict"]
