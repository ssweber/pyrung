"""Regression contracts from the final Phase-5 design review."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import PLC, Int, Program, Timer, copy, on_delay, rung, system
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.pilot import _bootstrap_local_designation_survived


def test_bootstrap_repairs_an_intermediate_designation_before_reaching_target() -> None:
    """Local proof is the exact designation, not the global target endpoint."""

    idle = 0
    ready = 1
    poised_value = 2
    complete = 3
    alarmed = 9
    handoff = Int("BootstrapIntermediateHandoff", default=idle)
    poised = Int("BootstrapIntermediatePoised", default=idle)
    state = Int("BootstrapIntermediateState", default=idle)
    preset = Int("BootstrapIntermediatePresetMs")
    watchdog = Timer.clone("BootstrapIntermediateWatchdog")

    with Program() as logic:
        # Deliberately precedes the scan-0 producer/consumer chain: preserving
        # its READY handoff writes POISED on scan 0, while COMPLETE is reached
        # only after this rung is revisited on scan 1.
        with rung(poised == poised_value):
            copy(complete, state, oneshot=True)

        with rung(system.sys.first_scan):
            copy(ready, handoff)

        with rung(handoff == ready):
            on_delay(watchdog, preset)

        with rung(watchdog.Done):
            copy(alarmed, handoff, oneshot=True)

        with rung(handoff == ready):
            copy(poised_value, poised, oneshot=True)

    events = tuple(pilot_events(PLC(logic, dt=0.010), state == complete, max_scans=20))

    repairs = tuple(event for event in events if event.kind == "requirement_locally_repaired")
    assert len(repairs) == 1, tuple((event.kind, event.scan, event.data) for event in events)
    assert repairs[0].data["assignments"] == ((preset.name, 11),)
    assert repairs[0].data["detail"] == "bootstrap local transaction repaired"
    assert repairs[0].scan == 1
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    assert events[-1].scan == 2


@pytest.mark.parametrize(
    "failed_disposition",
    ["ABSENT", "OVERWRITTEN", "STRANDED", "DISPLACED", "UNKNOWN"],
)
def test_bootstrap_local_proof_rejects_every_non_surviving_disposition(
    failed_disposition: str,
) -> None:
    """A target endpoint cannot substitute for exact local effect proof."""

    designation = object()
    observation = SimpleNamespace(
        designation=designation,
        observation=SimpleNamespace(disposition=failed_disposition),
    )

    assert not _bootstrap_local_designation_survived((observation,), designation)


def test_bootstrap_local_proof_requires_the_exact_designation_identity() -> None:
    designation = SimpleNamespace(tag="Intermediate", value=1)
    equal_but_detached = SimpleNamespace(tag="Intermediate", value=1)
    observation = SimpleNamespace(
        designation=equal_but_detached,
        observation=SimpleNamespace(disposition="SURVIVED"),
    )

    assert designation == equal_but_detached
    assert not _bootstrap_local_designation_survived((observation,), designation)
