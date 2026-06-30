# Limit-cycle fold — pilot-authorized macro-skip through active-hold soaks

## The problem (measured on the burner)

Coasting Execute → `y_BurnerLoop` takes **1201 scans**, of which ~1199 are pure
waiting. The pilot installed a period-2 oscillation on `x_RotateSensor` (the
liveness hold) to keep two rotate watchdogs reset. During the coast only **4
tags churn every scan**:

- `x_RotateSensor` (the oscillation itself)
- `Rotate_SensorOnWD_tmr_EN`, `Rotate_SensorOnWD_tmr_TT`,
  `Rotate_SensorOffWD_tmr_TT` (watchdog enable/timing bits reflecting the toggle)

Everything else is a clean plateau. Real progress is **timer-gated**, not
oscillation-counted:

```
Heat 0 → 1 : 1000 scans (10 s HeatDelay dwell)
Heat 1 → 2 :  199 scans (~2 s)
Heat 2 → 3 :    1 scan
```

The existing runner fold (`core/fold.py`) cannot skip these dwells: those 4
flickering tags break its plateau guard (`after_vis != before_vis`), so it
degrades to scan-by-scan. The gap here is trivial; on a real minutes-to-hours
soak it is 10^5–10^7 scans of pure waiting — the difference between `how()`
returning and never returning.

## Why widening the exclude set is NOT enough

Even if the 4 churn tags were excluded, the runner fold would then time-fold the
HeatDelay dwell via the **dt-knob** (`_do_fold` → `_dt_override = skip*dt`,
`fold.py:1359`). Timers accumulate by per-scan `_dt` (`timers.py:103`), so one
`dt × 1000` scan advances the *watchdog* by a full 10 s and trips it — the
oscillation's whole job is to reset it every other scan. **The dt-knob is the
trap.** The fix is not exclusion; it is a different advance mechanism.

## The two soak regimes (the engineer's two moves)

- **Pure soak** — nothing must keep happening every scan. Engineer cranks **dt**.
  → already handled by the existing fold. Done.
- **Active-hold soak** — a long timer ramps while a sub-cycle (oscillation,
  watchdog pet, keep-alive handshake) MUST run every scan. Engineer patches the
  soak timer's **acc** and takes one normal scan. → this design.

## Mechanism (sound because timers read only `_dt`, never timestamp)

Patch the monotone coordinate(s) forward; run **one real period at normal dt** to
land the crossing; **never touch dt**. Cyclic tags are left at their current
phase — correct because they are net-zero over the skipped span (the watchdog
stays reset whether we run those scans or not).

## Algorithm (general limit-cycle fold)

A pilot coast primitive (`pilot/cyclefold.py`), used by `_coast_holding_state` /
`_coast_to_value` when a plain fold stalls but the macro-state is stable.

```
ring = []                         # consecutive full snapshots
loop until reached / ejected / budget:
    step once (normal dt); ring.append(snap)
    if reached(): return True
    cyc = detect_cycle(ring)      # smallest P with consistent per-period deltas
    if cyc is None: continue      # not yet periodic — keep stepping
    # classify (from detect_cycle):
    #   boundary-stable tags : snap[i] == snap[i+P]   (constant or net-zero cyclic)
    #   monotone tags M      : snap[i+P]-snap[i] == d_t (const, numeric, !=0)
    if not M: return False        # cycle makes no progress → spinning, bail
    # bound: nearest crossing in PERIOD units over all monotone coords
    #   k = min over M of periods-until( comparison flips | target | role ejects )
    #   reuse core/fold _progress_bound / _scans_to_cross at period granularity
    if k <= 1: continue           # crossing is within one period — let it run
    # patch monotone coords forward by (k-1) periods, keep cyclic tags in phase
    patch({t: cur[t] + (k-1)*d_t for t,d_t in M})    # do NOT set _dt_override
    run one period (P scans) at normal dt            # lands the crossing
    # re-confirm: the period after landing must still classify, else regime
    # changed (a comparison we didn't bound flipped) → clear ring, resume stepping
```

### Soundness guards (non-negotiable, mirror the runner fold)

1. **Observe before skip** — require `min_repeats` (≥2) matching periods before
   trusting P and the deltas.
2. **Bound at nearest crossing** — never patch past the scan where any comparison
   read on a monotone coord flips (regime boundary). Target and role-ejection are
   also crossings. Reuse `core/fold.comparisons` + the predicate's
   `extra_comparisons`.
3. **Re-confirm after landing** — run one period normally; if it no longer
   classifies, fall back to scan-by-scan and re-detect.
4. **No dt compression** — patch accs; run real scans at normal dt. The sub-cycle
   runs untouched.
5. **System clocks** — if a monotone-cone rung reads a system clock, bound at the
   clock edge too (reuse `_scans_to_clock_edge`). (Burner Execute reads none.)

### What maps onto existing machinery

- crossing arithmetic: `core/fold._progress_bound`, `_scans_to_cross`,
  `_resolve_num` — lifted from per-scan to per-period delta.
- comparison thresholds per tag: `fold_ctx.comparisons` (already built).
- patch + advance scan_id: `runner.patch`, and `state.set(scan_id=...)` like
  `_do_fold` (advance scan_id by (k-1)*P to keep history monotone).

## Expected result

Burner Execute coast: 1201 scans → ~3 macro-steps (one per Heat dwell) + a few
real probe/period scans ≈ <15 scans. Scales to arbitrary soak length.

## Validation

- New unit tests for `detect_cycle` (synthetic snapshot streams: clean period-2,
  multi-monotone, chaotic→None, too-short→None).
- New soundness test: cycle-fold landing == scan-by-scan landing (bit-equal) on a
  synthetic active-hold soak (oscillator + long timer).
- Regression anchors from `scratchpad/burner/handoff.md`:
  `pilot_rotate_liveness.py` (pre-positioned) and `sample_pilot_events.py` (cold)
  must still reach `y_BurnerLoop=True` with the same round/hypothesis verdicts —
  now in far fewer scans.

## Soundness subtlety found while building the classifier

A tag like `i % 3` reads `[2, 1, 0]` over three consecutive scans —
**locally indistinguishable from a linear ramp**. A purely empirical detector
extrapolates it past its wrap and patches a bogus value. Fix: `detect_cycle`
takes `monotone_allowed` — only tags statically *certified* monotone within a
regime (the fold context's `acc_names`: timer/counter accumulators) may be
classified monotone. Any other ramping tag fails the boundary-stable test and
forces P up to its true period. The coast MUST pass `acc_names`; `None`
(trust-any-numeric) is for tests only and is not modular-safe.

## Open risk for the coast loop (next)

**Landing phase/parity.** Scan-by-scan lands the crossing at a specific cycle
phase (e.g. watchdog acc = 0 vs 1 depending on scan parity). The patch+land must
reproduce that exact phase for bit-equality. Patch the monotone accs forward by a
whole number of periods so the cyclic tags stay phase-aligned, then run exactly
one period. The synthetic soundness test (oscillator + watchdog + long soak
timer, cycle-fold landing == scan-by-scan landing, bit-equal) is where this gets
nailed.

## Clocks: the runtime form of the soft-clock partition

The burner reads two system clocks (100 ms, 1 s) and the static partition marked
both **hard** (`soft_clocks=[]`, `sat_clocks=[]`), so the runner fold is bound to
their edges.  Cycle-fold handles them by **aligning the cycle period to every read
clock's full period in scans** (`period_multiple_of = LCM`; burner = 100) and
folding **whole periods only**.  That one condition resolves all three clock
concerns at once:

1. *Phase at landing* — free: clock is `f(timestamp)` and the jump advances
   `timestamp` by the exact `jump_scans·dt`.
2. *`rise()`/`fall()` `_prev`* — a whole-period (∴ whole-clock-period) jump leaves
   the clock at the same phase, so `_prev` stays consistent across the gap.
3. *A long-period edge inside the skip* — the observed window now spans each
   clock's full cycle, so boundary-stability over P genuinely captures the clock's
   net effect (zero, or a certified accumulator).

This is exactly the soft-clock idea moved from static to runtime: instead of
*proving* an edge inert, observe a full clock cycle and confirm the net change is
boundary-stable. Strictly more capable — it folds across clocks the partition gave
up on (hard).

## Don't-care tags: reuse the fold exclude set (answers "is this on frozen_writes?")

The classifier runs over the runner fold's **significant** set — full state minus
`frozen_writes ∪ churn_excluded ∪ profile_fb_names`, keeping `acc_names` as the
monotone candidates.  So it *does* build on `frozen_writes`, as the ignore set,
not as the authorization.  This is what lets it tolerate the burner's
resolved-on-read RTC tags (`A_PLCDT_*`, which `frozen_writes` already flags) that
would otherwise break every period.

## Results (validated)

- Synthetic clockless active-hold soak: **bit-equal** to scan-by-scan (all tags,
  scan_id, timestamp), watchdog never trips, < 10% of real scans.
- Synthetic clocked soak (`clock_1s` + oscillation): **bit-equal incl. `Blink`**;
  a 10x-longer soak stays observation-bound (real scans ≈ constant).
- **Real burner** (`probe_cyclefold_burner.py`): Execute → `y_BurnerLoop`
  **bit-equal** (significant tags + scan_id 2016), 1201 → 601 real scans (2.0x).
  The 2.0x is observation-bound (clock forces P=100; only the 1000-scan Heat-0
  dwell is long enough to fold) — the value is in minutes/hours soaks where the
  fixed ~observation cost vanishes.

## Status

- [x] Investigation + design (this doc)
- [x] `detect_cycle` classifier + unit tests — incl. modular-tag soundness governor
- [x] crossing-bound in period units (`_periods_to_crossing`, reuses fold helpers)
- [x] coast loop `cycle_fold_until` + patch/land/re-confirm + clock alignment +
      harness bound + don't-care ignore set
- [x] soundness tests: bit-equal vs scan-by-scan (clockless + clocked), watchdog
      safety, scaling — `test_pilot_cyclefold.py`, 13 green, ruff + ty clean
- [x] validated on the real burner via read-only probe (no source edits)
- [ ] **wire into `_coast_holding_state` / `_coast_to_value`** — DEFERRED: lives in
      `_ops.py`, which another agent is editing (holds / `when().do()`).  Standalone
      module is ready; integration is a one-call swap when that work settles.
- [ ] follow-ups: patch mirrors/modwrap at fold for full bit-equality when present;
      scope the clock LCM to only clocks active in the soak regime (shrinks the
      observation cost on the burner's short dwells)
