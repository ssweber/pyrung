# Burner / PILOT handoff

## Frame

The PLC is the world and the ship. PILOT steers it one observed scan at a time.
Program-owned departures are not a second controller or a stored transaction;
they are ordinary piloting in a temporarily incomparable world.

The governing rule is:

```text
ORIENT from the current world
-> ACT once or coast under an observed bearing
-> VERIFY agency and immediate bearing effect
-> RECORD what actually happened
-> ASSESS target-relative progress
-> checkpoint, continue provisionally, investigate/revert, or stop honestly
```

Plans are never carried across observations. The compass is queried again every
ORIENT. A route is evidence for the next bearing only.

## Current implementation

- VERIFY/ASSESS now expose independent evidence axes:
  `agency`, `bearing`, `progress`, and `new_frontier`. `Outcome` remains only a
  compatibility projection.
- Only the immediate requested channel value satisfies a bearing. Membership in
  a stored route suffix has no acceptance meaning.
- `observe_label` is diagnostic only. `MotionKind` describes intervention,
  coast-to-bearing, and coast-holding-world behavior.
- Candidate and trial plumbing carries one immediate bearing, not
  `expected_channel_values`.
- ORIENT uses `routes.live_compass_plan` on every iteration. Avoided actions are
  removed before path selection, including actions later in a prospective path.
- Chart construction now preserves a parallel automatic edge when the existing
  producer-family reader proves that a command value has a program-owned
  producer. Avoiding an operator exemplar therefore does not erase the PLC's
  own way to produce the same transition.
- A program departure with a proven clean continuation opens a bounded
  provisional attempt. The attempt stores only a gauge receipt, rollback depth,
  classification, and expiry—not a route.
- Provisional settlement is evidence-based:
  gauge advanced (or target reached) -> promote; gauge behind -> ordinary
  investigation/revert; preserved or unknown -> continue within the bound;
  expiry -> rollback without manufacturing a regression nogood.
- Further clean channel motion inside an existing provisional attempt is just
  more piloting. It keeps the original receipt and budget instead of nesting a
  second provisional state or manufacturing a regression.
- Unknown classification is not permission to wander. Without affirmative clean
  continuation evidence it follows the conservative investigation/revert arm.
- Replay keeps the exact pre-act world, including PilotRungs. The provisional
  gauge begins at the observed departure world so already-earned coast progress
  is not counted twice.
- Investigation restricts latched-failure evidence to the deep causal spine of
  the channel departure, so normal latched motion is not mislabeled as failure.
  A raw correction that silences those failures is observed through the
  waypoint to a stable landing; its guarded form is then replayed to the first
  landing boundary, where ordinary ORIENT resumes.
- An active guarded correction owns its destination until its guard releases;
  a fresh backward trace cannot append an opposite last-write-wins rung while
  that proof is active.
- Nogoods and executable-world identity include PLC projection plus PilotRungs.

Structured provisional events are now:

```text
provisional_started
provisional_promoted
provisional_regressed
provisional_expired
```

## Burner truth

Production mode correctly applies both `C_ProductionMode` and
`C_UnitModeChgRequest`. Start uses the exact Production/Idle receipt. Starting
(`3`) is an observed waypoint; Execute (`6`) is the useful landing. A Start
alarm replay learns the joint generic latch correction:

```text
x_DoorClosed=True + x_LintDoorClosed=True
```

The current corrected retry reaches Execute at scan 913. There is no burner-name pair
heuristic.

The focused HELD gate now demonstrates the generalized policy:

```text
Execute / Step 101
-> program-owned Hold
-> HELD / Step 103 (provisional, gauge baseline begins here)
-> ordinary trace opens the door
-> Step 105 advances (provisional_promoted immediately)
-> live ORIENT reads Unhold
-> unsafe attempt exposes DoorAlarm
-> investigation proves a guarded DoorClosed=True correction
-> corrected Unhold reaches Execute
-> correction yields at its observed landing
-> ordinary piloting reaches Completed without C_Complete
```

Promotion occurs at the gauge proof (`103->105`), not at a stored channel
return. This is the intended classification behavior.

## Live target and scripts

The current export is:

```text
C:\Users\Sam\AppData\Local\Temp\CLICK (00010A00)\pyrung_project
```

The reconstruction and repro scripts default to that export, print it, and fail
immediately if it is absent. `PYRUNG_CLICK_PROJECT` can override it.

```powershell
uv run python scratchpad/burner/reconstitute_completed_steps.py
uv run python scratchpad/burner/repro_completed.py
```

The constructive script proves the intended post-burner sequence through Dry,
Cool, program Hold, door cycle, Unhold, Shine, internal Complete, and
`S_StateCurrent == 17` without pressing `C_Complete`.

The live autonomous run (2026-07-13, after the exposure-guard + wait-nogood
work) confirms the joint Start correction, reaches Execute at 913, gets bumped
to HELD by the door release (see below), pays ONE sterile 11->16 coast, then
the wait nogood re-routes ORIENT to `C_Unhold` at scan 919 — the trace
re-stages the FB prerequisites under the Unholding context and the passage
returns to Execute at 1712. The HELD passage is closed.

Two residuals, in priority order:

1. **Provisional expiry destroyed the march (the terminal failure).** The
   scan-108 provisional (opened on the Clear `9->2` departure) rode the whole
   run; its gauge anchor (`Internal__Step=101`) never advanced because the
   recipe needs the dry phase, so at `expires_at` (scan 2108) the expiry arm
   rolled the world back to the scan-5 checkpoint — vaporizing 2200 scans of
   real march (Idle, the corrected Start, the HELD passage) and terminating at
   Aborted with the `A_Alm10_Status` red herring. The march had banked
   ordinary trend checkpoints inside the provisional; CLAUDE.md's own phrase —
   "the landing is provisional until ordinary progress banks a checkpoint" —
   says that should have PROMOTED it. Fix direction: promotion on banked
   ordinary progress (trend checkpoint above the provisional boundary), not
   solely on gauge advance; expiry then only ever rolls back a march that
   truly earned nothing.

2. **Doors still release at Execute (the Hold bump, ~800 wasted scans/lap).**
   The joint door+lint correction falls back to landing scope because
   `_exposure_guard` is honestly poisoned: the recipe's own door-open advance
   (`HoldForShine & ~i_DoorClosed -> TransBool`) is a silenced consumer whose
   chain reaches the command pipeline with no channel-state gate (its gate is
   the step flag, which is gauge territory, not channel territory). The
   mechanism cannot yet tell damage from purpose. Clean future lever: a
   silenced consumer whose chain ADVANCES the gauge is purpose, not damage —
   exclude it from the exposure instead of poisoning. Until then the loop
   survives via the Hold-bump -> wait-nogood -> Unhold cycle.

## Remaining independent frontiers

- Sterile zoom: a coast whose channel does not move, gauge does not advance,
  and frontier does not change must remain a bad edge/stall—not a special zoom
  rule.
- Cycle fold: improve long burner dwells without changing classification.
- Retire remaining historical “detour/corridor” prose and internal filenames
  opportunistically; do not preserve their semantics.

## Validation snapshot

Full PILOT suite after this migration:

```text
374 passed, 31 skipped, 1 expected failure
```

Ruff and the repository-scoped ty check pass. The constructive burner script
reaches COMPLETED(17) at scan 2817 with all stage assertions and cold alarms.
