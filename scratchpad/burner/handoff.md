# Burner PILOT handoff — reactive path to Execute(6)

Goal: `pilot_how(plc, y_BurnerLoop, choice=1)` reaches **Execute(6)** on the real
Click burner program. Acceptance bar this session: Starting(3) → Execute(6).
(Full `y_BurnerLoop=True` additionally needs the rotate watchdog cleared —
`Rotate_Error` latches even at Execute — a separate concern; `x_RotateSensor`
is held False and not animated.)

- CLICK project under test:
  `C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project`
  (`PYRUNG_CLICK_PROJECT` overrides).
- Diagnostic: `scratchpad/burner/sample_pilot_events.py`
  (`PILOT_MAX_EVENTS` / `PILOT_MAX_SCANS` env vars).
- `choice=1` = ProductionMode (one of y_BurnerLoop's 3 output routes).

## FIXED prior session — fold was masking the abort

`fold` collapsed the Starting plateau and skipped the `rise(clock_1s)` edge that
recomputes `A_AlmExtent` (R67). Three commits fixed it:

- `1607a42` fix(fold): bound skip to read system-clock edges
- `e9a478c` fix(core): edge-detect rise()/fall() on derived system clocks
- `7ee39ee` fix(fold): disable fold while scan-id-derived signals are read

With honest fold: door not held → Abort(8); door held → Execute(6).

## This session — architecture + trace + investigate improvements

### Module extraction (landed)

Extracted verify.py (gate pipeline), _ops.py (shared PLC helpers), and
causal.py (cause-chain walker) from pilot.py.  steer.py, progress.py, and
types.py also split out by the other implementer.  Clean DAG:

```
_ops.py → causal.py → investigate.py → verify.py → pilot.py
```

Excursion recovery moved from verify to investigate (`investigate_excursion`) —
verify detects, investigate diagnoses + replay-validates.

### Trace improvements (landed)

1. **Aggregate decomposition** (`data_flow="aggregate"`): trace_back decomposes
   `calc(block.select(s,e).sum(), dest)` into per-element children.  For
   `A_AlmExtent != 0`, finds the non-zero ds elements and traces each one.
   New `Aggregate` type in crossing.py alongside Literal/Affine.

2. **Clock-gated writer awareness**: `rise(clock)` on unwritten non-steerable
   tags → `self_advancing=True` coast node.  Tells the pilot to wait for the
   clock edge, not steer it.

### Investigate improvements (landed)

1. **Enabler chase** (tier 2) in causal.py: when trigger roots don't reach
   steerable inputs, fall through to step.enablers.  Crosses the abort rung's
   enabler (`A_AlmExtent != 0`) to reach deeper into the alarm chain.

2. **Latch-exposure hypotheses** (tier 4) in investigate.py: scan changed_tags
   for latches that fired with an un-held steerable guard input.

3. **Heuristic-upstream hypotheses** (tier 0) in investigate.py: PDG upstream
   cone scan → propose steerable inputs at pre-incident values.

All tiers produce InvestigationHypothesis, replay-validated by build_replay_fn.

### Tests (landed)

- `test_forward_sum_returns_aggregate` — crossings forward returns Aggregate
- `test_aggregate_sum_decomposition` — trace_back decomposes sum to elements
- All 83 pilot tests pass, 145 crossings tests pass

## Current problem — zoom false positive

**Observed**: The pilot reaches Starting via C_Start (FRONTIER, distance 16→25),
then zoom with prerequisite_holds `x_BlowerFB=True, x_RotateFB=True` runs
scan 10→56 and reports CONFIRMED with trend=21.  Then trend_regression reverts
to checkpoint (scan 7, Idle/4) because 21 > 16 (best_trend).

**Expected**: The zoom should eject to Aborting because `x_DoorClosed` is not
held.  The alarm chain (R7 latch → R67 clock-gated sum → R1 abort) should fire
at the first `clock_1s` edge (~scan 50) and send S_StateCurrent to 8.

**Hypothesis**: `run_until(fold=True)` accelerates the Blower/Rotate timer
completion past the `clock_1s` edge, so S_StateComplete fires and
S_StateCurrent reaches 6 (Execute) BEFORE A_AlmExtent updates.  The zoom
stops at S_StateCurrent=6 and never sees the abort.  The fold is landing on
clock edges (our fix), but the timer fold may be completing the FBs in fewer
scans than the first clock tick.

**Fix direction**: The zoom's `run_until` should either:
- Disable fold (safe but slow — the whole point of zoom is fast coasting)
- Check alarm state at the `_ejected` guard after each fold step
- Evaluate both `_reached` and `_ejected` at each folded step, not just at
  the final settled state

### Second problem — trend monitor vs FRONTIER

Even if the zoom were honest (ejected at Aborting), the trend monitor has a
separate issue: FRONTIER at C_Start set distance to 25 but kept best_trend=16
(our intentional change — FRONTIER doesn't reset best_trend).  Any zoom result
with trend > 16 triggers regression, even if the zoom improved from 25→21.
The design intent was "if the new corridor keeps drifting, revert" — but this
means a FRONTIER → zoom → improvement sequence always regresses.

Possible fix: FRONTIER should record its trend as a corridor baseline.  The
regression check compares against the corridor baseline (25), not the global
best (16).  If the corridor improves (25→21), it's progress; if it worsens
(25→30), it's regression.

## Sub-problem B (still open) — zoom scan budget

`_letrun_zoom` has its own `_ZOOM_BUDGET = 10_000`; committing the fork advances
`scan_id` by the consumed scans. With `max_scans=2500` the pilot finishes after a
single zoom. Decide: the zoom fork's scan_id should not count against the pilot's
iteration budget.

## The door alarm + abort chain (burner main.py)

```python
with rung(Or(S_Starting, S_Unholding, S_Unsuspending), ~i_DoorClosed):   # R7
    latch(A_Alm14_DoorOpen_Trig)                 # latches on entering Starting
with rung(rise(system.sys.clock_1s)):            # R67 — once per second
    calc(ds.select(201, 300).sum(), A_AlmExtent) # door alarm's extent registers here
with rung(A_AlmExtent != 0, Or(S_Resetting, S_Idle, S_Starting, S_Execute, ...)):  # R1
    copy(CmdAbortRef, C_CtrlCmd)                 # -> Aborting
```

`x_DoorClosed` feeds `i_DoorClosed` via read_inputs. The latch (R7) is sticky, so
holding the door *after* entering Starting is too late — it must be held before
`C_Start`.

## Click state encoding (`S_StateCurrent`)

0 undefined, 1 CLEARING, 2 STOPPED, 3 STARTING, 4 IDLE, 5 SUSPENDED,
**6 EXECUTE**, 7 STOPPING, 8 ABORTING, 9 ABORTED, 10 HOLDING, 11 HELD,
12 UNHOLDING, 13 SUSPENDING, 14 UNSUSPENDING, 15 RESETTING, 16 COMPLETING,
17 COMPLETED. Real startup path: `9 → (Clear) 1 → (wait) 2 → (Reset) 15 →
(wait) 4 → (Start) 3 → (wait) 6`. `-ING` states auto-advance on a wait.

## Key files

- `src/pyrung/core/analysis/pilot/pilot.py` — drive loop, entry points.
- `src/pyrung/core/analysis/pilot/steer.py` — zoom, pulse, cone settlement.
- `src/pyrung/core/analysis/pilot/verify.py` — gate pipeline.
- `src/pyrung/core/analysis/pilot/progress.py` — trend monitoring, checkpoints.
- `src/pyrung/core/analysis/pilot/investigate.py` — incident investigation,
  excursion recovery, latch-exposure/upstream hypotheses, replay harness.
- `src/pyrung/core/analysis/pilot/causal.py` — cause-chain walker with enabler
  fallback.
- `src/pyrung/core/analysis/pilot/candidates.py` — prerequisite/command split,
  zoom prescription.
- `src/pyrung/core/analysis/pilot/trace.py` — backward trace with aggregate
  decomposition + clock-gated awareness.
- `src/pyrung/core/analysis/pilot/outcome.py` — five-outcome classifier.
- `src/pyrung/core/analysis/crossings/calc.py` — CalcCrossing with Aggregate
  forward.
- `src/pyrung/core/fold.py` — fold engine (clock-edge + scan-derived bounding).
- Diagnostics: `sample_pilot_events.py`, `diag_zoom_endstate.py`,
  `diag_door_before_start.py`.
