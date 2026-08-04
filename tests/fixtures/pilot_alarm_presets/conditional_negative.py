"""Fixture: a consequential negative timer read before first completion.

The timer runs before the alarm latch. With the default 20 ms preset, its
first 10 ms scan leaves ``Done`` false and the later rung observes that exact
negative value while latching ``Consequence``. Shortening the preset after the
scan cannot undo the committed latch. Supplying the short preset before the
scan prevents the latch, while ``Clear`` models a separate current-state
recovery for a consequence which already committed.
"""

from pyrung import PLC, Bool, Int, Program, Timer, latch, on_delay, reset, rung

DEFAULT_PRESET_MS = 20
PREVENTING_PRESET_MS = 0

PresetMs = Int(
    "ConditionalNegativePresetMs",
    default=DEFAULT_PRESET_MS,
    external=True,
)
Clear = Bool("ConditionalNegativeClear", external=True)
Consequence = Bool("ConditionalNegativeConsequence")
Watchdog = Timer.clone("ConditionalNegativeWatchdog")


with Program() as logic:
    with rung():
        on_delay(Watchdog, PresetMs)

    with rung(~Watchdog.Done):
        latch(Consequence)

    with rung(Clear):
        reset(Consequence)


def late_preset_then_clear() -> tuple[bool, bool, bool]:
    """Return the committed, late-preset, and independently-cleared states."""

    plc = PLC(logic, dt=0.010)
    plc.step()
    committed = plc.state.tags[Consequence.name]

    plc.force(PresetMs, PREVENTING_PRESET_MS)
    plc.step()
    after_late_preset = plc.state.tags[Consequence.name]

    plc.force(Clear, True)
    plc.step()
    after_clear = plc.state.tags[Consequence.name]
    return committed, after_late_preset, after_clear


def prevented_from_source() -> bool:
    """Return the consequence when the preset is corrected before its read."""

    plc = PLC(logic, dt=0.010)
    plc.force(PresetMs, PREVENTING_PRESET_MS)
    plc.step()
    return plc.state.tags.get(Consequence.name, Consequence.default)


if __name__ == "__main__":
    retained = late_preset_then_clear()
    prevented = prevented_from_source()

    print(f"Committed, late preset, clear: {retained}")
    print(f"Prevented at source: {prevented}")

    assert retained == (True, True, False)
    assert prevented is False
