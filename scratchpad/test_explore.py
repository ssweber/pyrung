"""Demo: explore() + how() on a pump start sequence with interlocks."""

from pyrung import (
    Or,
    PLC,
    Bool,
    Real,
    rung,
    Timer,
    latch,
    on_delay,
    program,
    reset,
)

# Inputs — no external=True needed
Pressure = Real()
PressureSetpoint = Real()
ValveOpen = Bool()
StartBtn = Bool()
StopBtn = Bool()
EStopBtn = Bool()

# Internal state
PressureOk = Bool()
Permissive = Bool()
Motor = Bool()
LubeTimer = Timer.clone("LubeTimer")
LubeComplete = Bool()
FaultLatch = Bool()


@program
def logic():
    # Pressure must exceed setpoint
    with rung(Pressure > PressureSetpoint):
        latch(PressureOk)
    with rung(Pressure <= PressureSetpoint):
        reset(PressureOk)

    # Lube pump needs 2s warmup after valve opens
    with rung(ValveOpen):
        on_delay(LubeTimer, 2000)
    with rung(LubeTimer.Done):
        latch(LubeComplete)
    with rung(~ValveOpen):
        reset(LubeComplete)

    # All permissives must be met
    with rung(PressureOk, ValveOpen, LubeComplete, ~FaultLatch):
        latch(Permissive)
    with rung(Or(~PressureOk, ~ValveOpen, FaultLatch)):
        reset(Permissive)

    # Start motor only when permissive and start pressed
    with rung(Permissive, StartBtn, ~StopBtn, ~EStopBtn):
        latch(Motor)

    # Stop conditions
    with rung(Or(StopBtn, EStopBtn, ~Permissive)):
        reset(Motor)

    # E-stop latches a fault
    with rung(EStopBtn):
        latch(FaultLatch)


from pyrung.core.analysis.prove import explore

print("=== Exploring pump start sequence ===\n")
graph = explore(logic, progress=True)
print(f"\nStates: {graph.state_count}")
print(f"Edges: {graph.edge_count}")

runner = PLC(logic, dt=0.010)
runner._transition_graph = graph

print("\n--- how() to start the motor ---")
path = runner.how(Motor)
print(path)

print("\n--- how() to reach a fault ---")
path2 = runner.how(FaultLatch)
print(path2)

print("\n--- how() to start motor while avoiding fault ---")
path3 = runner.how(Motor, avoid=FaultLatch)
print(path3)
