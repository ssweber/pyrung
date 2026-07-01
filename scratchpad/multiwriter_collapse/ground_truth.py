"""Ground-truth: confirm the clobber is a NON-RETENTIVE (out) phenomenon only,
and that the duplicate_out validator flags it — so it is not a route PILOT must
plan, it is a conflict the validator rejects.

For each program, drive the steerable manual input alone and report the
end-of-scan Cmd value, plus whether the static validator flags a conflict.
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, copy, latch, out, rung
from pyrung.core.validation.duplicate_out import validate_conflicting_outputs


def run(label, logic, drive):
    plc = PLC(logic, dt=0.010)
    plc.step()
    plc.patch(drive)
    plc.step()
    cmd = plc.state.tags["Cmd"]
    try:
        report = validate_conflicting_outputs(logic)
        flagged = [f.target_name for f in report.findings]
    except Exception as e:  # noqa: BLE001
        flagged = f"<validator error: {e}>"
    print(f"{label}")
    print(f"   drive {drive} -> Cmd={cmd}   validator_conflicts={flagged}")


def multi_latch():
    Manual = Bool("Manual", external=True)
    Detect = Bool("Detect", external=True)
    State = Int("State")
    Cmd = Bool("Cmd")
    with Program() as logic:
        with rung(Detect):
            copy(5, State)
        with rung(Manual):
            latch(Cmd)
        with rung(State == 5):
            latch(Cmd)
    return logic


def multi_out():
    Manual = Bool("Manual", external=True)
    Auto = Bool("Auto", external=True)
    Detect = Bool("Detect", external=True)
    State = Int("State")
    Cmd = Bool("Cmd")
    with Program() as logic:
        with rung(Detect):
            copy(5, State)
        with rung(Manual):
            out(Cmd)
        with rung(Auto, State == 5):
            out(Cmd)
    return logic


print("P1 multi-LATCH (retentive): hold Manual only")
run("  ", multi_latch(), {"Manual": True})
print("\nP2 multi-OUT (duplicate coil): hold Manual only")
run("  ", multi_out(), {"Manual": True})
print("\nP2 multi-OUT: hold Auto + Detect (drive the LAST out's condition)")
run("  ", multi_out(), {"Auto": True, "Detect": True})
