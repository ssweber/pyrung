# Lesson 11: From Simulation to Hardware

> *"The tech (maybe you) at 3am will thank you."*

You started with a button that turned on a motor. You ended with a tested, deployable conveyor sorting station -- start/stop/e-stop, auto and manual modes, a state-driven sorting sequence, structured bin counting, and a test suite that proves it all works. The tests you wrote in [Lesson 10](testing.md) are still your safety net. pyrung's simulation behavior matches its codegen output -- the same assertions that proved your sorting logic in pytest will hold on the target hardware. That's the bargain: you don't have to test on hardware because you already tested in simulation.

Everything from here is about taking what you've built and connecting it to the physical world.

!!! warning "About this example"

    pyrung and the conveyor sorting station in this guide are provided as an educational example, "as-is" with no expressed or implied warranty. If you adapt any of this code for a real application, **it is your responsibility to completely modify, integrate, and test it to ensure it meets all system and safety requirements for your intended use.**

    Like all general-purpose PLCs, the hardware targeted in this lesson is not fault-tolerant and is not designed, manufactured, or intended for use in hazardous environments requiring fail-safe performance -- nuclear facilities, aircraft navigation, air traffic control, life support, or weapons systems -- where failure could lead directly to death, personal injury, or severe environmental damage.

    Real installations must follow all applicable local and national codes (NEC, NFPA, NEMA, and the codes of your jurisdiction). pyrung verifies your *logic*; it cannot verify your wiring, your safety circuit, or your machine. Get a review from a qualified controls engineer before energizing anything that can move, heat, pinch, or otherwise hurt someone.

## `StopBtn` was the warm-up. Now meet the E-stop.

You've been writing `~StopBtn` since [Lesson 3](latch-reset.md). That's the same NC wiring convention real stop buttons use -- the bit is HIGH when healthy, LOW when pressed or broken. So you already know how fail-safe inputs read in code. The wiring is the easy part.

The hard part is **who owns the stop.** When you wired `StopBtn` to the PLC, the PLC was in charge: it read the bit, decided to call `reset(Running)`, and stopped the motor as a software decision. That works for a conveyor in the lab. It does *not* work on a machine that can hurt someone, because the PLC is not a safety device. If your scan halts, your watchdog hangs, your firmware glitches, or your output transistor welds shut, the PLC's "decision" to stop never reaches the actuator.

A real E-stop takes the PLC *out of the chain of command*. The red mushroom button wires to a dedicated **safety relay** (Pilz, Banner, ABB Jokab) rated to ISO 13849 / IEC 62061. The safety relay handles dual-channel monitoring, contact welding detection, and the actual stop circuit that drops power to dangerous outputs. The PLC reads the relay's permission contact as `EstopOK` and is *informed* -- but not in charge. If the PLC dies, the safety relay still drops the contactor.

- **`StopBtn`** -- operator says "please stop." PLC handles it in software. It's a control input.
- **`EstopOK`** -- safety relay says "the world is OK to run." PLC obeys it as a gate. It's a permission input.

Both are NC wired, but the naming tells you which is which. `~StopBtn` reads as "stop is asserted." `EstopOK` reads as "safety is satisfied" -- no negation needed because the name encodes the polarity. Same NC wiring, opposite naming, because they encode different *meanings*.

In the example code, `EstopOK` gates all outputs through `with Rung(EstopOK):` -- read that as a *demonstration* of the pattern, not a safety design.

## Three ways to deploy

Your pyrung program can reach hardware through three completely different paths. Pick the one that fits your use case -- or combine them.

| Use case | Option | What runs where |
|---|---|---|
| Prototype, HMI integration, lab work | **A: Modbus runtime** | pyrung *is* the controller, running on a laptop or Pi, speaking Modbus TCP |
| Production PLC, integrate with existing plant | **B: Click codegen** | pyrung translates to Click ladder CSVs; the PLC runs natively |
| Standalone embedded, no PLC software | **C: CircuitPython** | pyrung transpiles to a Python scan loop on a P1AM-200 |

These aren't mutually exclusive -- the same pyrung source can target all three.

## Option A: Connect via Modbus

Your pyrung program runs on your laptop, a Raspberry Pi, or whatever -- and exposes its tags as a Modbus TCP server. Anything that speaks Modbus can connect and read or write tags as if pyrung were a real Click PLC.

This covers several distinct use cases:

1. **HMI integration during development** -- connect a real HMI to your simulation, validate operator workflows before any hardware ships
2. **Soft-PLC in production** -- for non-safety-critical applications, pyrung *is* the runtime
3. **Hybrid systems** -- pyrung does the logic, a real PLC or I/O module handles field wiring via Modbus
4. **Hardware-in-the-loop testing** -- connect real sensors to a pyrung simulation that controls real outputs

HMIs, SCADA systems, [ClickNick](https://github.com/ssweber/clicknick)'s Data View window, other PLCs, or your own scripts can connect and watch box counts climb, toggle between auto and manual, and press E-stop -- all from a real interface talking to your simulated conveyor.

!!! note "Modbus is a development protocol"

    Modbus TCP is fine for development, monitoring, and HMIs. It's not a substitute for proper fieldbus protocols (EtherNet/IP, ProfiNet, EtherCAT) when you need deterministic timing or cybersecurity. Don't put Modbus on the open internet without a VPN.

## Option B: Map to a Click PLC

```python
from pyrung.click import x, y, ds, TagMap, pyrung_to_ladder

mapping = TagMap({
    StartBtn:       x[1],       # Physical input terminal 1
    StopBtn:        x[2],       # NC stop button
    EstopOK:        x[3],       # NC safety relay permission
    Auto:           x[4],
    Manual:         x[5],
    EntrySensor:    x[6],
    DiverterBtn:    x[7],
    Bin[1].Sensor:  x[8],
    Bin[2].Sensor:  x[9],
    ConveyorMotor:  y[1],       # Physical output terminal 1
    DiverterCmd:    y[2],
    StatusLight:    y[3],
})

mapping.validate(logic)                        # Check against Click constraints
pyrung_to_ladder(logic, mapping, "conveyor/")  # Export ladder CSV + nicknames
```

The `Bin[1].Sensor` mapping is the [Lesson 9](structured-tags.md) UDT in action -- `.map_to()` works on structured tag fields the same way it works on flat tags.

!!! tip "The validator is the bridge"

    pyrung lets you write rich expressions because the simulator can handle them. Click can't. `mapping.validate(logic)` catches every gap between what you wrote and what your target can run, and tells you exactly what to fix. For example, pyrung lets you write `Rung(SizeReading + Offset > Threshold)` with math directly in the condition, but Click requires you to `calc` that into a separate tag first. The validator catches this. By the time `validate()` is clean, the codegen is guaranteed to produce something the PLC can run -- the same behavior as the simulator.

`pyrung_to_ladder` generates a directory with one CSV per program, a nickname file for the tag table, and a manifest. [ClickNick](https://github.com/ssweber/clicknick)'s Guided Paste reads the manifest and walks you through importing each piece into Click Programming Software in the right order.

**What doesn't port cleanly.** Every codegen target has limits. A few things the validator will flag:

- Inline math in conditions (`SizeReading + Offset > Threshold`) -- must be a separate `calc` rung
- Complex nested `any_of`/`all_of` beyond Click's branch depth
- `Real` precision differences -- Click uses 32-bit float; Python uses 64-bit
- Timer/counter presets that exceed Click's range limits
- `named_array` structures that don't fit Click's flat memory model without manual address assignment

The validator teaches you which restrictions matter for *your* code. You don't have to learn Click's limits up front.

For a full reference on memory banks, address mapping, and `named_array` patterns for Click, see the [Click Cheatsheet](../guides/click-cheatsheet.md).

## Option C: Generate CircuitPython for a P1AM-200

```python
from pyrung.circuitpy import P1AM, generate_circuitpy

hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")   # 8-ch discrete input
outputs = hw.slot(2, "P1-08TRS")   # 8-ch discrete output

source = generate_circuitpy(logic, hw, target_scan_ms=10.0)
```

**Same source, two runtimes.** The CircuitPython codegen produces a complete Python file with a scan loop, hardware initialization, and your logic -- ready to copy to a board's flash. Same conveyor sorting station you simulated, same tests you wrote, now running on a microcontroller with real Productivity1000 I/O. No PLC software, no proprietary editor, no licensing fees, no vendor lock-in. If you can write Python, you can deploy industrial control.

---

!!! warning "Hardware will surprise you"

    Your simulation was deterministic. Your hardware is not. Sensor noise, contact bounce, ground loops, EMI, and mechanical chatter are real, and pyrung can't simulate them. When something works on the bench but misbehaves in the cabinet, you're back to oscilloscopes and multimeters. [Lesson 5](timers.md)'s `on_delay` and [Lesson 4](assignment.md)'s `rise()` are the building blocks for debounce filters -- the [Forces & Debug guide](../guides/forces-debug.md) covers patterns.

    The DAP debugger and forces work against a *running* pyrung program (Option A) -- but for Options B and C, your debugging tools are the vendor's: Click Programming Software's Data View, the P1AM-200's serial console.

## Exercise

Run `mapping.validate(logic)` on the conveyor program. What does it complain about? Pick one complaint and fix it. If it comes back clean, try adding an intentional violation -- put math directly in a `Rung()` condition -- and verify the validator catches it.

## Where to go from here

**Extend the conveyor.** Add an HMI screen via Modbus (Option A). Add Modbus comms to a weigh-scale so the sort threshold comes from real equipment. Add a recipe system using `named_array`. Each of these builds directly on what you already know.

**Explore the broader PLC landscape.** You now have enough context to engage with: PackML for state-machine standardization ([Lesson 7](state-machines.md) was the on-ramp), OPC UA for plant-floor connectivity, safety-rated controllers (Pilz, Sick, Banner) for real safety beyond what `EstopOK` demonstrates here, and IEC 61131-3 SFC for graphical state machines. None are pyrung features, but the mental model transfers.

**Go deeper in pyrung.** The tutorial covered the core -- here's what's left:

- [Data movement](../instructions/copy.md): `copy`, `blockcopy`, `fill`, type conversion
- [Math](../instructions/math.md): `calc()`, overflow behavior, range sums
- [Tag structures](../guides/tag-structures.md): named arrays, cloning, field defaults, hardware mapping
- [Drum sequencers, shift registers, search](../instructions/drum-shift-search.md): advanced pattern instructions
- [Subroutines and program control](../instructions/program-control.md): `call`, `forloop`, multi-program structure
- [Communication](../instructions/communication.md): Modbus `send`/`receive`
- [VS Code debugger](../guides/dap-vscode.md): step through scans, set breakpoints on rungs, watch tags live
- [Click PLC dialect](../dialects/click.md): full hardware mapping and validation
- [CircuitPython deployment](../dialects/circuitpy.md): generate code for P1AM-200

??? tip "The Zen of Ladder"

    Try `import pyrung.zen`.

    - *The scan cycle is fast.*
    - *Rungs giveth power and taketh away.*
    - *And order has meaning.*
    - *But use order side effects sparingly.*
    - *One coil, one rung.*
    - *Latch only when needed.*
    - *If you need a FOR loop... no you don't.*
    - *Don't forget safety.*
    - *Keep it simple.*
    - *Test it.*
    - *Use clear tags and comments.*
    - *Name the purpose, not the part... unless you need a map to find it.*
    - *PackML and state machines are a honking great idea -- let's use more of those.*
    - *The tech (maybe you) at 3am will thank you.*

---

*Built with [pyrung](https://github.com/ssweber/pyrung). Write ladder logic in Python, simulate it, test it, deploy it.*
