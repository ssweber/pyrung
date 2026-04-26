# Task: Expose harness coupling list and write fault coverage example

## Background

The `Harness` class in `src/pyrung/core/harness.py` discovers device couplings from `Physical` + `link=` annotations and stores them internally as `_bool_couplings` and `_analog_couplings` (to be renamed `_profile_couplings`). These lists contain everything needed for automated fault coverage testing — but they're private. Users can't iterate over them.

Fault coverage is the question: "for every physical device, does the program detect when it fails?" This decomposes into two checks using existing primitives:

- **Structural**: does a path from the fault to an alarm exist at all? (`prove()`)
- **Timing**: does the fault timer trip fast enough under real timing? (`force` + `run_for`)

No new engine machinery is needed. This is a public accessor on an existing list, plus an example composing existing tools.

## Changes

### 1. Rename `_analog_couplings` → `_profile_couplings`

The name "analog" is a misnomer — profile-driven couplings handle Bool pulse trains (shaft encoders) as well as Real signals (thermocouples). Rename `_AnalogCoupling` → `_ProfileCoupling` and `_analog_couplings` → `_profile_couplings` throughout `harness.py`. Update `coupling_summary()` accordingly.

### 2. Add a public `Coupling` dataclass and `couplings()` method

Create a simple public dataclass:

```python
@dataclass(frozen=True)
class Coupling:
    en_name: str
    fb_name: str
    physical: Physical
    trigger_value: int | str | None = None
```

Add a `couplings()` method to `Harness` that yields a flat iterator over both `_bool_couplings` and `_profile_couplings`, mapped to `Coupling` instances. The user shouldn't need to know whether a coupling is delay-based or profile-driven.

### 3. Create `examples/fault_coverage.py`

Write an example file demonstrating both fault coverage patterns against a small but realistic program with a few devices, a fault timer, and an `AlarmExtent` integer. The example should include:

**Structural coverage with `prove()`:**

```python
for coupling in harness.couplings():
    fb = plc.tag(coupling.fb_name)
    result = prove(logic, Or(fb, AlarmExtent != 0))
    assert isinstance(result, Proven), f"{coupling.fb_name} undetected"
```

The property reads: "in every reachable state, either the feedback is healthy or the alarm caught it." A `Counterexample` means there exists a reachable state where the feedback is off and no alarm fired — a structural detection gap.

**Timing coverage with force:**

```python
for coupling in harness.couplings():
    with PLC(logic, dt=0.001) as plc:
        harness = Harness(plc)
        harness.install()
        Cmd.value = True
        plc.run_for(0.5)
        plc.force(coupling.fb_name, False)
        plc.run_for(5.0)
        assert AlarmExtent.value != 0, f"{coupling.fb_name} not detected in time"
```

This catches fault timers that exist structurally but are too slow — the alarm path exists but takes longer than the machine can safely tolerate.

**The example should explain the workflow:** run `prove()` first to find structural gaps (no point testing timing on a coupling that never reaches an alarm). Then run the force-based tests for timing validation on the ones that passed. Same coupling list, two passes, complete fault coverage.

## Context: why this works

`prove()` uses a three-valued timer abstraction (`False`/`Pending`/`True`) that collapses accumulator state to make BFS tractable. This means it answers "can the alarm fire?" but not "does it fire in time?" — it's timing-blind by design.

The force-based test runs with real `dt`, real accumulators, real scan rates. The harness drives the program to a running state, the force breaks one feedback, and the fault timer has to actually count down and trip. This catches timing gaps that `prove()` abstracts away.

The two tools are complementary, not redundant. `prove()` is exhaustive but timing-blind. Force tests are timing-aware but scenario-bound. Together they bracket fault coverage.

## Files to reference

- `src/pyrung/core/harness.py` — `Harness`, `_BoolCoupling`, `_AnalogCoupling`
- `src/pyrung/core/analysis/prove.py` — `prove()`, `Proven`, `Counterexample`
- `docs/guides/physical-harness.md` — user-facing harness docs
- `docs/guides/analysis.md` — `prove()` docs and condition syntax
- `docs/guides/testing.md` — force patterns
