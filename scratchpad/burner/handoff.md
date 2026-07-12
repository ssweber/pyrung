# Burner PILOT handoff — current frontier as of 2026-07-09 (late night)

The live target is unchanged:

```text
>>> how S_StateCurrent==17 avoid C_Complete
```

## What landed tonight (dev 6b092a1; suite 4909)

**Frontier 2 (detour recognition) — the mechanism is in.**  The law that
survived falsification: *regression is resurrected work, not channel
displacement* (see `detour_recognition.md` for the review that killed the two
attractive shortcuts — raw `_pilot_state_key` novelty and committed-channel-
history cleanliness — and `test_pilot_gauge.py` / the flipped
`test_pilot_detour_progress.py` for the gates).

- `pilot/gauge.py` — the target-relative progress gauge.  Two
  provable families only (everything else → unknown, never guessed):
  *ordinal* (threshold-absorbed monotone sources — `Knock_Count`; raw value
  excluded from the search key, contributes an earn-direction overlay) and
  *stepper* (`Internal__Step`: +1 affine advances with discrete OR
  self-limiting provenance — a guard reading a derivation of the tag itself
  fires once per context entry; literal loads recorded as anchor-relative
  resets, enabling channel values resolved one hop through the
  `sm_MapVal2State` alias Bools).
- `pilot/detour.py` — landing classification: a **stopover** needs a clean
  forward route on the compass value graph — no resident reset (a literal
  load *behind* the anchor, e.g. `S_Resetting → Internal__Step := 101` at
  channel 15) and no resurrected obligation (a route edge re-requiring an
  already-committed press in the same channel context — the route from
  ABORTED re-requires Clear/Reset/Start; the route from HELD requires
  `C_Unhold`, never discharged).  Everything else → regression → the old
  investigate-and-revert, unchanged.
- **The detour** (progress.py): a stopover is provisional. Adopt the settled
  landing, retain the pre-departure checkpoint, and skip investigation. At
  corridor rejoin, compare the gauge: advanced → `detour_worked` and a new
  checkpoint (trend baseline resets); anything else → `detour_failed`, revert,
  and remember the failed signature (the re-ejection then
  classifies regression and investigation gets a tight fresh window).
- **Verify gates consult the gauge**: SPIN / CYCLE / LATERAL accept a
  trial that advanced an event-earned ordinal even when the threshold-masked
  key aliases it.  This alone flipped the three-knocks gate (counter-gated
  channel revisits drive to Inside).
- **Budget charges searching, never productive waiting** (`dwell_scans` on
  `_World`, reverts rewind it): accepted coast spans are free when the coast
  reached its channel target / advanced the gauge / landed somewhere
  new; sterile laps still drain.  `_coast_to_value` / `_coast_holding_state`
  log `scan-ids / real scans / folds` at DEBUG.

Live transcript (bench recipe), the two moments that matter:

```text
detour: S_StateCurrent 6->9  (102 settle scans): regression — every forward route
        crosses a gauge reset or resurrects a discharged obligation
        -> investigation runs, rotate/temp correctives earned as before
detour: S_StateCurrent 6->11 (102 settle scans): stopover — clean forward route
        11 -> 12 -> 6 (no reset, no resurrected obligation)
        -> DETOUR STARTED, no investigation round, march preserved
```

## Frontier: driving the HELD handshake (the detour never finishes)

The loop now *stands* at HELD with the march intact but cannot advance:

- The 105→107 advance needs a **door cycle** (`x_DoorClosed` False→True at
  HoldForShine, ProductionExecuteSteps R18) — but `x_DoorClosed=True` is an
  **earned pilot rung**. The HELD path needs a guarded counter-rung that drives
  the input False during the door-cycle window, then yields so the earlier
  rung reasserts.
- After the door cycle, `C_Unhold` is the one legal button (the clean route's
  own edge action — `detour.py` already computed it; currents territory).
- Until then the loop spins sterile 11→16 zooms (each committing ~10k
  scan-ids that now correctly drain the budget) — honest but wasteful.

## Frontier 1b — folding, measured precisely now

- The dry coast folds: `39300 scan-ids in 10000 real scans, 3 folds`.
- **Every HELD-era coast: `10000 scan-ids in 10000 real scans, 0 folds`.**
  Also the Execute-era ejection coast (815→1855 era): `2005/2005, 0 folds`.
  Diagnosis probe: `probe_cyclefold.py` — detection finds a clean P=100 when
  the heat-churn is silenced and refuses (correctly) on the heat-retry
  transient; what churns aperiodically at HELD is unmeasured.  Start there.
- The fold context is sane: pmo=100, clocks aligned, no scan-derived reads,
  max_period=400 (the ring diagnosis section of `probe_cyclefold.py` prints
  per-period kill lists — point it at a HELD-era fork).

## Standing issues / loose ends

- **`repro_regression.py` (the y_BurnerLoop standing gate) is RED on clean
  HEAD** — declines at ~scan 10 naming `A_Alm10_Status`.  Pre-existing (A/B
  confirmed against HEAD before tonight's changes); probably project-template
  state (regen gotcha below) or drift since the gate last ran.  Do not treat
  it as a regression of tonight's work — but re-baseline it before trusting
  it again.
- `stash@{0}` ("detour-wip") holds the superseded first-draft wiring — safe
  to drop.
- Uncommitted in-tree (not mine): `circuitpy/codegen/compile/_core.py`
  blockless-kernel snapshot fix + `test_compiled_replay.py` (the reviewing
  agent's), and two pre-existing ruff format joins in investigate.py /
  steer.py.
- The kept program bug (heat.py R5 pre-empts R14, `Heat_EnableLimit` never
  cleared) stays as the stall-honesty test bed — do not "fix" it until the
  HELD handshake and folding land.

## Receipts

```powershell
uv run python scratchpad/burner/reconstitute_completed_steps.py   # ground truth: SUCCESS
uv run python scratchpad/burner/probe_detour_truth.py             # the falsification ledger (PASS)
uv run python scratchpad/burner/probe_cyclefold.py                # fold-context + per-period kill lists
uv run python scratchpad/burner/repro_completed.py                # the live frontier (DEBUG: detour/_ops/investigate)
uv run pytest tests/core/analysis/test_pilot_gauge.py tests/core/analysis/test_pilot_detour_progress.py -q
```

Regen gotcha (cost us an afternoon): re-exporting the Click project can
silently drop init logic — boot dead at mode 0 is unrecoverable
(`mode_change` R5 needs Idle/Stopped/Aborted).  Canaries: state 9 after one
scan; `S_P6_HeatMaxRetry == 1` proves init ran (`probe_boot_state.py`).
Backup of the 12:15:50 regen: `scratchpad/pyrung_project_preedit`.

## Done means

- `how(S_StateCurrent==17, avoid=C_Complete)` reaches 17 on the bench recipe:
  correctives (doors, rotate oscillation, temp boundary) + HELD handshake
  (door cycle under hold-release, `C_Unhold`) + program-issued Complete,
  never pressing `C_Complete`.
- The HELD detour works (gauge advanced at the Execute rejoin) — watch for
  `detour_worked` in the event stream.
- Dwells fold at period-jump rates in every era; a failed run's terminal
  still names its frontier.
