from __future__ import annotations

import json
from types import SimpleNamespace

from devtools.profile_pilot_replay import (
    CoastCall,
    Partition,
    Profile,
    build_report,
    executable_overlay_fingerprint,
    observe,
    qualified_replay_residual_scans,
)
from pyrung import PLC
from pyrung.core import Bool, Program, Rung, out
from pyrung.core.analysis.pilot import causal


def test_partition_is_an_exhaustive_scalar_receipt() -> None:
    partition = Partition()

    partition.add(
        {
            "logical_scans": 20,
            "ordinary_folded_scans": 7,
            "cycle_folded_scans": 8,
            "residual_scans": 5,
        }
    )

    assert partition.logical == 20
    assert partition.ordinary_folded + partition.cycle_folded + partition.residual == 20


def test_profiler_records_candidate_interval_without_scan_log() -> None:
    enable = Bool("ProfileEnable")
    light = Bool("ProfileLight")
    with Program(strict=False) as program:
        with Rung(enable):
            out(light)

    plc = PLC(program)
    plc.patch({"ProfileEnable": True})
    plc.run(3)
    profile = Profile()

    with observe(profile):
        causal._program_written_changes(plc, 0, 3, frozenset({"ProfileLight"}))

    assert len(profile.candidate_calls) == 1
    call = profile.candidate_calls[0]
    assert call.overlay_fingerprint == executable_overlay_fingerprint(plc)
    assert (call.start_scan, call.end_scan, call.candidates) == (
        0,
        3,
        ("ProfileLight",),
    )
    assert not hasattr(profile, "scans")


def test_passing_shadow_does_not_qualify_unshadowed_populations() -> None:
    calls = [
        CoastCall("replay", "overlay-a", 10, 20, 10, 0, 0, 5),
        CoastCall("replay", "overlay-a", 20, 30, 10, 0, 0, 7),
        CoastCall("replay", "overlay-b", 10, 20, 10, 0, 0, 11),
        CoastCall("zoom", "overlay-a", 10, 20, 10, 0, 0, 13),
    ]
    shadow = {
        "advancement_kind": "replay_coast",
        "overlay_fingerprint": "overlay-a",
        "start_scan": 10,
        "end_scan": 20,
        "endpoint_parity": True,
    }

    assert qualified_replay_residual_scans(calls, shadow) == 5


def test_profile_report_does_not_apply_passing_shadow_globally() -> None:
    profile = Profile()
    profile.coast_partitions["replay"].add(
        {
            "logical_scans": 12,
            "ordinary_folded_scans": 4,
            "cycle_folded_scans": 3,
            "residual_scans": 5,
        }
    )
    events = [
        SimpleNamespace(
            kind="finished",
            data={"reached": True, "reason": "target reached"},
        )
    ]
    shadow = {
        "advancement_kind": "replay_coast",
        "overlay_fingerprint": "abc",
        "start_scan": 1,
        "end_scan": 6,
        "logical_scans": 5,
        "residual_scans": 5,
        "interpreted_seconds": 1.0,
        "warm_compiled_seconds": 0.25,
        "speedup": 4.0,
        "endpoint_parity": True,
    }

    report = build_report(
        profile,
        events=events,
        route_seconds=2.0,
        reached=True,
        finish_scan=12,
        shadow=shadow,
    )

    assert report["baseline"]["interpreted_replay_residual_scans"] == 5
    assert report["feasibility_shadow"]["endpoint_parity"] is True
    assert report["baseline"]["candidate_replay"]["compiled_eligible_interval_scans"] == 0
    assert report["baseline"]["candidate_replay"]["compiled_eligible_replay_residual_scans"] == 0
    assert report["modeled_ceiling"] is None
    json.dumps(report)
