# Burner PILOT handoff — rotate-sensor liveness via round-by-round holds

Goal: `pilot_how(plc, y_BurnerLoop, choice=1)` reaches **Execute(6)** then
`y_BurnerLoop=True` on the real Click burner. The last mile is **liveness**: once
in Execute the rotate sensor (`x_RotateSensor`) must *oscillate* or a watchdog
faults the SFC. Making PILOT discover and drive that oscillation is the active
work.

- CLICK project: `C:\Users\ssweb\AppData\Local\Temp\CLICK (00010A00)\pyrung_project`
  (set `PYRUNG_CLICK_PROJECT` to this — some diag scripts still default to a stale path).
- `choice=1` = ProductionMode.
- **Focused driver (use this — no 7-min cold start):**
  `scratchpad/burner/pilot_rotate_liveness.py` drives to Execute(6) with the
  rotate sensor parked False (~815 scans), then runs `pilot_events` toward
  `y_BurnerLoop` from that state. Run with `$env:PYRUNG_CLICK_PROJECT` set.

## Current state (read this first)

We are mid-pivot. Two commits on `dev`:

- **`b856574` refactor(pilot): replace dwell-guessing LivenessHold with
  conditional holds.** Introduced `ConditionalHold` (a dwell-free hold carried as
  a `forced_holds` dict value) + structural `_liveness_hypotheses`. **⚠️ This
  regresses the live burner — see "The regression" below.** The *carrier* is
  fine; the *structural synthesis* is what's broken.
- **`4ed73e4` feat(runner): add when().do() reactive breakpoint action.**
  `plc.when(cond).do(callback)` runs `callback(state)` every scan the condition
  holds and continues. This is the runner-native carrier for reactive holds, and
  it is **fold-safe** (proven). Reactive holds use `patch` (one-shot), not
  `force` — a force would pin the input so the program could never drift it. This
  is step 1 of the agreed pivot.

Synthetic pilot tests are green, but **they pass while the live burner is broken**
— the shaft-rotate fixture happens to let both polarities resolve. Don't trust
them as live evidence.

## The regression (the crux)

`_liveness_hypotheses` (structural synthesis) can only ever resolve the
**currently-unsatisfied** polarity, because `trace_back(tag, value, snap)`
short-circuits when `snap` already holds `value` (returns no steerable leaves).

Proven by `scratchpad/burner/diag_polarity_resolution.py` on the real burner —
from a parked-False Execute incident where `SensorOffWD` fired:

```
SensorOnWD : reset(~i_RotateSensor)  resetting_val=False  trace_back(False) -> []   (already False — dropped)
SensorOffWD: reset(i_RotateSensor)   resetting_val=True   trace_back(True)  -> [x_RotateSensor]
```

So synthesis emits a **one-sided** "drive True" hold → sticks the sensor True →
trips `SensorOnWD` → no oscillation. You can never see both polarities from one
incident this way. The old `LivenessHold` worked because it derived a symmetric
wave from watchdog *presets*, not from `trace_back`.

**This is why round-by-round is the right model:** each step only needs the
currently-wrong polarity, which is exactly the one `trace_back` *can* resolve.

## The model we're building (agreed with Sam)

Round-by-round, with the ejection as the feedback signal:

```
park False → coast → SensorOffWD trips → demand True  (resolvable: it's currently False)
drive True → coast → SensorOnWD trips  → demand False (now resolvable: it's currently True)
both rules present → sensor oscillates → RunDelay completes → y_BurnerLoop
```

- **Carrier:** `plc.when(x_RotateSensor != v).do(lambda s: plc.patch({x_RotateSensor: v}))`,
  one per polarity. Already shipped (`4ed73e4`). Fold-safe: the per-scan patch is a
  visible change, so `run_until(fold=True)` steps scan-by-scan *only* while a rule
  is firing and folds the idle spans normally. Use `patch` (one-shot), not `force`
  (which would pin the input).
- No dwell, no structural enumeration of watchdogs, no reliance on finding both
  sides up front.

## Rotate watchdog structure (reference — `subroutines/rotate.py` R10–R12)

- **R10 `SensorOnWD`** 2 s, enable `Rotate_CurStep>=3 AND i_RotateSensor`,
  `reset(~i_RotateSensor)` → counts while sensor **True** → demands **False**.
- **R11 `SensorOffWD`** 10 s, enable `Rotate_CurStep>=3`, `reset(i_RotateSensor)`
  → counts while sensor **False** → demands **True**.
- **R12** either Done → `Rotate_Error=2` → ejects Execute→Aborting(8).
- `i_RotateSensor` is the input image of steerable `x_RotateSensor` (identity bridge).

## Remaining steps

2. **Revert structural `_liveness_hypotheses`** → emit the single currently-wrong
   polarity demand per ejection, carried as a `.when().do()` hold (the one
   `trace_back` resolves). Replaces the ConditionalHold coast-forcing path.
3. **Acceptance: "ejected on a NEW cause = progress"** (`verify.py` / `progress.py`).
   This is *the* replay-isolation fix: a one-sided hold must not be rejected for
   still ejecting — it fixed its own watchdog; the complement's ejection is the
   next round. Without this, round-by-round can't accumulate the second rule.
4. **`trace_back` quasi-trigger** (Sam's point): a held value that is the
   *enabling condition* of an accumulating watchdog is a traceable cause, not a
   satisfied no-op. General form of the short-circuit above; also improves causal
   attribution.
5. **Live-loop routing.** From Execute, the rotate ejection (→Aborting 8) is
   currently classified `zoom_accepted`/progress and the loop wanders to
   `Heat_CurStep` instead of investigating (observed via `pilot_rotate_liveness.py`).
   The terminal-letrun ejection must reach `_investigate_and_revert`. Fix A
   (`27372c1`) was meant to catch this; confirm why it doesn't fire from this
   pre-positioned state. **No cold start needed — drive from Execute.**

**Open ordering decision:** do step 5 first (confirm the ejection reaches
investigation at all — shapes what 2+3 land into) or build 2+3 on the synthetic
fixture first. Leaning toward working straight from Execute via the focused driver.

## Key files

- `core/runner.py` — `when().do()`: `_BreakpointBuilder.do`, `_register_breakpoint`
  (action `"do"` + `callback`), `_evaluate_breakpoints`.
- `pilot/investigate.py` — `_liveness_hypotheses` (revert to round-by-round),
  `_resetting_polarity`, `_SnapView`, `build_replay_fn`.
- `pilot/progress.py` — `_monitor_trend` + `_investigate_and_revert` (Fix A); the
  "new-cause = progress" acceptance change lands here / in `verify.py`.
- `pilot/verify.py` — gate pipeline / outcome classification.
- `pilot/_ops.py` — `ConditionalHold`/`_HoldRule`/`_split_holds`/`_coast_holding_state`
  (the conditional coast may be superseded by `.when().do()`).
- `pilot/steer.py` — `_try_terminal_letrun`.
- `pilot/trace.py` — `trace_back` (the quasi-trigger change).

## Diagnostics & tests (this work)

- `scratchpad/burner/pilot_rotate_liveness.py` — focused driver: Execute → pilot → y_BurnerLoop.
- `scratchpad/burner/diag_polarity_resolution.py` — proves the trace_back short-circuit.
- `scratchpad/burner/diag_liveness_incident.py` — builds the real rotate incident, runs
  `_liveness_hypotheses` (stale CLICK path — set `PYRUNG_CLICK_PROJECT`).
- `tests/core/test_breakpoints_labels.py` — `.do()` tests incl. fold-correctness.
- `tests/core/analysis/test_pilot_ops.py` — `TestConditionalHold`, split/coast/install.
- `tests/core/analysis/test_pilot_investigate.py` — `TestLivenessHypotheses`,
  `TestShaftRotateLiveness`. **These assert the structural-synthesis behavior and must be
  rewritten when step 2 lands** (they currently pass while live is broken).

## Click state encoding
0 undefined, 1 CLEARING, 2 STOPPED, 3 STARTING, 4 IDLE, 5 SUSPENDED,
**6 EXECUTE**, 7 STOPPING, 8 ABORTING, 9 ABORTED, 10 HOLDING, 11 HELD, …
Startup: `9→(Clear)1→2→(Reset)15→4→(Start)3→(coast)6`.
