"""Lesson 6: Counters — docs/learn/counters.md"""

# --- The ladder logic way ---

from pyrung import Bool, Counter, Program, Rung, PLC, count_up, rise

BinASensor  = Bool("BinASensor")
BinBSensor  = Bool("BinBSensor")
BinACounter = Counter.named(1, "BinACounter")
BinBCounter = Counter.named(2, "BinBCounter")
CountReset  = Bool("CountReset")

with Program() as logic:
    with Rung(rise(BinASensor)):
        count_up(BinACounter, preset=10) \
            .reset(CountReset)
    with Rung(rise(BinBSensor)):
        count_up(BinBCounter, preset=10) \
            .reset(CountReset)

# --- Try it ---

with PLC(logic) as plc:
    # Simulate 3 boxes into Bin A
    for _ in range(3):
        BinASensor.value = True
        plc.step()
        BinASensor.value = False
        plc.step()

    assert BinACounter.Acc.value == 3
    assert BinACounter.Done.value is False

    # Simulate 7 more
    for _ in range(7):
        BinASensor.value = True
        plc.step()
        BinASensor.value = False
        plc.step()

    assert BinACounter.Acc.value == 10
    assert BinACounter.Done.value is True   # Batch complete!
