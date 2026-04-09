"""Lesson 9: Structured Tags and Blocks — docs/learn/structured-tags.md"""

# --- UDTs ---

from pyrung import udt, Bool, Int, Counter, Program, Rung, PLC, out, rise, count_up

@udt(count=2)
class Bin:
    Sensor: Bool
    Full: Bool

BinACounter = Counter.named(1, "BinACounter")
BinBCounter = Counter.named(2, "BinBCounter")
CountReset  = Bool("CountReset")

# --- Blocks ---

from pyrung import Block, TagType, copy, blockcopy

SortLog  = Block("SortLog", TagType.INT, 1, 5)    # SortLog1..SortLog5
BoxSize  = Int("BoxSize")
NewBox   = Bool("NewBox")

with Program() as logic:
    with Rung(rise(Bin[1].Sensor)):
        count_up(BinACounter, preset=10) \
            .reset(CountReset)
    with Rung(rise(Bin[2].Sensor)):
        count_up(BinBCounter, preset=10) \
            .reset(CountReset)

    with Rung(BinACounter.Done):
        out(Bin[1].Full)
    with Rung(BinBCounter.Done):
        out(Bin[2].Full)

    # Log box sizes: shift register pattern
    with Rung(rise(NewBox)):
        blockcopy(SortLog.select(1, 4), SortLog.select(2, 5))  # Shift down
        copy(BoxSize, SortLog[1])                                # Insert at front

# --- Try it ---

with PLC(logic) as plc:
    # 3 boxes into Bin 1
    for _ in range(3):
        Bin[1].Sensor.value = True
        plc.step()
        Bin[1].Sensor.value = False
        plc.step()

    assert BinACounter.Acc.value == 3
    assert BinBCounter.Acc.value == 0    # Bin 2 untouched
    assert Bin[1].Full.value is False

    # Log 3 box sizes
    for size in [150, 80, 200]:
        BoxSize.value = size
        NewBox.value = True
        plc.step()
        NewBox.value = False
        plc.step()

    # Newest first
    assert SortLog[1].value == 200
    assert SortLog[2].value == 80
    assert SortLog[3].value == 150
