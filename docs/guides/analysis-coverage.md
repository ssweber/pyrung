# Test Coverage

Is my testing complete? These tools need the program and a test suite.

See also: [Program Structure](analysis-structure.md) (static analysis), [Diagnosis](analysis-diagnosis.md) (snapshot-only debugging), [Cause & Effect](analysis-causal.md) (causal chains over scan history).

## Cold and hot rungs

```python
with PLC(logic) as plc:
    StartBtn.value = True
    plc.run(cycles=10)

    plc.query.cold_rungs()   # rung numbers that never fired
    plc.query.hot_rungs()    # rung numbers that fired every scan
```

Cold rungs are dead code or untested paths. Hot rungs may indicate always-true conditions worth reviewing.

Rung numbers are **1-indexed**. The first rung is `1`, matching the `Rung N` labels shown by `why()`/`cause()` and the debugger. The same 1-indexed numbers appear in the coverage report and the whitelist.

## Stranded bits

```python
stranded = plc.query.stranded_bits()
```

Returns `CausalChain` objects for each latched tag with no reachable reset path. Each chain carries blocker diagnostics pointing at the specific inputs that would need to transition.

The ladder lint [`COIL_STUCK_HIGH`](ladder-lints.md#rule-reference) checks structure. It asks "is there a reset rung at all?" `stranded_bits()` checks reachability. It asks "is there a reset rung *and can it actually fire*?"

## Wait edges without escape

```python
for finding in plc.query.wait_edges_without_escape():
    print(finding.message)
    # Rotate step 1 waits on i_RotateFB with no escape — R9 guards
    # Rotate_CurStep == 3 and R5 needs Rotate_EnableLimit, which nothing sets
    # (rests at 0)
```

A step that only advances when something outside the program arrives is a *wait edge*. If nothing else can fire while the step waits, the machine sits there looking fine. This survey is static, so it needs no history. It reports the absence of an escape the program can fire unaided.

The survey assumes a tag that nothing in the program sets stays at its resting value. A guard on such a tag makes the advance a wait. An escape doesn't count if:

- its guard needs such a tag to change, like a timeout enabled by a config register nobody set, or an abort waiting on a button
- its step guard leaves out the waiting step

A config register and a button look the same in a declaration. So the survey reports "nothing sets this" and leaves the reason to you.

The survey never edits the program. The same finding is the [`STEP_NO_ESCAPE`](ladder-lints.md#rule-reference) ladder lint, so `logic.check()` reports it with no extra call.

**Reach.** It recognizes step machines that advance by `math(Step + 1, Step)` or by copying a constant into it (`copy(2, Step)`), gated on a level or a rising edge, waiting on a contact or an analog threshold. It stays silent on shapes it cannot read, such as a `fall()`-gated advance, an `Or` guard or a drum. It does not guess.

## Coverage reports and merge

Individual test findings are mostly noise, because a single test only exercises a slice of the program. The signal emerges when you merge findings across a test suite.

```python
from pyrung.core.analysis.query import CoverageReport

def test_start_stop(plc):
    StartBtn.value = True
    plc.run(cycles=5)
    StopBtn.value = True
    plc.step()
    return plc.query.report()

def test_fault_handling(plc):
    plc.force(Fault, True)
    plc.run(cycles=5)
    return plc.query.report()
```

`CoverageReport.merge()` combines findings across tests:

```python
merged = report_a.merge(report_b)
```

Negative findings (cold rungs, stranded bits) merge by **intersection**. A rung is only cold in the merged view if *no* test fired it. Each test you add can only shrink the residuals. What remains after the full suite is what you actually need to investigate.

Stranded bits merge by chain identity (tag + blocker fingerprint), so "stranded for a different reason" after a refactor is a distinct signal from "still stranded."

## Pytest plugin

The manual merge above works, but the `pyrung.pytest_plugin` handles it automatically. Enable it in your `conftest.py`:

```python
pytest_plugins = ["pyrung.pytest_plugin"]
```

Then wire the `pyrung_coverage` fixture into your PLC fixture:

```python
@pytest.fixture
def plc(pyrung_coverage):
    with PLC(logic, dt=0.1) as p:
        yield p
        pyrung_coverage.collect(p)
```

Every test that uses `plc` contributes a report. At session end, the plugin merges all reports and writes `pyrung_coverage.json`:

```json
{
  "cold_rungs": [22, 91],
  "hot_rungs": [1, 3, 4],
  "stranded_chains": []
}
```

Control the output path with `--pyrung-coverage-json`:

```bash
pytest --pyrung-coverage-json=build/coverage.json   # custom path
pytest --pyrung-coverage-json=                       # disable output
```

## Whitelist and CI gating

A TOML whitelist declares known-acceptable findings. Examples are cold rungs you've decided are dormant by design, and stranded bits that are operator-only and not testable from software:

```toml
# pyrung_whitelist.toml

[cold_rungs]
allow = [22, 91, 104]

[stranded_chains]
allow = ["Sts_SpecialFault", "Sts_ManualReset"]
```

Pass it with `--pyrung-whitelist`:

```bash
pytest --pyrung-whitelist=pyrung_whitelist.toml
```

New findings not in the whitelist fail the session (exitstatus 1) and print a summary:

```
=============================== pyrung coverage ===============================
New cold rungs not in whitelist: [200, 201]
New stranded bits not in whitelist: ['Sts_NewFault']
```

The whitelist keys stranded bits by tag name only, not by blocker fingerprint. If a refactor changes *why* a bit is stranded, the whitelist still covers it. The JSON report's chain identity will differ, which surfaces the change for review.

With one test, cold rungs and stranded bits are mostly noise. After hundreds of tests, anything still in the residual has had hundreds of chances to be exercised and wasn't. That's where the whitelist becomes a short list of deliberate decisions rather than a pile of false positives.

## Next steps

- [Program Structure](analysis-structure.md) — DataView and simplified forms
- [Ladder Lints](ladder-lints.md) — static checks for ladder logic
- [Diagnosis](analysis-diagnosis.md) — snapshot-only debugging with `why()` and `how()`
- [Cause & Effect](analysis-causal.md) — causal chains over scan history
- [Verification](verification.md) — prove properties hold, fault coverage, lock files
- [Testing Guide](testing.md) — forces as fixtures, forking, monitors, breakpoints
