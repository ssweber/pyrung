"""Zero-slot CircuitPython example: onboard switch drives onboard LED latch."""

from pyrung import Program, rung, latch, reset
from pyrung.circuitpy import P1AM, board, generate_circuitpy

with Program() as logic:
    with rung(board.switch):
        latch(board.led)
    with rung(~board.switch):
        reset(board.led)


hw = P1AM()  # No slots required when only onboard board tags are used.
result = generate_circuitpy(logic, hw, target_scan_ms=10.0)
print(result.code)
