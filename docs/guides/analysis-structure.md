# Program Structure

What does my program look like? These tools work on the program alone — no runtime, no scans, no state.

See also: [Diagnosis](analysis-diagnosis.md) (snapshot-based debugging), [Cause & Effect](analysis-causal.md) (scan history), [Test Coverage](analysis-coverage.md) (test suite surveys).

## DataView: what does my program touch?

`plc.dataview` returns a chainable query over the program's static dependency graph. No scans needed — it reads the program structure directly.

```python
from pyrung import Bool, PLC, Program, rung, And, latch, reset, out

StartBtn    = Bool()
StopBtn     = Bool()
Fault       = Bool()
Running     = Bool()
MotorOut    = Bool()

with Program() as logic:
    with rung(And(StartBtn, ~Fault)):
        latch(Running)
    with rung(StopBtn):
        reset(Running)
    with rung(Running):
        out(MotorOut)

with PLC(logic) as plc:
    dv = plc.dataview
```

### Role filters

Every tag gets a role based on its position in the dependency graph:

```python
dv.inputs()      # only read, never written by logic — your physical inputs
dv.pivots()      # both read and written — internal state
dv.terminals()   # only written, never read — your physical outputs
dv.isolated()    # neither read nor written by any rung
```

Filters chain. `.inputs().contains("btn")` narrows to input tags matching "btn".

### Name matching

`.contains()` does abbreviation-aware fuzzy matching:

```python
dv.contains("cmd")      # finds CommandRun, Cmd_Reset, etc.
dv.contains("motor")    # finds MotorOut, ConveyorMotor, etc.
```

It splits on camelCase and underscores, then expands both sides into consonant abbreviations — `"cmd"` finds `CommandRun`, and `"command"` finds `Cmd_Reset`.

### Dependency slicing

```python
dv.upstream("MotorOut")    # everything that can affect MotorOut
dv.downstream("StartBtn")  # everything StartBtn can affect
```

These return narrowed DataViews, so you can chain further:

```python
dv.inputs().upstream("MotorOut")  # which inputs feed into MotorOut?
```

### Iteration

DataView is iterable and supports `len`, `in`, and `bool`:

```python
for tag_name in dv.inputs():
    print(tag_name)

assert "StartBtn" in dv
assert len(dv.pivots()) > 0
```

`.tags` returns the underlying `frozenset` of tag names. `.roles()` returns a `dict[str, TagRole]`.

### Static use without a runner

`program.dataview()` returns the same thing without needing a `PLC`:

```python
dv = logic.dataview()   # works directly on the Program
```

Useful in test utilities or static analysis scripts that don't need to run scans.

## Simplified form: what does this output actually depend on?

`program.simplified()` resolves each terminal tag's condition chain back to inputs, eliminating intermediate pivots. A 14-rung interlock chain through 10 intermediate tags becomes a two-term Boolean expression over the 8 inputs that actually matter.

```python
from pyrung import Bool, Program, rung, branch, out

EStop          = Bool()
RunPermit      = Bool()
PlantMode      = Bool()
StartBtn       = Bool()
MaintOverride  = Bool()
SafetyOK       = Bool()
Permitted      = Bool()
Running        = Bool()
SealIn         = Bool()
MotorOut       = Bool()

with Program() as logic:
    with rung(~EStop):
        out(SafetyOK)
    with rung(RunPermit, SafetyOK):
        out(Permitted)
    with rung(Permitted):
        with branch(StartBtn):
            out(Running)
        with branch(SealIn):
            out(Running)
    with rung(Running):
        out(SealIn)
    with rung():
        with branch(Running, ~EStop):
            out(MotorOut)
        with branch(MaintOverride):
            out(MotorOut)

forms = logic.simplified()
```

Each entry is a `TerminalForm` with the resolved expression and resolution stats:

```python
form = forms["MotorOut"]
form.expr          # the simplified Boolean expression tree
form.writer_count  # how many rungs write this tag
form.pivot_count   # how many intermediate tags were resolved away
form.depth         # deepest resolution chain traversed
```

Use `render()` for a human-readable string:

```python
from pyrung.core.analysis.simplified import render

render(forms["MotorOut"].expr)
# 'Or(And(RunPermit, ~EStop, Or(StartBtn, Running), ~Fault), MaintOverride)'
```

### What it tells you

The simplified form strips away organizational structure — the intermediate tags that exist to break logic into reviewable chunks — and shows the actual dependency. A 14-rung → 2-term reduction tells you: 8 inputs matter, there are 2 independent paths, and `MaintOverride` bypasses everything.

### Branch topology is preserved

Sibling branches produce `And(parent, Or(local₁, local₂))`, not the flat DNF form `Or(And(parent, local₁), And(parent, local₂))`. The series/parallel structure of the original program carries through resolution — shared preconditions appear once, with the distinguishing triggers nested inside.

### Cycles (seal-in)

Feedback loops like seal-in latches are detected and left as-is. When resolution encounters a tag it has already visited in the current chain, it stops substituting. The seal-in tag appears in the output as a leaf — indicating the latch rather than infinitely expanding.

## Static validators

Static validators check program structure at build time — no scans needed. Call `logic.validate()` to run them all:

```python
report = logic.validate()
assert not report, report.summary()
```

`ValidationReport` is falsy when clean, truthy when there are findings. It's iterable — each finding carries a `.code`, `.target_name`, and `.message`.

### Selecting rules

By default all rules run. Use `select` to limit or `ignore` to exclude by rule code:

```python
report = logic.validate(select={"COIL_STUCK_HIGH", "COIL_STUCK_LOW"})
report = logic.validate(ignore={"PHYS_ANTITOGGLE"})
```

Unknown codes raise `ValueError`.

### Rule reference

| Code | What it detects |
|---|---|
| `COIL_CONFLICTING_OUTPUT` | Multiple `out`/timer/counter/drum/shift instructions targeting the same tag from non-mutually-exclusive paths. Last-writer-wins stomping every scan. |
| `COIL_STUCK_HIGH` | Tag is latched but never reset anywhere in the program. An `out` inside a skippable subroutine counts as a latch — see below. |
| `COIL_STUCK_LOW` | Tag is reset but never latched anywhere in the program. |
| `TAG_READONLY_WRITE` | Write instruction targets a `readonly=True` tag. |
| `PTR_DEFAULT_BEFORE_BLOCK_START` | Exact indirect dereference like `DS[Ptr]` where `Ptr` defaults below the block start address. Most often this means a 1-based block is being indexed by a tag that still has the implicit `default=0`. |
| `TAG_CHOICES_VIOLATION` | Literal-value write to a tag whose `choices` key set doesn't include that value. |
| `TAG_FINAL_MULTIPLE_WRITERS` | More than one write site for a `final=True` tag — no mutual-exclusivity exemption. |
| `TAG_RANGE_VIOLATION` | Literal-value write outside the tag's declared `min`/`max` range. |
| `PHYS_MISSING_PROFILE` | Tag has a `Physical` profile via `link` but the linked tag has no profile defined. |
| `PHYS_ANTITOGGLE` | Opposing writes to a feedback-linked tag pair within the same scan, risking physical oscillation. |
| `CMP_EQ_ON_MONOTONE` | Equality against a timer or counter accumulator that can step past the exact value. |
| `CMP_OPERAND_STAYS_ZERO` | A numeric tag used in a comparison has an implicit zero start and no ladder writer, so it stays zero. Configured defaults, external inputs, and read-only zero constants are left alone. |
| `CMP_PRESET_STAYS_ZERO` | A tag-valued timer or counter preset has an implicit zero start and no ladder writer, so completion is immediate. Configured and literal zero presets are intentional and left alone. |
| `CMP_STEPPER_VALUE_NOT_SET` | A discrete stepping tag (state-machine/step/stage logic) is tested with `==` against a value that none of its understood direct copies, copy chains, or constant indirect-table producers can establish. Dynamic, external, and unresolved producer paths are left alone. |
| `CMP_TRUE_AT_RESET` | A timer/counter comparison has the completion test backwards and is already true when the accumulator resets. |
| `CMP_STATIC_ON_LEFT` | A fixed value appears on the left of a comparison and the changing value on the right. |
| `STEP_NO_ESCAPE` | A step whose only advance needs something the program cannot supply, with no escape it can fire unaided. The machine can sit there forever. See [wait edges without escape](analysis-coverage.md#wait-edges-without-escape). |

`PTR_DEFAULT_BEFORE_BLOCK_START` is intentionally syntax-level. It checks the actual dereference tag used in `Block[Ptr]`, not whether some earlier rung computed a different intermediate pointer.

The physical-realism rules (`TAG_RANGE_VIOLATION`, `PHYS_MISSING_PROFILE`, `PHYS_ANTITOGGLE`) accept a `dt` parameter forwarded from `validate()`:

```python
report = logic.validate(dt=0.05)
```

!!! note "An `out` in a subroutine is a latch"
    An `out` coil de-energizes only on the scans where it actually *runs*. A subroutine that can be skipped — a conditional `call`, or a `return_early()` above the rung — doesn't run every scan, so its coil freezes at whatever it was when the subroutine last ran. That makes the `out` a latch: something else has to `reset` the coil, and `COIL_STUCK_HIGH` says so when nothing does.

    ```python
    with Rung(Running):
        call("run_cycle")

    with subroutine("run_cycle"):
        with Rung(Heater_Demand):
            out(Heater)          # COIL_STUCK_HIGH — Heater stays on when Running drops
    ```

    The rung's own condition is not the issue — a rung that runs and evaluates false has still driven its coil low. What matters is whether the scan *reaches* the instruction at all.

    So a coil is exempt whenever its `out` instructions, taken together, run on every scan. One `out` in the main program does that. So does a state machine whose subroutines cover the state space between them:

    ```python
    Mode = Int("Mode", choices={1: "run", 2: "hold", 3: "stop"})

    with Rung(Mode == 1):
        call("run")
    with Rung(Mode == 2):
        call("hold")
    with Rung(Mode == 3):
        call("stop")     # every Mode drives Heater → no finding
    ```

    Proving that coverage needs the state tag to declare a closed domain — `choices=`, or `min`/`max`. Without one, nothing rules out `Mode == 7`, the coil is reported, and the hint asks for the declaration. A `Bool` discriminator (`Enable` / `~Enable`) needs no declaration; its domain is closed already. An edge-gated call (`rise(Request)`) never covers, since the edge is false on nearly every scan.

!!! note "Stuck bits vs. stranded bits"
    `COIL_STUCK_HIGH`/`COIL_STUCK_LOW` check structure — "is there a reset rung at all?" The runtime [`plc.query.stranded_bits()`](analysis-coverage.md#stranded-bits) checks reachability — "is there a reset rung *and can it actually fire*?"

!!! note "Conflicting output exclusivity"
    The validator detects `CompareEq` different-constant pairs, `BitCondition`/`NormallyClosedCondition` complements, and range-complement pairs (`Lt`/`Ge`, `Le`/`Gt`) on caller conditions. Different subroutines with provably exclusive callers are safe.


## Next steps

- [Diagnosis](analysis-diagnosis.md) — snapshot-based debugging with `why()` and `how()`
- [Cause & Effect](analysis-causal.md) — causal chains over scan history
- [Test Coverage](analysis-coverage.md) — cold rungs, stranded bits, pytest plugin
- [Verification](verification.md) — prove properties hold, fault coverage, lock files
