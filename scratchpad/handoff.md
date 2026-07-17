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
- Drums: desugar them into equivalent primitive rungs at build time so trace,
  crossings, profiles, and corrections need no drum-specific PILOT arms.
  Afterwards, reassess whether corrections still need `harness=` and held
  `fork=` plumbing for non-drum Tier-2 empirical profiles.
