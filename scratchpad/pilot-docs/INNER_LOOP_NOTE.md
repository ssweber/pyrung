# Candidate Construction Inner Loop

## Observation

At `Internal__Step == 102`, the user program already owns an automatic boundary:

1. `S_HeatAtTemp_tmr.Done` sets `Internal__TransBool`.
2. The transition writer advances Step `101 -> 102`.
3. The even-step writer advances Step `102 -> 103` on the next scan.
4. Step 103 begins Cool. The program-owned Hold does not begin until Step 105.

PILOT should therefore coast one scan at Step 102 and re-read the Step 103 world.
Instead, candidate construction's completion re-read can introduce
`Cmd_Reset2FactoryDefault`, then lower it into a `PilotRung`, without returning
control to the main PILOT loop.

## Architectural read

This is a semantic inner planner inside `_build_candidates`:

```text
main loop reads Step 102
  -> candidate construction re-reads future completion
  -> discovers and materializes another action
```

That later action ingress can bypass admission applied to the original trace.
`structural_nogoods` is a symptom: the inner decision needed a transport for
rejecting work back in the outer loop.

The intended ownership is:

```text
main loop reads Step 102
  -> program_step owns automatic 102 -> 103
  -> coast one scan
  -> main loop re-reads Step 103
```

The next refactor should find where completion re-read crosses from describing
the current automatic boundary into discovering/materializing future actions.
`program_step` should terminate the current read with a coast bearing; candidate
construction should not compete with that owned boundary.
