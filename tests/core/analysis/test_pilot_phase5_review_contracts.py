"""Regression contracts from the final Phase-5 design review."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import PLC, Int, Program, Timer, copy, on_delay, rung, system
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.execution import ChannelMotion, ScanProgressReceipt
from pyrung.core.analysis.pilot.pilot import _monitor_committed_trial


def test_bootstrap_retries_an_intermediate_designation_before_reaching_target() -> None:
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

    corrections = tuple(
        event
        for event in events
        if event.kind == "theory_correction_composed"
        and event.data["configuration"] == ((preset.name, 11),)
    )
    assert len(corrections) == 1, tuple(
        (event.kind, event.scan, event.data) for event in events
    )
    assert not any(
        event.kind == "candidate_try" and (preset.name, 11) in event.data["applied"]
        for event in events
    )
    assert corrections[0].scan == 0
    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    assert events[-1].scan == 2


def test_post_commit_progress_follows_the_exact_scan_receipt(monkeypatch) -> None:
    """The outer loop does not re-prove a verifier-owned scan receipt."""
    import pyrung.core.analysis.pilot.pilot as pilot_module

    monitor_calls = []

    def legacy_monitor(*args):
        monitor_calls.append(args)
        yield SimpleNamespace(kind="legacy-monitor")

    monkeypatch.setattr(pilot_module, "_monitor_trend", legacy_monitor)
    receipt_checkpoint = object()
    monkeypatch.setattr(
        pilot_module,
        "_trial_checkpoint",
        lambda *_args: receipt_checkpoint,
    )
    policy = SimpleNamespace(action_pairs=(("Input", True),), applied=(("Input", True),))
    trial = SimpleNamespace(
        attempt=SimpleNamespace(
            bearing=SimpleNamespace(act=SimpleNamespace(policy=policy)),
        ),
        execution=SimpleNamespace(
            channel_motion=ChannelMotion(),
            scan_progress=ScanProgressReceipt(
                source_scan=11,
                productive_scan=12,
                landing_scan=12,
                kind="frontier",
                selected_act=("pulse", (("Input", True),)),
                distance_after=1,
            ),
        ),
    )
    state = SimpleNamespace(
        work=SimpleNamespace(state=SimpleNamespace(scan_id=12, tags={"Input": True})),
        steps=(),
        pending_departure=None,
        checkpoints=[],
        best_trend=2,
    )

    events = tuple(
        _monitor_committed_trial(
            trial,
            SimpleNamespace(tree=object()),
            state,
            SimpleNamespace(),
        )
    )

    assert [event.kind for event in events] == ["trial_committed"]
    assert monitor_calls == []
    assert state.checkpoints == [receipt_checkpoint]
    assert state.best_trend == 1


def test_exact_scan_progress_does_not_bypass_channel_departure(monkeypatch) -> None:
    """A productive intrascan edge cannot turn a missed bearing into a tip."""
    import pyrung.core.analysis.pilot.pilot as pilot_module

    monitor_calls = []

    def monitor(*args):
        monitor_calls.append(args)
        yield SimpleNamespace(kind="departure-monitor")

    monkeypatch.setattr(pilot_module, "_monitor_trend", monitor)
    policy = SimpleNamespace(action_pairs=(), applied=())
    trial = SimpleNamespace(
        attempt=SimpleNamespace(
            bearing=SimpleNamespace(act=SimpleNamespace(policy=policy)),
        ),
        execution=SimpleNamespace(
            channel_motion=ChannelMotion("State", 6, stop_reason="departed"),
            scan_progress=ScanProgressReceipt(
                source_scan=11,
                productive_scan=12,
                landing_scan=12,
                kind="frontier",
                selected_act=("coast", ()),
                distance_after=1,
            ),
        ),
    )
    state = SimpleNamespace(
        work=SimpleNamespace(state=SimpleNamespace(scan_id=12, tags={})),
        steps=(),
        pending_departure=None,
        checkpoints=[],
        best_trend=2,
    )

    events = tuple(
        _monitor_committed_trial(
            trial,
            SimpleNamespace(tree=object()),
            state,
            SimpleNamespace(),
        )
    )

    assert [event.kind for event in events] == ["trial_committed", "departure-monitor"]
    assert len(monitor_calls) == 1
    assert state.checkpoints == []
    assert state.best_trend == 2


def test_selected_producer_landing_outranks_crossed_intermediate_heading(
    monkeypatch,
) -> None:
    """A retained route tip is progress, not an ejection from its heading."""
    import pyrung.core.analysis.pilot.pilot as pilot_module

    monitor_calls = []

    def legacy_monitor(*args):
        monitor_calls.append(args)
        yield SimpleNamespace(kind="departure-monitor")

    monkeypatch.setattr(pilot_module, "_monitor_trend", legacy_monitor)
    receipt_checkpoint = object()
    monkeypatch.setattr(
        pilot_module,
        "_trial_checkpoint",
        lambda *_args: receipt_checkpoint,
    )
    policy = SimpleNamespace(action_pairs=(), applied=())
    trial = SimpleNamespace(
        attempt=SimpleNamespace(
            bearing=SimpleNamespace(act=SimpleNamespace(policy=policy)),
        ),
        execution=SimpleNamespace(
            channel_motion=ChannelMotion("State", 40, stop_reason="departed"),
            scan_progress=ScanProgressReceipt(
                source_scan=11,
                productive_scan=12,
                landing_scan=13,
                kind="selected-producer",
                selected_act=("pulse", (("Advance", True),)),
                distance_after=1,
                landing_owns_tip=True,
            ),
        ),
    )
    state = SimpleNamespace(
        work=SimpleNamespace(state=SimpleNamespace(scan_id=13, tags={"State": 50})),
        steps=(),
        pending_departure=None,
        checkpoints=[],
        best_trend=2,
    )

    events = tuple(
        _monitor_committed_trial(
            trial,
            SimpleNamespace(tree=object()),
            state,
            SimpleNamespace(),
        )
    )

    assert [event.kind for event in events] == ["trial_committed"]
    assert monitor_calls == []
    assert state.checkpoints == [receipt_checkpoint]
    assert state.best_trend == 1
