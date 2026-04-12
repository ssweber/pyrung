"""Zero-slot CircuitPython traffic light using the onboard NeoPixel."""

from pyrung import Char, Program, Rung, Timer, copy, on_delay
from pyrung.circuitpy import P1AM, board, generate_circuitpy

State = Char("State", default="r")  # r=red, g=green, y=yellow

RedTimer = Timer.clone("RedTimer")
GreenTimer = Timer.clone("GreenTimer")
YellowTimer = Timer.clone("YellowTimer")


with Program() as logic:
    with Rung(State == "r"):
        on_delay(RedTimer, 3000)
    with Rung(RedTimer.Done):
        copy("g", State)

    with Rung(State == "g"):
        on_delay(GreenTimer, 3000)
    with Rung(GreenTimer.Done):
        copy("y", State)

    with Rung(State == "y"):
        on_delay(YellowTimer, 1000)
    with Rung(YellowTimer.Done):
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
