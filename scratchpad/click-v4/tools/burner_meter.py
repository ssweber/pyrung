"""Burner (tumbler) fold workloads.  usage: python burner_meter.py <workload> <out.json>"""

from __future__ import annotations

import importlib
import json
import sys
import time

import fold_meter

fold_meter.install()

from pyrung import PLC  # noqa: E402

PHYSICAL = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}


def logic():
    return importlib.import_module("tests.fixtures.tumbler").logic


def how_burner() -> dict:
    from pyrung.core.analysis.pilot import pilot_events

    plc = PLC(logic(), dt=0.010)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]
    fin = None
    for event in pilot_events(plc, target, max_scans=20_000):
        if event.kind == "finished":
            fin = dict(event.data)
            break
    return {"reached": bool(fin and fin.get("reached")), "final_scan": plc.state.scan_id}


def run_for_idle() -> dict:
    plc = PLC(logic(), dt=0.010)
    plc.step()
    for k, v in PHYSICAL.items():
        plc.force(k, v)
    plc.run_for(300.0)
    return {"final_scan": plc.state.scan_id}


def run_until_cmds() -> dict:
    """Cold boot, physical permissives held, then command-driven run_until legs."""
    plc = PLC(logic(), dt=0.010)
    plc.step()
    for k, v in PHYSICAL.items():
        plc.force(k, v)
    out = {}
    plc.run_until(lambda s: False, max_cycles=3_000)
    out["leg0_scan"] = plc.state.scan_id
    # Clear / reset / start style legs; tags may or may not exist on the program,
    # these just drive the PackML state machine where it will go.
    for i, cmd in enumerate(("Cmd_State_Clear", "Cmd_State_Reset", "Cmd_State_Start")):
        if cmd in plc._known_tags_by_name:
            plc.patch({cmd: True})
        before = plc.state.tags.get("Sts_StateCurrent")
        plc.run_until(
            lambda s, b=before: s.tags.get("Sts_StateCurrent") != b, max_cycles=5_000
        )
        plc.run_until(lambda s: False, max_cycles=5_000)
        out[f"leg{i + 1}"] = (plc.state.scan_id, plc.state.tags.get("Sts_StateCurrent"))
    return out


def seek_replay() -> dict:
    plc = PLC(logic(), dt=0.010)
    plc.step()
    for k, v in PHYSICAL.items():
        plc.force(k, v)
    plc.run_for(120.0)
    tip = plc.state.scan_id
    # measure only the replay
    fold_meter.reset()
    r = plc._replay_seek(tip - 7)
    return {"tip": tip, "replay_scan": r.state.scan_id, "stats": plc._last_replay_seek_stats}


WORKLOADS = {
    "how_burner": how_burner,
    "run_for_idle": run_for_idle,
    "run_until_cmds": run_until_cmds,
    "seek_replay": seek_replay,
}

if __name__ == "__main__":
    name, path = sys.argv[1], sys.argv[2]
    fold_meter.reset()
    t0 = time.perf_counter()
    info = WORKLOADS[name]()
    snap = fold_meter.snapshot()
    snap["total_wall_s"] = round(time.perf_counter() - t0, 2)
    snap["info"] = info
    with open(path, "w") as f:
        json.dump(snap, f, indent=1, default=str)
    print(name, {k: snap[k] for k in ("kernel_scans", "logical_scans", "folds_taken",
                                      "declined_plateau", "declined_bound", "cycle_folds",
                                      "wall_s")}, info)
