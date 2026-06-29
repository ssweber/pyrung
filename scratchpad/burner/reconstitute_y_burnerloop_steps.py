"""Concrete patch/force/step sequence that reaches y_BurnerLoop.

This intentionally does not use how().  It drives the generated CLICK project
like a test bench:

1. Hold physical permissives/feedback true.
2. Select Production mode.
3. Pulse Clear, Reset, Start.
4. Keep the rotate sensor moving while the SFCs initialize.
5. Wait until Heat reaches step 3 and turns on the burner output.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402


MONITOR_TAGS = (
    "S_UnitModeCurrent",
    "S_StateCurrent",
    "S_StateRequested",
    "S_StateCompleteBool",
    "Internal__Step",
    "S_CurrStep_Dry",
    "Rotate_xCall",
    "Rotate__x",
    "Rotate_CurStep",
    "Rotate__init",
    "Rotate_Error",
    "Blower_xCall",
    "Blower__x",
    "Blower_CurStep",
    "Blower__init",
    "Blower_Error",
    "HeatDelay_Tmr_Acc",
    "HeatDelay_Tmr_Done",
    "Heat_xCall",
    "Heat__x",
    "Heat_CurStep",
    "Heat__init",
    "Heat_Error",
    "S_DryerTemp_F",
    "S_P1_OperatingTemp_F",
    "Heat_TargetTemp_F",
    "o_BurnerLoop",
    "y_BurnerLoop",
)


scan = 0


def get(plc: PLC, name: str) -> object:
    return plc.state.tags.get(name, "<missing>")


def dump(plc: PLC, label: str) -> None:
    fields = ", ".join(
        f"{name}={get(plc, name)!r}" for name in MONITOR_TAGS if name in plc.state.tags
    )
    print(f"\n[{scan:04d}] {label}")
    print(f"  {fields}")


def step(plc: PLC, count: int = 1, *, animate_rotate_sensor: bool = False) -> bool:
    """Step the PLC; return True as soon as y_BurnerLoop is true."""
    global scan
    for _ in range(count):
        if animate_rotate_sensor:
            # Rotate watchdogs require a changing sensor after Rotate_CurStep >= 3.
            plc.force("x_RotateSensor", (scan // 50) % 2 == 0)
        plc.step()
        scan += 1
        if get(plc, "y_BurnerLoop") is True:
            dump(plc, "HIT y_BurnerLoop")
            return True
    return False


def pulse(plc: PLC, name: str, settle_scans: int = 4) -> None:
    plc.patch({name: True})
    step(plc)
    dump(plc, f"after {name} pulse")
    step(plc, settle_scans)
    dump(plc, f"after {name} settle")


def main() -> int:
    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    plc = PLC(logic)

    # Physical permissives and feedback.  These are external inputs, not
    # internal shortcuts; read_inputs maps them into the i_* tags.
    for name, value in {
        "x_DoorClosed": True,
        "x_LintDoorClosed": True,
        "x_BlowerFB": True,
        "x_RotateFB": True,
        "x_RotateSensor": False,
        "x_SailRelay": True,
    }.items():
        plc.force(name, value)

    step(plc)
    dump(plc, "after first scan + physical inputs")

    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    step(plc, 2)
    dump(plc, "after Production mode request")

    pulse(plc, "C_Clear")
    pulse(plc, "C_Reset")
    pulse(plc, "C_Start")

    # Normal scan-time wait.  At dt=0.010:
    # - Rotate initializes around 4s.
    # - Blower initializes around 7s.
    # - Execute then starts HeatDelay_Tmr; Heat_xCall follows around 10s later.
    # - Heat reaches CurStep 3 roughly 2s after it is called.
    for block in range(1, 100):
        if step(plc, 50, animate_rotate_sensor=True):
            return 0
        if block % 4 == 0:
            dump(plc, f"after wait {block * 50} scans")

    dump(plc, "FAILED to reach y_BurnerLoop")
    return 1


def pilot_trace():
    """PILOT backward trace on the real burner program."""
    from pyrung.core.analysis.pdg import ProgramGraph, TagRole, build_program_graph, resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _extract_condition_values,
        _values_match,
        _written_value_for_tag,
        copy_source_binding,
    )
    from pyrung.core.crossing import Affine, Literal

    def trace_back(tag, value, snapshot, pdg, program, *, visited=None, depth=0, max_depth=6):
        if visited is None:
            visited = set()
        key = (tag, value if isinstance(value, (bool, int, float, str, type(None))) else id(value))
        if key in visited:
            return []
        visited.add(key)
        if depth > max_depth:
            return []

        indent = "  " * depth
        if _values_match(snapshot.get(tag), value):
            return []

        if pdg.tag_roles.get(tag) == TagRole.INPUT:
            print(f"{indent}{tag}={value} -- INPUT")
            return [(tag, value, "input")]

        writers = pdg.writers_of.get(tag, frozenset())
        if not writers:
            print(f"{indent}{tag}={value} -- no writers")
            return [(tag, value, "no_writers")]

        actions = []
        for ri in sorted(writers):
            node = pdg.rung_nodes[ri]
            ro = resolve_rung(program, node)
            if ro is None:
                continue
            wv = _written_value_for_tag(ro, tag)
            if isinstance(wv, Literal) and not _values_match(wv.value, value):
                continue

            scope = f"sub:{node.subroutine}" if node.subroutine else "main"
            print(f"{indent}{tag}={value} -- rung {ri} ({scope})")

            sp = ro.sp_tree()
            if sp is not None:
                expr = _sp_to_expr(sp)
                conds = _extract_condition_values(expr)
                for ct, cvs in conds.items():
                    for cv in cvs:
                        actions.extend(trace_back(ct, cv, snapshot, pdg, program,
                                                  visited=visited, depth=depth+1, max_depth=max_depth))

            if node.subroutine:
                for ci, cn in enumerate(pdg.rung_nodes):
                    if node.subroutine in cn.calls:
                        call_ro = resolve_rung(program, cn)
                        if call_ro is None:
                            continue
                        call_sp = call_ro.sp_tree()
                        if call_sp is not None:
                            call_expr = _sp_to_expr(call_sp)
                            call_conds = _extract_condition_values(call_expr)
                            for ct, cvs in call_conds.items():
                                for cv in cvs:
                                    actions.extend(trace_back(ct, cv, snapshot, pdg, program,
                                                              visited=visited, depth=depth+2, max_depth=max_depth))

            csb = copy_source_binding(ro, tag, value)
            if csb is not None:
                src_tag, src_val = csb
                print(f"{indent}  data-flow: {tag}={value} <- copy({src_tag})")
                actions.extend(trace_back(src_tag, src_val, snapshot, pdg, program,
                                          visited=visited, depth=depth+1, max_depth=max_depth))
            break  # first viable writer only

        return actions

    print("\n" + "=" * 60)
    print("PILOT backward trace: y_BurnerLoop=True from cold start")
    print("=" * 60)

    plc = PLC(logic)
    pdg = build_program_graph(logic)
    snapshot = dict(plc.state.tags)

    print(f"\nTags in program: {len(pdg.tag_roles)}")
    print(f"Writers: {len(pdg.writers_of)}")
    print(f"INPUT tags: {sum(1 for r in pdg.tag_roles.values() if r == TagRole.INPUT)}")

    print("\n--- Trace from y_BurnerLoop ---")
    actions = trace_back("y_BurnerLoop", True, snapshot, pdg, logic, max_depth=8)
    print(f"\nLeaf actions ({len(actions)}):")
    for tag, val, reason in actions:
        print(f"  {tag} = {val}  ({reason})")

    # Also try the PILOT one-at-a-time loop
    print("\n" + "=" * 60)
    print("PILOT loop: apply one action at a time, re-trace")
    print("=" * 60)

    plc2 = PLC(logic)
    # Force physical permissives (same as manual sequence)
    for name, value in {
        "x_DoorClosed": True,
        "x_LintDoorClosed": True,
        "x_BlowerFB": True,
        "x_RotateFB": True,
        "x_RotateSensor": False,
        "x_SailRelay": True,
    }.items():
        plc2.force(name, value)

    budget = 2500
    attempt = 0
    last_state = None

    while plc2.state.scan_id < budget:
        attempt += 1
        snapshot2 = dict(plc2.state.tags)

        # Monitor key tags
        sc = snapshot2.get("S_StateCurrent", 0)
        mode = snapshot2.get("S_UnitModeCurrent", 0)
        heat_step = snapshot2.get("Heat_CurStep", 0)
        burner = snapshot2.get("y_BurnerLoop", False)

        state_str = f"Mode={mode} State={sc} Heat_CurStep={heat_step}"
        if state_str != last_state:
            print(f"\n  [scan {plc2.state.scan_id:04d}] {state_str} y_BurnerLoop={burner}")
            last_state = state_str

        if _values_match(burner, True):
            print(f"\n  TARGET REACHED at scan {plc2.state.scan_id}!")
            return 0

        actions2 = trace_back("y_BurnerLoop", True, snapshot2, pdg, logic, max_depth=8)

        if not actions2:
            # Animate rotate sensor and step
            plc2.force("x_RotateSensor", (plc2.state.scan_id // 50) % 2 == 0)
            plc2.step()
            continue

        tag, value, _ = actions2[0]
        if state_str != last_state or attempt <= 20:
            print(f"    >> patch({tag}={value})")
        plc2.patch({tag: value})
        plc2.force("x_RotateSensor", (plc2.state.scan_id // 50) % 2 == 0)
        plc2.step()

    print(f"\n  BUDGET EXHAUSTED at scan {plc2.state.scan_id}")
    return 1


if __name__ == "__main__":
    rc = main()
    pilot_trace()
    raise SystemExit(rc)
