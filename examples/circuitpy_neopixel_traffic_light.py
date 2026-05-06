"""Zero-slot CircuitPython traffic light using the onboard NeoPixel."""

from pyrung import Char, Program, rung, Timer, copy, on_delay
from pyrung.circuitpy import P1AM, board, generate_circuitpy

State = Char("State", default="r")  # r=red, g=green, y=yellow

RedTimer = Timer.clone("RedTimer")
GreenTimer = Timer.clone("GreenTimer")
YellowTimer = Timer.clone("YellowTimer")


with Program() as logic:
    with rung(State == "r"):
        on_delay(RedTimer, 3000)
    with rung(RedTimer.Done):
        copy("g", State)

    with rung(State == "g"):
        on_delay(GreenTimer, 3000)
    with rung(GreenTimer.Done):
        copy("y", State)

    with rung(State == "y"):
        on_delay(YellowTimer, 1000)
    with rung(YellowTimer.Done):
        copy("r", State)

    with rung(State == "r"):
        copy(255, board.neopixel.r)
        copy(0, board.neopixel.g)
        copy(0, board.neopixel.b)
    with rung(State == "g"):
        copy(0, board.neopixel.r)
        copy(255, board.neopixel.g)
        copy(0, board.neopixel.b)
    with rung(State == "y"):
        copy(255, board.neopixel.r)
        copy(255, board.neopixel.g)
        copy(0, board.neopixel.b)


result = generate_circuitpy(logic, P1AM(), target_scan_ms=10.0)
print(result.code)
