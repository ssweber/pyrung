# PILOT deferred handoff (2026-07-17)

These are the remaining follow-ups that survive deletion of
`scratchpad/wait_edge/DESIGN.md`.

- Add the frontier ETA garnish: terminal/frontier clauses on accumulator
  leaves should append `(~N scans out)` from the analytic tier of
  `scans_to_eject`, omitting the suffix when it returns `None`. This is still
  unwired: `_frontier_clause(frame)` has no context/PLC handle. Either thread
  that dependency through its terminal call sites or precompute the garnish
  while building candidates and carry it on `_IterationFrame`, alongside
  `completion_frontier`.
- Re-evaluate only the `current_readings` / `current_prescribed` fallback after
  channel-punt expansion can express program-awaited actions through ordinary
  completion/frontier evidence. Do **not** target all of `currents.py`:
  producer-family extraction remains static chart evidence, and the current
  reader still supplies live bearing and channel-continuation evidence.
- Keep the `avoid=Cmd_State_Complete` internal-route acceptance gate.
  Exposure-guard work has landed and no longer owns it; re-triage the full
  Tumbler gate after the channel-punt/program-owned-frontier gap is closed. The
  reduced tide-gated gate in
  `tests/core/analysis/test_pilot_currents_capability.py` remains the focused
  tripwire.

  Full-gate re-triage (2026-07-17): the drive does reach Execute, but only for
  one scan. It goes `Starting(3) -> Execute(6)` at scan 909, then
  `Execute(6) -> Holding(10)` at scan 910 and lands `Held(11)` at scan 912.
  The Starting-scoped door holds expire on entry to Execute; both door inputs
  return false, so `ProductionStates` R5 issues Hold. PILOT classifies that
  `6 -> 10` departure as `ambient` because Holding/Held is structurally on a
  route to Completed. That classification is not permission to ignore the
  departure: **it is ambient, but PILOT still has to solve the departure**.
  Here that means investigating the actual door-triggered move and re-earning
  the Execute-era door holds, just as the BurnerLoop drive does. Otherwise it
  banks the premature Held state, tries the coarse program-owned `11 -> 16`
  current, stalls without the recipe prerequisites, Unholds, and repeats
  (`Execute` again at 1708, `Holding` at 1709, `Held` at 1811). The later
  `AlmHistorian_ManualRecor` free-word decline is only a terminal symptom.

  There are therefore two stacked issues in the full gate:

  1. Ambient target-relative motion still needs departure-cause resolution;
     accepting `6 -> 10` must not suppress the door correction.
  2. After that, the existing channel-punt/program-owned-frontier gap must
     expose the recipe prerequisites behind the internal Complete producer.

- Drums: desugar them into equivalent primitive rungs at build time so trace,
  crossings, profiles, and corrections need no drum-specific PILOT arms.
  Afterwards, reassess whether corrections still need `harness=` and held
  `fork=` plumbing for non-drum Tier-2 empirical profiles.
