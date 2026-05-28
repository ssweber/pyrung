"""Test how() with both fixes applied."""
from pyrung import Bool, Int, PLC, Program, rung, copy
from pyrung.core.state import SystemState

IDLE = 0
STARTING = 1
RUNNING = 2
STOPPING = 3
FAULTED = 4

CmdStart = Bool(external=True)
CmdStop  = Bool(external=True)
CmdReset = Bool(external=True)
Fault    = Bool(external=True)
State    = Int(choices={IDLE: "IDLE", STARTING: "STARTING", RUNNING: "RUNNING",
                        STOPPING: "STOPPING", FAULTED: "FAULTED"})

with Program() as logic:
    with rung(State == IDLE, CmdStart, ~Fault):
        copy(STARTING, State)
    with rung(State == STARTING):
        copy(RUNNING, State)
    with rung(State == RUNNING, CmdStop):
        copy(STOPPING, State)
    with rung(State == STOPPING):
        copy(IDLE, State)
    with rung(Fault, State != FAULTED):
        copy(FAULTED, State)
    with rung(State == FAULTED, CmdReset, ~Fault):
        copy(IDLE, State)

print("=== IDLE -> RUNNING ===")
plc = PLC(logic)
plc.explore()
print(plc.how(State == RUNNING))
print()

print("=== FAULTED -> RUNNING ===")
tags = {"State": FAULTED, "CmdStart": False,
        "CmdStop": False, "CmdReset": False, "Fault": False}
plc2 = PLC(logic, initial_state=SystemState().with_tags(tags))
plc2.explore()
print(plc2.how(State == RUNNING))
print()

print("=== RUNNING -> RUNNING (already there) ===")
tags2 = {"State": RUNNING, "CmdStart": False,
         "CmdStop": False, "CmdReset": False, "Fault": False}
plc3 = PLC(logic, initial_state=SystemState().with_tags(tags2))
plc3.explore()
print(plc3.how(State == RUNNING))
print()

print("=== RUNNING -> IDLE ===")
print(plc3.how(State == IDLE))
