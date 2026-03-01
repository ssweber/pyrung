"""Zero-slot CircuitPython example: onboard switch drives onboard LED latch."""

from pyrung import Program, Rung, latch, reset
from pyrung.circuitpy import P1AM, board, generate_circuitpy

with Program() as logic:
    with Rung(board.switch):
        latch(board.led)
    with Rung(~board.switch):
        reset(board.led)


hw = P1AM()  # No slots required when only onboard board tags are used.
source = generate_circuitpy(logic, hw, target_scan_ms=10.0)
print(source)
