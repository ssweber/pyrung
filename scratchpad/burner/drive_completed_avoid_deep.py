"""Trace incident ownership for how(Sts_State_Completed, avoid=Complete).

This is the avoided-Complete counterpart to ``drive_y_burnerloop_deep.py``.
It reports the incident window and the investigation result at each retained
or corrected departure, plus the state-machine landing at decision frames.

Run: uv run python scratchpad/burner/drive_completed_avoid_deep.py
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot import program_step as program_step_module
from pyrung.core.analysis.pilot import progress as prog
from pyrung.core.runner import _compile_avoid

WALL_S = 180.0
PROCESS = psutil.Process()

_real_build_incident = prog.build_deviation_incident
_real_build_replay_fn = prog.build_replay_fn
_real_incident_witness = prog.incident_regression_witness
_real_read_program_step = program_step_module.read_program_step


def build_incident(**kwargs):
    incident = _real_build_incident(**kwargs)
    print(
        "\nINCIDENT"
        f" window=[{incident.anchor_scan},{incident.end_scan}]"
        f" channel={incident.channel_tag}"
        f" departure_scan={incident.departure_scan}"
    )
    print(f"  changed={incident.changed_tags}")
    print(f"  departures={[(d.tag, d.value, d.scan) for d in incident.departures]}")
    return incident


prog.build_deviation_incident = build_incident


def build_replay_fn(*args, **kwargs):
    replay = _real_build_replay_fn(*args, **kwargs)

    def traced_replay(holds):
        outcome = replay(holds)
        hold_pairs = [
            (
                getattr(hold, "dest", hold[0]),
                getattr(hold, "value", hold[1]),
                repr(getattr(hold, "guard", None)),
            )
            if not hasattr(hold, "dest")
            else (hold.dest, hold.value, repr(hold.guard))
            for hold in holds
        ]
        snap = outcome.snapshot
        print(
            "\nREPLAY"
            f" holds={hold_pairs}"
            f" accepted={outcome.accepted}"
            f" reason={outcome.reason!r}"
            f" landed={outcome.landed}"
            f" state={snap.get('Sts_StateCurrent')}"
            f" step={snap.get('Internal__Step')}"
            f" pending_suspend={snap.get('Sts_Pending1stScanSuspnd')}"
            f" first_scan_alarm={snap.get('A_Alm3_1stScanWatchdog')}"
        )
        return outcome

    return traced_replay


prog.build_replay_fn = build_replay_fn


def incident_witness(*args, **kwargs):
    witness = _real_incident_witness(*args, **kwargs)
    if witness is None:
        print("  WITNESS none")
    else:
        print(
            "  WITNESS"
            f" cause={[(str(o.rung), o.tag, o.value) for o in witness.cause]}"
            f" spine={sorted(witness.causal_spine)}"
        )
    return witness


prog.incident_regression_witness = incident_witness


def read_program_step(*args, **kwargs):
    result = _real_read_program_step(*args, **kwargs)
    changes = tuple(
        change
        for change in result.projected_changes
        if change[0] in {"Sts_StateCurrent", "Sts_StateRequested", "Cmd_CtrlCmd"}
    )
    print(
        "\nPROGRAM STEP"
        f" producer={result.producer.command_tag}={result.producer.command_value!r}"
        f" status={result.status}"
        f" boundary={result.boundary!r}"
        f" channel={result.channel}"
        f" inputs={[action.pair for action in result.required_inputs]}"
        f" handoffs={[(handoff.action, handoff.boundary) for handoff in result.input_handoffs]}"
        f" changes={changes}"
        f" reason={result.reason!r}"
    )
    return result


program_step_module.read_program_step = read_program_step


def main() -> None:
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    plc = PLC(logic)
    plc.step()
    tags = plc._known_tags_by_name
    avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])
    last_landing = None
    events = []
    started = time.perf_counter()

    for event in pilot_events(
        plc,
        tags["Sts_State_Completed"],
        max_scans=40_000,
        avoid_pred=avoid_pred,
    ):
        events.append(event)
        data = dict(event.data)
        if event.kind == "iteration":
            snapshot = data["snapshot"]
            landing = (
                snapshot.get("Sts_StateCurrent"),
                snapshot.get("Internal__Step"),
                snapshot.get("Cmd_CtrlCmd"),
            )
            if landing != last_landing:
                print(f"\nscan {event.scan}: state/step/cmd={landing}")
                last_landing = landing
        elif event.kind in {
            "candidate_accepted",
            "bearing_coast",
            "bearing_coast_accepted",
            "bearing_coast_rejected",
            "letrun_ejection",
            "pending_departure_started",
            "pending_departure_promoted",
            "pending_departure_regressed",
            "pending_departure_expired",
            "departure_investigated",
            "trend_regression",
            "candidates_built",
            "candidate_rejected",
            "stuck",
            "finished",
        }:
            print(
                f"scan {event.scan}: {event.kind}"
                f" rss={PROCESS.memory_info().rss >> 20}MB"
            )
            if event.kind in {"departure_investigated", "trend_regression"}:
                investigation = data.get("investigation") or {}
                print(
                    "  investigation"
                    f" hypotheses={investigation.get('hypotheses')}"
                    f" confirmed={investigation.get('confirmed')}"
                    f" rejected={investigation.get('rejected')}"
                    f" unresolved={investigation.get('unresolved')}"
                    f" retained={data.get('retained')}"
                    f" revoked={data.get('revoked_corrections')}"
                    f" revoked_pilot_rungs={data.get('revoked_pilot_rungs')}"
                )
                for hypothesis in investigation.get("hypothesis_detail", ()):
                    holds = hypothesis.get("holds", ())
                    print(
                        f"    H {hypothesis.get('kind')}: {hypothesis.get('detail')}"
                        f" holds={[(getattr(h, 'dest', None), getattr(h, 'value', None), repr(getattr(h, 'guard', None))) for h in holds]}"
                    )
                for hypothesis in investigation.get("confirmed_detail", ()):
                    print(
                        f"    C {hypothesis.kind}: {hypothesis.detail}"
                        if hasattr(hypothesis, "kind")
                        else f"    C {hypothesis.get('kind')}: {hypothesis.get('detail')}"
                    )
                for rejected in investigation.get("rejected_detail", ()):
                    print(
                        f"    R [{rejected.get('slug')}] {rejected.get('kind')}: "
                        f"{rejected.get('detail')}"
                        f"\n      ground={rejected.get('ground')}"
                    )
            else:
                selected = {
                    key: data[key]
                    for key in (
                        "channel_tag",
                        "from_value",
                        "requested_value",
                        "to_value",
                        "settled_value",
                        "reason",
                        "reached",
                    )
                    if key in data
                }
                if event.kind.startswith("pending_departure_"):
                    selected["earned_work_mark"] = data.get("earned_work_mark")
                    selected["landing_mark"] = data.get("landing_mark")
                if event.kind == "candidate_accepted":
                    selected["applied"] = data.get("applied")
                    selected["candidate"] = data.get("candidate_detail")
                if event.kind == "bearing_coast_rejected":
                    selected["gates"] = data.get("gates")
                if event.kind == "candidates_built":
                    selected["candidates"] = [
                        (candidate.get("pair"), candidate.get("rank_reason"))
                        for candidate in data.get("candidates", ())
                    ]
                    selected["stuck_reason"] = data.get("stuck_reason")
                    selected["wait_reason"] = data.get("wait_reason")
                if event.kind == "candidate_rejected":
                    selected["candidate"] = data.get("candidate")
                    selected["gates"] = data.get("gates")
                if selected:
                    print(f"  {selected}")
        if event.kind == "finished" or time.perf_counter() - started > WALL_S:
            break

    print(f"\ndone in {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
