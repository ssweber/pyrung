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

Rung numbers are **1-indexed** — the first rung is `1`, matching the `Rung N` labels shown by `why()`/`cause()` and the debugger. The same 1-indexed numbers appear in the coverage report and the whitelist.

## Stranded bits

```python
stranded = plc.query.stranded_bits()
```

Returns `CausalChain` objects for each latched tag with no reachable reset path. Each chain carries blocker diagnostics pointing at the specific inputs that would need to transition.

The static validator [`COIL_STUCK_HIGH`](analysis-structure.md#rule-reference) checks structure — "is there a reset rung at all?" `stranded_bits()` checks reachability — "is there a reset rung *and can it actually fire*?"

## Coverage reports and merge

Individual test findings are mostly noise — a single test only exercises a slice of the program. The signal emerges when you merge findings across a test suite.

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

Negative findings (cold rungs, stranded bits) merge by **intersection** — a rung is only cold in the merged view if *no* test fired it. Each test you add can only shrink the residuals. What remains after the full suite is what you actually need to investigate.

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

A TOML whitelist declares known-acceptable findings — cold rungs you've decided are dormant by design, stranded bits that are operator-only and not testable from software:

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

The whitelist keys stranded bits by tag name only — not by blocker fingerprint. If a refactor changes *why* a bit is stranded, the whitelist still covers it, but the JSON report's chain identity will differ, surfacing the change for review.

With one test, cold rungs and stranded bits are mostly noise. After hundreds of tests, anything still in the residual has had hundreds of chances to be exercised and wasn't. That's where the whitelist becomes a short list of deliberate decisions rather than a pile of false positives.

## Next steps

- [Program Structure](analysis-structure.md) — DataView, simplified forms, static validators
- [Diagnosis](analysis-diagnosis.md) — snapshot-only debugging with `why()` and `how()`
- [Cause & Effect](analysis-causal.md) — causal chains over scan history
- [Verification](verification.md) — prove properties hold, fault coverage, lock files
- [Testing Guide](testing.md) — forces as fixtures, forking, monitors, breakpoints
