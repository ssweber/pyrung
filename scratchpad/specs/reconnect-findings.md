# Field-order reconnect: why it needs the last resort (2026-11-05)

Branch `bob/how-gaps`, WIP commit on top of c7a62f7. Not landed.

## The gap

`_field_order_reconnect_logic` (S = SequenceStep): NetworkAvailable=True writes 10 (repair
branch), then the sibling `S<=20, ~RecoveryCounter.Done, ServiceStatus==0` overwrites it to 94
on the same scan. ServiceStatus is copied from the steerable ServiceStatusInput by the last rung.
The normal plan is ServiceStatusInput=-1 first, then NetworkAvailable=True.

## What the WIP does (each piece checked with the event dumps)

1. **Trigger values** (intrascan.py, requirement_derivation.py, requirement_evidence.py): an
   overwriter-guard requirement on the trigger scan drops atoms the attempt's own assignments
   decide (`NetworkAvailable != True`). Requirement becomes `ANY(Done, ServiceStatus!=0)`:
   SETUP_FIRST instead of RETRY_TOGETHER. Without it, RETRY_TOGETHER "composes" by steering
   NetworkAvailable=False -- undoing the trigger.
2. Setup traceback asks about the theory's trigger act, carries `prevented_write`.
3. Prevention fallback includes the parent branches' guard reads.
4. `_copy_producer_facts`: a plain copy from a named tag (store keeps the value) is a producer
   goal `source == value` added to the copy rung's guard.
5. Setup traceback carries sibling atoms (`NeedIntrascanTraceback.alternatives`, max 4); the
   pilot researches them in order and retires only when all prove nothing
   (`_research_traceback_request` in pilot.py).
6. (Experiment, harmless) RETRY_TOGETHER asks the setup traceback before probing.

With 1-5 the reconnect reaches step 10 with no last resort: ServiceStatusInput=-1, then the
NetworkAvailable pulse.

## Why it is parked: the 81 restart regresses

`test_a_route_a_scan_has_already_spoiled_says_what_a_restart_needs[field]`: piece 1 flips the
field route's NetworkAvailable requirement to SETUP_FIRST too. Dev's RETRY_TOGETHER composition
had (by luck) found the real route (ReadyCommand + SimulationMode + CheckpointSensor via R16,
no NetworkAvailable needed), landed 81, saw R28 kick it, and named the latch
(`SequenceInterrupted=1 ... controlled by the program`), from which the restart clear is built.
With the flip, the pilot reaches S=10 instead; there the R29 interrupt branch (S<50,
~BaseSensor -> 80) makes 80 look two steps away, its `BaseSensor=False` is forbidden by an
active requirement, the read offers no candidates, and it stops with "No productive next action"
-- no latch diagnosis, `restart.clear=None`.

## What would unpark it

The read at S=10 must fall back to the longer route (R16) when the shortest route's step is
forbidden by an active requirement, instead of offering nothing. That is a route-planning change;
measure it on the whole pilot suite and tumbler before trusting it. Then piece 1 can land, and
2-5 with it.
