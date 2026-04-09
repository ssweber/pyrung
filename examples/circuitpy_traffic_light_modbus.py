"""Traffic light with Modbus server (HMI) and client (remote walk button).

Generates CircuitPython for a P1AM-200 intersection controller:
- Slot 1: P1-08SIM  (input simulator — manual override switch, local ped button)
- Slot 2: P1-08TRS  (relay outputs — red, yellow, green lights)
- Modbus TCP server  — SCADA/HMI reads current light state and timer values
- Modbus TCP client  — reads walk-request bit from a remote pedestrian panel PLC

The client receive uses a raw Modbus address (``ModbusAddress``) instead of a Click
bank string, so it works with any Modbus TCP device — not only Click PLCs.
"""

from pyrung import Bool, Char, Int, Or, Program, Rung, Timer, copy, on_delay, out, rise
from pyrung.circuitpy import (
    ModbusClientConfig,
    ModbusServerConfig,
    P1AM,
    generate_circuitpy,
)
from pyrung.click import ModbusTcpTarget, TagMap, c, ds, t, td, txt, receive, send
from pyrung.core.instruction.send_receive import ModbusAddress, RegisterType

# ── Hardware ──────────────────────────────────────────────────────────────
hw = P1AM()
inputs = hw.slot(1, "P1-08SIM")   # 8-ch discrete input simulator
outputs = hw.slot(2, "P1-15TD2")  # 15-ch discrete output (24V source)

ManualOverride = inputs[1]         # toggle: freeze current phase
LocalPedButton = inputs[2]         # local pedestrian push-button

RedLight = outputs[1]
YellowLight = outputs[2]
GreenLight = outputs[3]

# ── Tags ──────────────────────────────────────────────────────────────────
State = Char("State", default="r")  # r=red, g=green, y=yellow

RedTimer = Timer.named(1, "RedTimer")
GreenTimer = Timer.named(2, "GreenTimer")
YellowTimer = Timer.named(3, "YellowTimer")

# Walk request — received from remote pedestrian panel via Modbus client
WalkRequest = Bool("WalkRequest")
WalkActive = Bool("WalkActive")

# Modbus client status tags (transient — not retained across power cycles)
RxBusy = Bool("RxBusy")
RxOk = Bool("RxOk")
RxErr = Bool("RxErr")
RxExCode = Int("RxExCode", retentive=False)

# ── Logic ─────────────────────────────────────────────────────────────────
with Program() as logic:
    # --- State machine (frozen when ManualOverride is ON) ---
    with Rung(State == "r", ~ManualOverride):
        on_delay(RedTimer, preset=5000, unit="Tms")
    with Rung(RedTimer.Done):
        copy("g", State)

    with Rung(State == "g", ~ManualOverride):
        on_delay(GreenTimer, preset=4000, unit="Tms")
    with Rung(GreenTimer.Done):
        copy("y", State)

    with Rung(State == "y", ~ManualOverride):
        on_delay(YellowTimer, preset=1500, unit="Tms")
    with Rung(YellowTimer.Done):
        copy("r", State)

    # --- Walk request: latch on rising edge, clear after green phase ---
    with Rung(Or(rise(WalkRequest), rise(LocalPedButton))):
        out(WalkActive)
    with Rung(GreenTimer.Done):
        copy(False, WalkActive)

    # --- Drive relay outputs ---
    with Rung(State == "r"):
        out(RedLight)
    with Rung(State == "g"):
        out(GreenLight)
    with Rung(State == "y"):
        out(YellowLight)

    # --- Modbus client: read walk request from remote panel ---
    # Uses a raw Modbus address (coil 0) instead of Click "C1".
    # This talks to any Modbus device, not just a Click PLC.
    with Rung():
        receive(
            target="ped_panel",
            remote_start=ModbusAddress(0, RegisterType.COIL),
            dest=WalkRequest,
            receiving=RxBusy,
            success=RxOk,
            error=RxErr,
            exception_response=RxExCode,
        )

# ── Tag Map (Click address space for Modbus exposure) ─────────────────────
mapping = TagMap({
    State: txt[1],          # TXT1 = current phase letter
    WalkActive: c[1],       # C1 = walk active flag
    WalkRequest: c[2],      # C2 = walk request (received from remote)
    # Timers — done bits (T) and accumulators (TD)
    RedTimer.Done: t[1],
    RedTimer.Acc: td[1],
    GreenTimer.Done: t[2],
    GreenTimer.Acc: td[2],
    YellowTimer.Done: t[3],
    YellowTimer.Acc: td[3],
    # Modbus client status
    RxBusy: c[3],
    RxOk: c[4],
    RxErr: c[5],
    RxExCode: ds[1],
})

# ── Modbus configs ────────────────────────────────────────────────────────
server_cfg = ModbusServerConfig(ip="192.168.1.221")

ped_panel = ModbusTcpTarget(name="ped_panel", ip="192.168.1.122", port=502, device_id=1)
client_cfg = ModbusClientConfig(targets=(ped_panel,))

# ── Generate ──────────────────────────────────────────────────────────────
result = generate_circuitpy(
    logic,
    hw,
    target_scan_ms=10.0,
    watchdog_ms=5000,
    modbus_server=server_cfg,
    modbus_client=client_cfg,
    tag_map=mapping,
)
print(result.code)
