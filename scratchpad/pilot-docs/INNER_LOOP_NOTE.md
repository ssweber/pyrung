# Candidate Construction Inner Loop

## Status

Closed.

The candidate-construction divide is removed:

- completion and exact-producer reads may discover action details, but those
  details enter the same `_admit_trace_details` pass as the broad target trace;
- `_WaitPrescription` carries only a bearing, reason, boundary, frontier, and
  program-step diagnostics -- it cannot materialize an action;
- duplicate readings may enrich an already-read action with its owned `until`
  lifetime before admission;
- blocked actions, ordinary world-keyed nogoods, current satisfaction,
  managed-rung ownership, and establish staging apply to every trace source;
- `structural_nogoods` and
  `Gauge.writer_path_erases_banked_work` are deleted. A destructive
  intervention is tried on a fork, rejected by `verify_gates` when the actual
  gauge landing is `behind`, and recorded through the normal exact-act nogood
  loop.

This preserves both sides of the Step 101/102 contract:

- Step 101 can still supply `S_DryerTemp_F=131` with the
  `S_HeatAtTemp_tmr_Acc >= Sts_P2_Dry_Tm` lifetime;
- Step 102 coasts across the program-owned `Internal__Step 102 -> 103`
  boundary instead of admitting a requirement from the future world.

The route-removal residue owned by this note is also cleaned:

- `pilot/CLAUDE.md` no longer documents inferred route commitments,
  `RouteExhausted`, or `RouteUnproductive`;
- the vacuous `RouteUnproductive` dark-run assertion is deleted.

## Validation

Completed:

- focused admission, program-step, orientation, guarded-rung, gauge, and
  verification tests;
- the program-owned completion detour tests;
- `make test-pilot` -- 572 passed, 25 skipped;
- `make lint`;
- avoided-Complete Tumbler drive reaches completion with the same 178-event
  skeleton length and all semantic route assertions passing.

In progress when this note was trimmed:

- regenerate and review the avoided-Complete golden. The first observed change
  was diagnostic only: completion-read feedback actions already owned by
  installed rungs disappeared from `candidates`; the executable sequence and
  event count were unchanged;
- rerun that golden without regeneration, then run the broader Tumbler gate.

## Remaining work outside this cleanup

- `tests/tumbler/skeleton.py` still defensively canonicalizes object addresses.
  Current goldens contain no `ADDR` tokens because `Condition.__repr__` now
  renders source. Removing the scrubber is optional hardening cleanup, not part
  of candidate admission.
- `TraceChoice`, `_RouteDraft`, `_RouteConflict`, and `_RouteConflictPin` remain
  live R1 Step 4 targets. Delete them only after the immediate-act-universe
  dark-compare gate in `TRACE_REFACTOR.md`.
- `root_route` / `recorded_root_route` remain reporting receipts, not navigation
  state.
