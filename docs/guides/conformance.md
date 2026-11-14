# CLICK Conformance

pyrung aims to do what a CLICK does. The conformance suite checks that. It is a set of small ladder programs that run the same way on pyrung and on a real CLICK. The expected answer is always what the CLICK does.

!!! warning "A simulator, not a substitute for testing"
    The conformance results come from one CPU (a C2-03CPU with CLICK V4.00), and yours may differ in ways the suite doesn't reach. The [known differences](#where-pyrung-knowingly-differs) are listed. Test on the real machine, with proper safeguards, before anything interacts with people. pyrung and ClickNick are independent projects, not affiliated with or endorsed by AutomationDirect.

## What a case is

A case is one sentence and a small ladder that proves it.

> A counter counts the rising edges of its rung, not the scans it is true. A bit on for scans 2..3 and 5..7 counts 2.

The ladder works out its own answer and writes it to a DS register. The test harness starts the case over Modbus, waits, and reads the register back. The harness sees only registers, so the same case runs unchanged in three places.

| Where it runs | What it checks |
|---|---|
| A pyrung `PLC`, stepped scan by scan | pyrung gives the expected answer |
| pyrung's soft PLC, over Modbus | the answer survives the Modbus server |
| A real CLICK, over Modbus | the expected answer is what a CLICK does |

The generated P1AM-200 code runs the same cases on a desktop Python.

## Confirmed on a CLICK

A case is confirmed when it passes on a real CLICK. The suite records the pass with a hash of the case's ladder. Changing the sentence, the ladder or the expected answer makes the case unconfirmed until it passes on a CLICK again.

A case that is not confirmed still runs against pyrung on every test run. Its expected answer is a hypothesis until a CLICK answers.

## Where the suite stands

pyrung 0.16.0 has 412 cases. The CLICK is a C2-03CPU with CLICK V4.00.

| State of a case | Cases |
|---|---:|
| Passed on the CLICK as it is written now | 401 |
| Records what the CLICK reads | 7 |
| Cannot run on a CLICK | 4 |
| **Total** | **412** |

A case in the second row asks the CLICK an open question, such as what survives a power cycle. Its expected range holds every answer, so a pass proves nothing. The suite records what the CLICK read instead.

The cases are spread over these areas.

| Area | Cases |
|---|---:|
| How a rung solves | 32 |
| Comparisons | 7 |
| Coils and the timed Out coil types | 36 |
| Timers and counters | 38 |
| Copy, block copy, pack and text | 59 |
| Math | 60 |
| Drums, shift and search | 99 |
| Subroutines and For loops | 30 |
| System bits, fault flags and power-up | 50 |
| Modbus addressing | 1 |

## Where pyrung knowingly differs

Seven cases record two known differences.

- pyrung keeps a `Real` at 64 bits. A CLICK REAL is a 32-bit float. See [Math](../instructions/math.md).
- A copy through a pointer past the end of the DD bank writes to memory on a CLICK. pyrung has no memory there and drops the write.

## What the suite does not reach

- `send` and `receive`. They need a second Modbus device.
- Interrupts, PID and the high-speed instructions. pyrung does not have them.
- Any CPU other than the one above. Yours may differ.
- Exact timer counts over Modbus. A CLICK's scan time is its own, so a case about time expects a range there.

## Run it

From a clone of the repository:

```bash
make test-conformance                    # against pyrung's soft PLC
make test-conformance PLC=192.168.1.10   # against a real CLICK
```

Each run prints one line per case, marked `confirmed` or `unconfirmed`, then the totals.

The CLICK must already hold the suite's ladder. `make conformance-export OUT=dir` writes it as CLICK projects, and [ClickNick](https://pyrung.com/clicknick/)'s Guided Paste loads them. The suite is larger than the 8,000 steps one CLICK project holds, so it loads in parts.

Use a CLICK with nothing wired to it. The suite writes to the PLC's memory.
