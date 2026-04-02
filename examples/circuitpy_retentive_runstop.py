"""Minimal P1AM example for hardware-testing SD retentive storage and RUN/STOP.

Slot 1 (P1-08SIM): ch1 = count button, ch2 = reset button
Slot 2 (P1-15TD2): ch1 = run indicator (ON while running), ch2 = count > 0

Retentive tags: Count survives power cycles via SD card.
RunStopConfig:  board switch toggles RUN/STOP; STOP forces outputs off.

Expected serial output on boot:
  Mode: RUN
  Retentive load skipped: [Errno 2] No such file/directory: /sd/memory.json
    (or "Retentive storage unavailable: ..." if no SD card)
    (or loaded count value if memory.json exists)

Flipping the board switch prints:
  Mode: STOP   (run indicator OFF, count indicator OFF)
  Mode: RUN    (run indicator ON, count resumes from retentive value)
"""

from pyrung import Int, Program, Rung, copy, out, rise
from pyrung.circuitpy import P1AM, RunStopConfig, board, generate_circuitpy

# ── Hardware ──────────────────────────────────────────────────────────────
hw = P1AM()
inputs = hw.slot(1, "P1-08SIM")
outputs = hw.slot(2, "P1-15TD2")

CountButton = inputs[1]
ResetButton = inputs[2]

RunIndicator = outputs[1]
CountIndicator = outputs[2]

# ── Tags ──────────────────────────────────────────────────────────────────
Count = Int("Count")  # retentive by default

# ── Logic ─────────────────────────────────────────────────────────────────
with Program() as logic:
    # Increment Count on rising edge of button
    with Rung(rise(CountButton)):
        copy(Count + 1, Count)

    # Reset Count when reset button pressed
    with Rung(ResetButton):
        copy(0, Count)

    # Run indicator: always ON while in RUN mode (STOP forces it off)
    with Rung():
        out(RunIndicator)

    # Save retentive memory after each count change
    with Rung(rise(CountButton) | ResetButton):
        out(board.save_memory_cmd)

    # Count indicator: ON when count > 0
    with Rung(Count > 0):
        out(CountIndicator)

# ── Generate ──────────────────────────────────────────────────────────────
result = generate_circuitpy(
    logic,
    hw,
    target_scan_ms=10.0,
    watchdog_ms=5000,
    runstop=RunStopConfig(),
)
print(result.code)
