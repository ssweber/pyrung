# PILOT multi-phase sequencing — plan (deferred from the table-oracle session)

## Motivating case

`how(StateCurrent == STARTING)` with the machine driven into **Manual** mode.
STARTING is blocked in Manual (`StateMask[STARTING]=0x0004 & DisabledStates[Manual]=0x0224 ≠ 0`),
so the transition needs a mode change *first*. The full plan is strictly ordered:

1. change mode to Production/Maintenance — drive `ModeChgRequest` **edge** + a mode
   selector (`ModeMaintenance`), let `mode_change` run and settle;
2. `CmdReset` → RESETTING → IDLE;
3. `CmdStart` → land on the one-scan STARTING transient.

Regression: `tests/core/analysis/test_packml_diagnosis.py::TestHowIntoModeDisabledState::test_how_disabled_state_reaches`
(strict xfail). `test_how_disabled_state_is_loud` already passes (loud reason, no silent unreachable).

## What already works (landed this session)

- Trace surfaces the mode-change prerequisite: `_route_pilotable` filters dead
  routes so `ModeChgRequest` surfaces instead of the spent `~InitDone` caller.
- One-scan transient is catchable: `_settle_cone(reached_fn=...)` stops the settle
  the scan the target holds. Production `how(STARTING)` reaches.
- Unreachable always carries a reason (`_pilot_loop` returns it; `pilot_how` uses
  it as the fallback after `_linked_feedback_block`).

## The two remaining gaps

### Gap A — mode selector completeness (trace)

The enable arm surfaces the trigger (`ModeChgRequest`) but not reliably *with* a
selector (`ModeMaintenance`/`ModeProduction`). Firing `ModeChgRequest` alone with
`UnitModeCmd=0` lands mode 0 (Undefined). Investigate why the `copy(2, UnitModeCmd)`
guard (`ModeMaintenance`) and the caller trigger don't both survive into the chosen
arm — likely an OR-arm vs AND-requirement collapse in `_trace_expression` when the
caller-route and copy-source children are merged.

Note: mode 0 is genuinely pilotable *and* mask-valid here (`dh[200]=0` disables
nothing), so pilotability cannot exclude it — only `avoid=(UnitModeCurrent==0)`.
Whether "Undefined" should be treated as non-commandable (via the choices label) is
a separate design call.

### Gap B — multi-phase sequencing (drive loop) — the big one

The loop applies `ModeChgRequest + CmdStart + CmdChgRequest` in **one** pulse
instead of sequencing the phases. It needs to treat the mode change as a
prerequisite that must *complete and settle* before the command path is pursued.

Where to look:
- `candidates.py` — the bearing → candidate split. Today all trace actions become
  co-applied candidates. Sequencing needs prerequisite ordering: a `same_tag_chain`
  / `ordered_actions` style dependency where `UnitModeCurrent` (mode) is a
  prerequisite of `StateCurrent` (state) and must be established first.
- `_ops.py` prerequisite holds (`forced_holds`) + `_coast_holding_state` — the
  established-prerequisite-then-advance machinery already exists for the
  Starting→Execute let-run; the mode change is the same shape (establish mode,
  settle, then pursue the next frontier) but for a *commanded* prerequisite, not a
  self-advancing one.
- The compass route plan (`CompassPlan`/`CompassEdge`) already enumerates the
  state-machine edges; sequencing may fall out of walking that plan edge-by-edge
  with a settle between edges, rather than collapsing to one pulse.

## Design questions to resolve first

1. Is sequencing a candidate-ordering change (drive prereqs before commands within
   one iteration) or a loop-structure change (dedicate iterations to each phase)?
2. How does the loop know phase N is *done* before starting N+1 — target of the
   sub-goal (mode == 2) reached and settled? Reuse `target_reached` on the
   sub-goal.
3. Does the existing prerequisite-hold path already sequence self-advancing
   frontiers, and can commanded prerequisites reuse it?
