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
| `COIL_STUCK_HIGH` | Tag is latched but never reset anywhere in the program. |
| `COIL_STUCK_LOW` | Tag is reset but never latched anywhere in the program. |
| `TAG_READONLY_WRITE` | Write instruction targets a `readonly=True` tag. |
| `PTR_DEFAULT_BEFORE_BLOCK_START` | Exact indirect dereference like `DS[Ptr]` where `Ptr` defaults below the block start address. Most often this means a 1-based block is being indexed by a tag that still has the implicit `default=0`. |
| `TAG_CHOICES_VIOLATION` | Literal-value write to a tag whose `choices` key set doesn't include that value. |
| `TAG_FINAL_MULTIPLE_WRITERS` | More than one write site for a `final=True` tag — no mutual-exclusivity exemption. |
| `TAG_RANGE_VIOLATION` | Literal-value write outside the tag's declared `min`/`max` range. |
| `PHYS_MISSING_PROFILE` | Tag has a `Physical` profile via `link` but the linked tag has no profile defined. |
| `PHYS_ANTITOGGLE` | Opposing writes to a feedback-linked tag pair within the same scan, risking physical oscillation. |

`PTR_DEFAULT_BEFORE_BLOCK_START` is intentionally syntax-level. It checks the actual dereference tag used in `Block[Ptr]`, not whether some earlier rung computed a different intermediate pointer.

The physical-realism rules (`TAG_RANGE_VIOLATION`, `PHYS_MISSING_PROFILE`, `PHYS_ANTITOGGLE`) accept a `dt` parameter forwarded from `validate()`:

```python
report = logic.validate(dt=0.05)
```

!!! note "Stuck bits vs. stranded bits"
    `COIL_STUCK_HIGH`/`COIL_STUCK_LOW` check structure — "is there a reset rung at all?" The runtime [`plc.query.stranded_bits()`](analysis-coverage.md#stranded-bits) checks reachability — "is there a reset rung *and can it actually fire*?"

!!! note "Conflicting output exclusivity"
    The validator detects `CompareEq` different-constant pairs, `BitCondition`/`NormallyClosedCondition` complements, and range-complement pairs (`Lt`/`Ge`, `Le`/`Gt`) on caller conditions. Different subroutines with provably exclusive callers are safe.


## Next steps

- [Diagnosis](analysis-diagnosis.md) — snapshot-based debugging with `why()` and `how()`
- [Cause & Effect](analysis-causal.md) — causal chains over scan history
- [Test Coverage](analysis-coverage.md) — cold rungs, stranded bits, pytest plugin
- [Verification](verification.md) — prove properties hold, fault coverage, lock files
