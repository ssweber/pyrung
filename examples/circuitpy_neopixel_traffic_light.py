"""Zero-slot CircuitPython traffic light using the onboard NeoPixel."""

from pyrung import Bool, Char, Int, Program, Rung, Tms, copy, on_delay
from pyrung.circuitpy import P1AM, board, generate_circuitpy

State = Char("State", default="r")  # r=red, g=green, y=yellow

RedDone = Bool("RedDone")
RedAcc = Int("RedAcc")
GreenDone = Bool("GreenDone")
GreenAcc = Int("GreenAcc")
YellowDone = Bool("YellowDone")
YellowAcc = Int("YellowAcc")


with Program() as logic:
    with Rung(State == "r"):
        on_delay(RedDone, RedAcc, preset=3000, unit=Tms)
    with Rung(RedDone):
        copy("g", State)

    with Rung(State == "g"):
        on_delay(GreenDone, GreenAcc, preset=3000, unit=Tms)
    with Rung(GreenDone):
        copy("y", State)

    with Rung(State == "y"):
        on_delay(YellowDone, YellowAcc, preset=1000, unit=Tms)
    with Rung(YellowDone):
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


source = generate_circuitpy(logic, P1AM(), target_scan_ms=10.0)
print(source)
