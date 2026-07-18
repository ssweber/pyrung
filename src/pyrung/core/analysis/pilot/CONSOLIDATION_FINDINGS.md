# PILOT consolidation findings

This file records behavior questions uncovered during the behavior-preserving
consolidation pass. Refactor changes must not silently resolve them. Each item
needs its own behavioral decision, focused success/failure tests, and a
separately reviewable change if accepted.

## Open

### Search budget anchor on an already-running PLC

The drive loop charges `state.work.state.scan_id - state.dwell_scans` against
`max_scans`. A live drive that begins after earlier scans may therefore charge
pre-drive work to the new search. Decide whether the intended measure is total
runner age or work since the drive's anchor scan.

### Initial settling is limited to scan zero

The loop settles calculated intermediates only when `scan_id == 0`. A live
drive entered after a state injection or other unsettled external change does
not receive the same settling scan. Decide whether settling describes initial
program startup specifically or every pilot-drive entry boundary.

### Budget diagnosis uses the last orientation frame

The budget-exhaustion comment promises a fresh frame, but the implementation
uses `last_frame`, which can predate scans consumed by the final attempt.
Decide whether terminal diagnosis must re-orient against the final world.

### Terminal event time moves backward after revert

The loop emits `stuck` at the failed world's scan, reverts to a checkpoint, and
then emits `finished` at the checkpoint's earlier scan. Event consumers receive
no explicit revert marker between those timestamps. Decide whether `finished`
uses event time, world time, or carries both.

### Final step span relies on object identity

The target-reached path updates the journey copy of the final step only when
`state.journey[-1] is state.steps[-1]`. A future copy at either recording site
would silently stop the journey span update. Decide whether span completion
should update both collections explicitly or be resolved when rendering.

### Terminal candidate counts are asymmetric

The ordinary `Stuck` event reports the current candidate count, while the
budget-exhausted `stuck` event hardcodes zero. Decide whether these events
describe attempted candidates, currently admissible candidates, or merely the
terminal kind.

## Deliberately deferred

### One observation-application point

This consolidation pass does not partially adopt a package-wide
"instruments return observations; the loop applies once per turn" contract.
`steer.py` and `skiff.py` already return observations, and the loop applies
those results. However, the loop also creates terminal-coast, probe-exhaustion,
and rejected-action observations, while `progress.py` applies regression
nogoods after investigation.

Moving only some of those sites would make ownership less consistent. A later
dedicated contract pass must change `steer.py`, `skiff.py`, `progress.py`,
`pilot.py`, and the `Compass.apply` boundary together, with tests proving that
rejected attempts, terminal coasts, skiff exhaustion, regression recovery, and
world reverts retain exactly the same knowledge.

### Phase-specific act event switches

The drive loop retains separate act-type switches for announcement, rejection,
and acceptance events. A shared formatter was prototyped during consolidation,
but it increased total code and hid the phases' intentionally different event
schemas. Consolidating these switches should wait until navigation acts own
declarative event metadata. Introducing that contract in this
behavior-preserving pass would be a redesign rather than residue removal.
