# Candidate Construction Inner Loop

## Status

The Step 102 symptom is fixed (c9d6f5ce); the guard-rendering defects it exposed
are fixed in e1f64fa7. The architectural divide is not. This note is now the
cleanup plan for that divide, plus the dead code the route removal in
`TRACE_REFACTOR.md` R1 Step 3 left behind.

Not verified: `make test-tumbler` was never run against the regenerated goldens.
The regen wrote what the code produced and `make test-pilot` / `make lint` are
green, but the goldens' own replay is an open step.

## Working on this

**Do not drive `how()` from cold to reach a deep state.** Reaching Step 102 that
way burns the `S_HeatAtTemp` dwell (360001 scans, 4-9 minutes per iteration).
`tests/tumbler/bench.py::Bench.force_done(acc_tag, preset)` fast-forwards a
minute-scale dwell by writing the accumulator straight to preset, so replaying
stages 0-4 of `tests/tumbler/test_constructive_route.py` parks a PLC at
`Internal__Step == 102` in about 15 seconds. Hand that `b.plc` to
`pilot_events(...)` and instrument from there.

**`test_constructive_route.py` is also the ground truth** for what PILOT
*should* do, hand-driven cold to COMPLETED(17) without ever pressing the Complete
command. Read it before judging a decision in this territory. The route after
Step 102, off `ProductionExecuteSteps`:

| step | rung | waits on | who supplies it |
| --- | --- | --- | --- |
| 102 | R25 (even-step) | nothing | program, next scan -> 103 |
| 103 | R13/R14 | `S_CoolCycle_tmr` (`Sts_P4_Cooldown_Tm` min) | program (time) |
| 104 | R26/R25 | nothing | program -> 105 |
| 105 | R17 | issues `Cmd_CtrlCmd = Ref_Cmd_Hold` | program (the owned Hold) |
| 105->106 | R18 | `~i_DoorClosed` | operator -- open the door |
| 107 | R20 | `Sts_State_Execute` | operator -- Unhold back to Execute |
| 108->109 | R26/R25 | nothing | program |
| 109 | R21/R22 | `S_Fluffing_tmr` (`Sts_P3_Fluff_Tm` min) | program (time) |
| -- | R23 | `rise(S_Fluffing_tmr.Done)` -> `Cmd_CtrlCmd = Ref_Cmd_Complete` | program |

Tools and gates:

- `devtools/watch_pilot_decisions.py` -- streams each decision with the DAP prose;
  `--stop-action TAG=VALUE` halts the moment an action enters candidate
  construction, which is how the original leak was caught.
- `devtools/pilot_divergence.py` -- first changed golden decision without waiting
  for the whole golden test.
- `make test-pilot`, `make lint`, then
  `PYRUNG_REGEN_GOLDEN=1 uv run pytest tests/tumbler/test_pilot_golden_skeleton.py`
  (regen fails loudly by design; review the diff, commit, rerun without the flag).
  Check the golden diff *after* the regen finishes -- reading the directory
  mid-run compares half-written files.

Baseline to regress against: the avoided-Complete skeleton is 178 events with 8
let-run ejections and 2 `Cmd_State_Unhold` attempts (was 284 / 14 / 6 before
c9d6f5ce).

## What the symptom was

At `Internal__Step == 102` the program owns an automatic boundary: the even-step
writer advances `102 -> 103` on the next scan, and Step 103 begins Cool. PILOT
should coast one scan and re-read the Step 103 world.

Instead `read_program_step` traced the selected producer at a one-scan
projection and reported that frame's requirement as an unmet external input.
Mid-crossing, that requirement belongs to the *next* world, so candidate
construction lowered a future action into live work: `Cmd_Reset2FactoryDefault`,
which is a real lever (`ProductionExecuteSteps` R1,
`rung(Sts_State_Starting, C_P_FluffOnlyFlag): copy(109, Internal__Step)` jumps
straight to Fluff) but belongs to a world PILOT is not in.

The disproof was already on the `ProgramStep` and unused. An input the program is
genuinely stopped at is still required once its own motion finishes:

| live step | `trace` requires | `next_trace` requires | verdict |
| --- | --- | --- | --- |
| 101 | `S_DryerTemp_F=131` | same -- persists | genuine input |
| 102 | `Cmd_Reset2FactoryDefault` | `[]` -- dissolved | mid-crossing artifact |

An owned advance is progress, not interference, so the reading is `KEEP_RUNNING`
with the crossing as its immediate boundary. `INTERRUPTED` is wrong here: it sets
`preserve_channels`, and `orientation.py::_orient_read` then targets the channel's
*pre-motion* value, which already matches, so the coast moves nothing and the
dead-end gate rejects it (`empty frontier, no pending effects`).

## The divide that remains

All of this still happens inside one `Compass.orient` call -- step 2 of the
documented control flow -- before `steer.execute` runs:

```text
pilot.py drive loop            <- main loop reads the world once
  Compass.orient
    orientation._orient_read
      options._build_candidates
        options._prescribe_wait
          program_step.read_program_step
            fork.step() x4     <- simulates a future world
          -> _WaitPrescription(details=[...])
        -> spliced into trace_actions / active_trace_actions
```

Sites, current line numbers:

| # | site | what it does |
| --- | --- | --- |
| 1 | `options.py:751-786` | runs `read_program_step`; a 4-scan fork projection inside orientation |
| 2 | `options.py:815-849` | `NEEDS_INPUT` returns `details = step.required_inputs` with `until=` composed in |
| 3 | `options.py:643-699` | `_completion_reread` -- fresh `trace_back` per completion pair, every steerable leaf a candidate |
| 4 | `options.py:1075-1104` | splices those details into `trace_actions` / `active_trace_actions`, downstream of the outer admission pass at 883-933 |
| 5 | `options.py:92`, `pilot.py:962` | `structural_nogoods` + its `continue` -- the transport the inner decision needed to reject outward |

Two different admission rules guard the same pool, and the inner one runs after
the outer: the outer derives `trace_actions` by removing `key_nogoods`
(`options.py:933`), then the inner appends into both `trace_actions` and
`active_trace_actions` at 1102-1103.

## Cleanup steps

Ordered, because step 1 is the only one that is real work.

1. **Teach the outer trace to carry the owned `until` boundary.** The comment at
   `options.py:1083-1089` says outright that the broader target trace "could not
   see" that lifetime. It is what makes the coast *hold* an input rather than
   pulse it -- the step-101 `S_DryerTemp_F=131 until S_HeatAtTemp_tmr_Acc >=
   Sts_P2_Dry_Tm` case depends on it. Either the outer trace learns the boundary,
   or `_prescribe_wait` returns it as a separate receipt the outer admission
   consumes. Not verified feasible; this is where to look first.
2. **Strip `details` from `_WaitPrescription`** so it carries only a bearing:
   prescribed, reason, boundary, frontier. Site 4 then has nothing to splice.
3. **Delete `structural_nogoods`** -- 8 references: `options.py` (field at 92,
   `_detail_erases_banked_work`, the 926 filter, the 1452 constructor arg),
   `pilot.py:962-970`, `recording.py:294`, `devtools/watch_pilot_decisions.py`.
   Check first whether it still fires from the *outer* trace; if it does, that is
   a separate legitimate mechanism and only the inner call site goes.

## Dead code after the route removal

R1 Step 3 landed: `inferred_route_commitment`, `skipped_route_ids`,
`skipped_root_routes`, `active_root_route`, `RouteExhausted`, and
`RouteUnproductive` have zero `src/` references.

Verified residue:

- **`pilot/CLAUDE.md` documents machinery that no longer exists** -- the
  inferred root-route lifecycle ownership row (105-111), `RouteExhausted` /
  `RouteUnproductive` in the Compass result set (144-145), and control-flow
  item 5 (150-153), plus the "exception is the user's explicit trace-route lock"
  paragraph under *Recompute from the current world*. R1 Step 3's own exit
  criterion asked for this and it was missed.
- **`tests/tumbler/test_pilot_wip_dark_run_tool.py:215`** asserts
  `row["baseline_result"] != "RouteUnproductive"`. That result type is gone, so
  the assertion is vacuously true and can never fail again.
- **`tests/tumbler/skeleton.py` address machinery** -- `_OBJECT_ADDRESS_RE`,
  `_canonicalize_object_addresses` (used at 479), `_address_neutral_sort_key`
  (used at 389). These existed only because `Condition` had no `__repr__`, so
  guards serialized as `<CompareEq object at 0x...>`. Confirmed unused: all four
  regenerated goldens contain zero `ADDR` tokens. Weigh removal against keeping
  it as defensive scrubbing -- a future emitter that puts any other object repr
  in a payload would reintroduce address nondeterminism, and this is the layer
  that would absorb it.

Still live, *not* dead -- these are R1 Step 4 targets and need its dark-compare
equivalence gate before deletion: `TraceChoice` (33 src refs, carries `via=`),
`_RouteDraft` (~25 sites in `trace.py`), `_RouteConflict` / `_RouteConflictPin`.
`root_route` / `recorded_root_route` (11 refs) survive deliberately as
*reporting* via `_report_selected_route`, which `TRACE_REFACTOR.md` sanctions
("The public `Plan.route` / pivot description is reporting, not navigation").

## Guard rendering

Two defects found while reading installed holds, both fixed alongside:

- `Condition` had no `__repr__`, so every guard was an address in logs,
  exceptions, plan output, and goldens. `Condition.__repr__` now delegates to
  `render_condition` (lazy import -- `render.py` imports *from* `condition.py`).
  Presentation only: `==` and `hash` stay identity-based.
- The corrective-hold scope rendered a duplicated disjunct,
  `Or(Sts_StateCurrent == 4, Sts_StateCurrent == 3, Sts_StateCurrent == 3)`.
  `investigate.py` builds the incident corridor by *role* -- source, exposure
  guards, safe landing -- and two roles routinely name the same channel state.
  The union is now collapsed on `_semantic_key` via `_ops.py::_union_conditions`.

The goldens could not have caught either one: they stored guard *class names*
only. They now store guard text, which is how the duplicate became provable.
