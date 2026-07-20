"""Instrumented how(y_BurnerLoop): dump incident evidence + per-replay judgment.

Wraps build_deviation_incident / incident_regression_witness / build_replay_fn
(via the names progress.py imported) to print what the investigation actually
saw: changed tags, the exact recorded departure cause, and each replay's
outcome reason.

Run:  PYTHONPATH=. uv run python scratchpad/burner/drive_y_burnerloop_deep.py
"""

from __future__ import annotations

import importlib
import time

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.causal import _shared_cause
from pyrung.core.analysis.pilot import progress as prog

WALL_S = 480.0

_real_build_incident = prog.build_deviation_incident
_real_regression_witness = prog.incident_regression_witness
_real_build_replay = prog.build_replay_fn


def build_incident(**kw):
    incident = _real_build_incident(**kw)
    done_marks = [
        (e.scan, t, b, a)
        for e in incident.timeline
        for t, b, a in e.transitions
        if t.endswith("_Done") or "_tmr" in t
    ]
    print(f"\n  INCIDENT window=[{incident.anchor_scan},{incident.end_scan}] "
          f"chan={incident.channel_tag} departure_scan={incident.departure_scan}")
    print(f"    changed_tags={incident.changed_tags}")
    print(f"    departures={[(d.tag, d.value, d.scan) for d in incident.departures]}")
    print(f"    timeline: {len(incident.timeline)} events; done/tmr marks:")
    for scan, t, b, a in done_marks[:25]:
        print(f"      scan {scan:6d}  {t}: {b!r} -> {a!r}")
    return incident


def regression_witness(plc, incident):
    result = _real_regression_witness(plc, incident)
    print(f"    regression_witness={result}")
    if result is not None:
        chain = _shared_cause(plc, result.channel_tag, result.departure_scan)
        for step in chain.steps:
            transition = step.transition
            triggers = tuple(
                (item.tag_name, item.from_value, item.to_value, item.scan_id)
                for item in step.triggers
            )
            enablers = tuple(
                (item.tag_name, item.value)
                for item in step.enablers
            )
            print(
                "      chain "
                f"{step.subroutine or 'main'}:{step.rung_index} "
                f"{transition.tag_name} {transition.from_value!r}->{transition.to_value!r}"
                f"@{transition.scan_id} triggers={triggers} enablers={enablers}"
            )
    return result


def build_replay(*args, **kw):
    replay = _real_build_replay(*args, **kw)
    print(f"    replay specs: {[(s.kind, s.scans, s.channel_tag, s.channel_target) for s in args[3]]}")
    print(f"    replay_watch_roles={kw.get('replay_watch_roles')} "
          f"zoom_chan={kw.get('zoom_channel_tag')}={kw.get('zoom_target_value')!r} "
          f"letrun_roles={kw.get('terminal_letrun_role_tags')} "
          f"witness={kw.get('regression_witness')}")

    def wrapped(holds):
        out = replay(holds)
        pairs = [
            (h.dest, h.value, str(getattr(h, "guard", ""))[:60])
            if hasattr(h, "dest")
            else h
            for h in holds
        ]
        end = {
            t: out.snapshot.get(t)
            for t in (
                "Sts_StateCurrent",
                "A_Alm12_Blower_Trig",
                "Blower_Error",
                "x_BlowerFB",
                "i_BlowerFB",
                "Blower_CurStep",
            )
        }
        print(f"    REPLAY holds={pairs} -> accepted={out.accepted} reason={out.reason!r}")
        print(f"      end={end}")
        return out

    return wrapped


prog.build_deviation_incident = build_incident
prog.incident_regression_witness = regression_witness
prog.build_replay_fn = build_replay


def main() -> None:
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    plc = PLC(logic, dt=0.010)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]
    t0 = time.perf_counter()
    regressions = 0
    for event in pilot_events(plc, target, max_scans=40_000):
        if event.kind in ("candidate_accepted", "zoom_accepted", "letrun_ejection", "stuck", "finished"):
            print(f"[{time.perf_counter() - t0:6.1f}s] scan {event.scan:6d} {event.kind}")
        if event.kind == "trend_regression":
            regressions += 1
            inv = dict(event.data).get("investigation") or {}
            print(
                f"  == regression #{regressions}: hyps={inv.get('hypotheses')} "
                f"confirmed={inv.get('confirmed')} =="
            )
            for h in inv.get("confirmed_detail", ()):
                print(f"     CONFIRMED {h.get('kind')}: {h.get('detail')}")
        if event.kind == "finished" or time.perf_counter() - t0 > WALL_S:
            break
    print(f"done {time.perf_counter() - t0:.1f}s after {regressions} regressions")


if __name__ == "__main__":
    main()
