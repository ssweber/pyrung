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

Convert the form to a string for human-readable output:

```python
str(form)
# 'MotorOut = Or(And(RunPermit, ~EStop, Or(StartBtn, Running)), MaintOverride)'
```

### Assert inferred permissives

```python
SafetyOK = Bool()
Start = Bool()
Jog = Bool()
Motor = Bool()

with Program() as logic:
    with rung(SafetyOK):
        with branch(Start):
            out(Motor)
        with branch(Jog):
            out(Motor)

form = logic.simplified()["Motor"]
assert SafetyOK in form.permissives
```

`.permissives` contains the positive Boolean tags required by every path that
can make the terminal true. It resolves combinational pivots first, so the set
describes the effective form rather than only the contacts on the final rung.
Membership accepts a `Tag` or its logical name.

An alternate path removes a tag from the inferred set:

```python
assert RunPermit not in forms["MotorOut"].permissives  # MaintOverride bypasses it
```

`pyrung live` includes the same set in `simplified` output:

```text
Motor = And(SafetyOK, Or(Start, Jog))
  permissives: SafetyOK
  (2 writer(s), 0 pivot(s) resolved, depth 0)
```

These are inferred properties of the current program, not stored tag metadata,
and they do not add contacts or otherwise change execution.

### What it tells you

The simplified form strips away organizational structure — the intermediate tags that exist to break logic into reviewable chunks — and shows the actual dependency. A 14-rung → 2-term reduction tells you: 8 inputs matter, there are 2 independent paths, and `MaintOverride` bypasses everything.

### Branch topology is preserved

Sibling branches produce `And(parent, Or(local₁, local₂))`, not the flat DNF form `Or(And(parent, local₁), And(parent, local₂))`. The series/parallel structure of the original program carries through resolution — shared preconditions appear once, with the distinguishing triggers nested inside.

### Cycles (seal-in)

Feedback loops like seal-in latches are detected and left as-is. When resolution encounters a tag it has already visited in the current chain, it stops substituting. The seal-in tag appears in the output as a leaf — indicating the latch rather than infinitely expanding.

## Ladder lints

[`logic.check()`](ladder-lints.md) runs static checks for contradictory rungs, conflicting outputs, stuck coils, suspicious comparisons, invalid tag writes, and other structural problems. See [Ladder Lints](ladder-lints.md) for rule selection, severities, and the complete rule reference.


## Next steps

- [Diagnosis](analysis-diagnosis.md) — snapshot-based debugging with `why()` and `how()`
- [Cause & Effect](analysis-causal.md) — causal chains over scan history
- [Test Coverage](analysis-coverage.md) — cold rungs, stranded bits, pytest plugin
- [Verification](verification.md) — prove properties hold, fault coverage, lock files
