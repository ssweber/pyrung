"""Zero-slot CircuitPython traffic light using the onboard NeoPixel."""

from pyrung import Char, Program, Rung, Timer, copy, on_delay
from pyrung.circuitpy import P1AM, board, generate_circuitpy

State = Char("State", default="r")  # r=red, g=green, y=yellow

RedTimer = Timer.named(1, "RedTimer")
GreenTimer = Timer.named(2, "GreenTimer")
YellowTimer = Timer.named(3, "YellowTimer")


with Program() as logic:
    with Rung(State == "r"):
        on_delay(RedTimer, preset=3000, unit="Tms")
    with Rung(RedTimer.done):
        copy("g", State)

    with Rung(State == "g"):
        on_delay(GreenTimer, preset=3000, unit="Tms")
    with Rung(GreenTimer.done):
        copy("y", State)

    with Rung(State == "y"):
        on_delay(YellowTimer, preset=1000, unit="Tms")
    with Rung(YellowTimer.done):
        copy("r", State)

    with Rung(State == "r"):
        copy(255, board.neopixel.r)
        copy(0, board.neopixel.g)
        copy(0, board.neopixel.b)
    with Rung(State == "g"):
        copy(0, board.neopixel.r)
        copy(255, board.neopixel.g)
        copy(0, board.neopixel.b)
    with Rung(State == "y"):
        copy(255, board.neopixel.r)
        copy(255, board.neopixel.g)
        copy(0, board.neopixel.b)


result = generate_circuitpy(logic, P1AM(), target_scan_ms=10.0)
print(result.code)
