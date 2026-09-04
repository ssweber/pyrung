# Ladder Lints: Static Checks for Ladder Logic

```python
ModeCommand = Int("ModeCommand", external=True)
InvalidMode = Bool("InvalidMode")

with Program() as logic:
    with rung(ModeCommand < 1, ModeCommand > 3):
        out(InvalidMode)
```

The rung intends to turn on `InvalidMode` when the command falls outside `1..3`. Rung arguments are ANDed, though, and no value can be both less than 1 and greater than 3. The mistake does not need a scan to surface:

```python
report = logic.check()
for finding in report:
    print(finding)
```

```text
[RUNG_CONTRADICTION] error
 --> Main:R1
  |
  |  with rung(ModeCommand < 1, ModeCommand > 3):
  |            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ can't both be true
  |
  = hint: did you mean Or(ModeCommand < 1, ModeCommand > 3)?
```

This is an easy mistake when transcribing a drawn ladder. Contacts placed in series mean AND; contacts on parallel branches mean OR. Multiple `rung(...)` arguments preserve the series relationship, while `Or(...)` preserves the parallel one.

The corrected guard uses parallel logic:

```python
with rung(Or(ModeCommand < 1, ModeCommand > 3)):
    out(InvalidMode)
```

`logic.check()` runs this kind of check across the whole ladder. It reports contradictory rungs, conflicting outputs, stuck coils, suspicious comparisons, invalid tag writes, and other patterns that can be established from the program alone.

For a CI gate that fails on errors:

```python
report = logic.check()
assert not report.errors(), report.summary()
```

A clean report is not a proof that the program is correct or that every path is reachable. Use [Test Coverage](analysis-coverage.md) for behavior observed across tests and [Verification](verification.md) for exhaustive state-space properties.

## Reading a report

`ValidationReport` is iterable. Each finding carries a `.code`, `.severity`, `.target_name`, and `.message`:

```python
for finding in logic.check():
    print(finding)
```

The four severities are:

| Severity | Meaning |
|---|---|
| `error` | The program is provably wrong as written. |
| `warning` | A high-confidence bug pattern that usually needs a repair. |
| `info` | A consistency or completeness issue worth reviewing. |
| `advisory` | An intent heuristic that is too uncertain for a warning. |

The report is truthy when it contains any finding. Use `assert not report` when every severity must be clean.

### Diagnostic frames

The opening diagnostic shows the standard single-site frame: a source location, the ladder as written, a caret carrying the short explanation, and a concrete repair when the lint can name one. The underline follows the offending span, so it may cover one value, one instruction target, one comparison, or all contradictory rung terms.

Findings involving several sites lead with the shared problem and then show one frame per site. This is how conflicting outputs, multiple writers, repeated comparisons, and same-scan physical toggles show the relationship rather than blaming one line. Tag-level findings with no instruction to underline show the tag location without a source frame.

`str(finding)` is the complete terminal diagnostic shown above. For integrations, `finding.display` exposes the structured `.problem`, `.frames`, and `.hint`; `finding.message` contains the frame-and-hint body without the code and severity header. Consumers do not need to parse terminal text.

## Selecting rules

All default rules run when `select` is omitted. `select` and `ignore` accept either a complete rule code or a category:

```python
logic.check(select={"RUNG"})
logic.check(select={"COIL_STUCK_HIGH", "COIL_STUCK_LOW"})
logic.check(ignore={"CMP_STATIC_ON_LEFT"})
```

Categories are the prefixes before the first underscore:

| Category | Checks |
|---|---|
| `RUNG` | Contradictory, ineffective, and redundant rung conditions |
| `COIL` | Conflicting and one-sided coil writes |
| `CMP` | Comparisons that cannot behave as intended |
| `TAG` | Writes that violate tag declarations |
| `PTR` | Unsafe indirect-pointer defaults |
| `CALL` | Unreachable or recursive subroutine calls |
| `MATH` | Definite arithmetic faults |
| `PHYS` | Physical-link completeness and realism |
| `STEP` | State-machine steps with no available escape |

Unknown codes and categories raise `ValueError`.

## Command line

Run the same checks against a module containing one `Program`:

```console
pyrung check my_program
pyrung check my_program --select RUNG CMP
pyrung check my_program --ignore CMP_STATIC_ON_LEFT
```

Use `module:variable` when a module contains more than one program. The command prints every finding and exits with status 1 when the report contains an error. Warnings, information, and advisories are shown without failing the command.

Against a program already loaded by the debugger:

```console
pyrung live check
pyrung live check RUNG
```

## Rule reference

### Rung conditions

| Code | Severity | What it detects |
|---|---|---|
| `RUNG_CONTRADICTION` | Error | A rung condition conjunction that is provably unsatisfiable, so the rung can never fire. A bare `Rung()` is intentionally always on and is not reported. |
| `RUNG_TAUTOLOGY` | Warning | A top-level `Or(...)` term that is provably always true and therefore gates nothing. The finding shows the residual condition that actually controls the rung. |
| `RUNG_REDUNDANT_TERM` | Info | An exact duplicate or provably subsumed Boolean/range term. Contradictions and tautologies take precedence over this lower-severity finding. |

### Coils

| Code | Severity | What it detects |
|---|---|---|
| `COIL_CONFLICTING_OUTPUT` | Error | Multiple `out`/timer/counter/drum/shift instructions target the same tag from paths that are not provably mutually exclusive. The last writer wins every scan. |
| `COIL_STUCK_HIGH` | Warning | A tag is latched but never reset. An `out` inside a skippable subroutine counts as a latch; see [Outputs in skippable subroutines](#outputs-in-skippable-subroutines). |
| `COIL_STUCK_LOW` | Warning | A tag is reset but never latched anywhere in the program. |

### Comparisons

| Code | Severity | What it detects |
|---|---|---|
| `CMP_ALWAYS_FALSE` | Warning | A comparison that is false for every value in its complete Bool, choices, bounded-integer, or fully understood producer domain. The finding names the operand's values and whether they come from a declaration or from its writer rungs. Open domains are left alone. |
| `CMP_ALWAYS_TRUE` | Info | A comparison that is true for every value in the same complete domains and therefore does not gate its rung. |
| `CMP_EQ_ON_MONOTONE` | Warning | Equality against a timer or counter accumulator that can step past the exact value. |
| `CMP_OPERAND_NO_WRITER` | Advisory | A numeric comparison operand has no ladder writer or declared outside source. Configured defaults, external and physical inputs, read-only constants, and numeric `0`/`1` Boolean conventions are left alone. |
| `CMP_PRESET_STAYS_ZERO` | Warning | A tag-valued timer or counter preset has an implicit zero start and no ladder writer, so completion is immediate. Configured and literal zero presets are left alone. |
| `CMP_STEPPER_VALUE_NOT_SET` | Warning | A discrete stepping tag is compared with a value that none of its understood producers can establish. Dynamic, external, and unresolved producer paths are left alone. |
| `CMP_REPEATED_STATE_VALUE` | Advisory | Literal equality comparisons against the same discrete values are repeated heavily or spread across the program. Numeric `0`/`1`-only conventions are left alone. |
| `CMP_TRUE_AT_RESET` | Warning | A timer or counter completion comparison is already true when the accumulator resets. |
| `CMP_STATIC_ON_LEFT` | Advisory | An ordered comparison may read backwards because the changing value is on the right. Equality and inequality comparisons are left alone. |

### Tags

| Code | Severity | What it detects |
|---|---|---|
| `TAG_READONLY_WRITE` | Error | A write instruction targets a readonly tag declared with `readonly=True`. |
| `TAG_CHOICES_VIOLATION` | Error | A literal write is not present in the target tag's `choices` keys. |
| `TAG_RANGE_VIOLATION` | Error | A literal write falls outside the target tag's declared `min`/`max` limits. |
| `TAG_FINAL_MULTIPLE_WRITERS` | Error | More than one write site targets a tag declared with `final=True`. Mutual exclusivity does not exempt the writers. |
| `TAG_DEAD_WRITE` | Warning | A resolved direct scalar write that ordered scan analysis proves is overwritten before any read. The first implementation deliberately punts on loops, branches, calls, subroutine returns, indirect targets, one-shots, and conditional overwrites. |

### Pointers

| Code | Severity | What it detects |
|---|---|---|
| `PTR_DEFAULT_BEFORE_BLOCK_START` | Warning | An exact indirect dereference such as `DS[Ptr]` uses a pointer whose default is below the block start. This usually means a 1-based block is indexed by a tag with the implicit `default=0`. |
| `PTR_MAY_ESCAPE_BLOCK` | Warning | A pointer's complete domain contains invalid addresses compatible with the dereference's effective guards. Guard narrowing is used only for scan-stable pointers; open domains and pointers sanitized by a proven unconditional write-before-read are left alone. |

This rule checks the actual dereference tag used in `Block[Ptr]`. It does not infer that an earlier rung computed a different intermediate pointer.

### Calls and arithmetic

| Code | Severity | What it detects |
|---|---|---|
| `CALL_NEVER_CALLED` | Info | A program-owned subroutine with no call path from Main. |
| `CALL_RECURSION` | Error | A direct or indirect recursive subroutine component. One concrete closed call path is shown for each recursive component. |
| `MATH_DIV_ZERO` | Error | A `/`, `//`, or `%` divisor in `calc` that is proved zero whenever the instruction can execute. Literal/constant zero, solely-zero closed domains, and guard-proved scan-stable tags are covered; possible-zero and control-flow-ambiguous cases are left alone. |

### Physical behavior

| Code | Severity | What it detects |
|---|---|---|
| `PHYS_MISSING_PROFILE` | Info | A tag has a `Physical` relationship through `link`, but the linked tag has no physical profile. |
| `PHYS_ANTITOGGLE` | Warning | A feedback-linked command can pulse or change state faster than its declared feedback timing. |

The physical checks, including `TAG_RANGE_VIOLATION`, use the scan interval passed as `dt`:

```python
report = logic.check(dt=0.05)
```

### State-machine steps

| Code | Severity | What it detects |
|---|---|---|
| `STEP_NO_ESCAPE` | Warning | A step's only advance requires something the program cannot supply, with no escape that can fire unaided. The machine can remain in that step forever. See [Wait edges without escape](analysis-coverage.md#wait-edges-without-escape). |

## Outputs in skippable subroutines

An `out` coil de-energizes only on scans where its instruction runs. If a conditional `call` skips the subroutine, or `return_early()` skips the rung, the coil retains its last value. The `out` therefore needs another path that can reset it:

```python
with rung(Running):
    call("run_cycle")

with subroutine("run_cycle"):
    with rung(HeaterDemand):
        out(Heater)  # COIL_STUCK_HIGH: Heater stays on when Running drops
```

The rung condition is not the issue. A reached rung that evaluates false still drives its `out` coil low. The issue is whether the scan reaches the instruction at all.

A coil is safe when its `out` instructions collectively run on every scan. One `out` in the main program does that. Mutually exclusive subroutines can also cover the full domain:

```python
Mode = Int("Mode", choices={1: "run", 2: "hold", 3: "stop"})

with rung(Mode == 1):
    call("run")
with rung(Mode == 2):
    call("hold")
with rung(Mode == 3):
    call("stop")  # every Mode value drives Heater: no finding
```

The state tag needs a closed domain through `choices=` or `min`/`max`. Without one, `Mode == 7` remains possible and the lint reports the coil. A `Bool` discriminator such as `Enable`/`~Enable` already has a closed domain. An edge-gated call such as `rise(Request)` never provides full coverage because the edge is false on nearly every scan.

## Structural checks and reachability

`COIL_STUCK_HIGH` and `COIL_STUCK_LOW` ask whether the program contains the required opposing write. [`plc.query.stranded_bits()`](analysis-coverage.md#stranded-bits) asks whether that write can actually be reached.

For conflicting outputs, the lint proves mutual exclusivity for different-constant equality comparisons, complementary Boolean contacts, and complementary ranges such as `<`/`>=` or `<=`/`>`. Different subroutines are safe when their caller conditions are provably exclusive. When exclusivity cannot be established statically, the lint keeps the finding.

## Target validation

Core ladder lints and target portability checks run separately:

```python
logic.check()
logic.validate("click", tag_map=mapping, mode="strict")
logic.validate("circuitpy", hw=hw, mode="strict")
```

`logic.validate()` without a dialect remains a compatibility alias for `logic.check()`. The dialect forms check whether a program can run on that target; they do not also run the ladder lints. See [Click PLC validation](../dialects/click.md#validation) and [CircuitPython validation](../dialects/circuitpy.md#validation) for their rule sets.
