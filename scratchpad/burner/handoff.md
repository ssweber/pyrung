# Burner PILOT handoff - current frontier as of 2026-07-08

This file intentionally starts from where we are today. The old rotate-sensor
handoff is historical context now: `how(y_BurnerLoop)` is solved again. The live
frontier is:

```text
how S_StateCurrent==17 avoid C_Complete
```

The real problem is not a burner-specific recipe hack. It is generalized
recognition of program-directed progression: how PILOT should read that the PLC
program itself is driving a command/state current, when the operator must only
supply the few legal external nudges and otherwise let internal steps, timers,
and program-owned command writes run.

## Next Reviewer Orientation

Before touching code, take a long overview pass through the pending working-tree
changes and read them as one system:

- `prove/classify.py`: indirect constant-table stepping coupling.
- `pilot/evidence.py`: `_ExploreContext` classifications as transition evidence.
- `pilot/charts.py`: route graph / `ANY_FROM` edge semantics.
- `pilot/candidates.py`: grounded-route ordering, wildcard wait refusal, current
  candidate wiring.
- `pilot/currents.py`: the first read-side recognizer for program-owned detours.
- `tests/core/analysis/test_stepping_indirect.py` and
  `test_pilot_table_detour.py`: the current fixture-level contracts.

The idea to internalize thoroughly:

```text
Command/request registers participate in the same transition pipeline that
changes the channel. Their writers are route causes, whether the writer is an
operator button or a program-owned rung.
```

For the current frontier, seeing `copy(CmdCompleteRef, C_CtrlCmd)` and
`copy(1, C_CmdChgRequestBool)` as part of the same pipeline that later changes
`S_StateCurrent` is the unlock. Once those writes are recognized as the producer
for the `S_StateCurrent 6->16` edge, their guard (`rise(S_Shining_tmr.Done)`)
becomes the real leaf to trace. That pulls in the recipe current (`Internal__Step`
and timers), so `Execute -> Held -> Execute -> Shining -> Completing` can be
recognized as progress instead of regression.

Do not treat this as a local burner exception. The generalized problem is edge
availability: a table-visible channel edge is not waitable until the producer
of its command/request registers is available.

## Current Facts

`how(y_BurnerLoop)` is restored on the real CLICK project.

Useful current checks:

```powershell
uv run python scratchpad/burner/repro_regression.py
uv run python scratchpad/burner/repro_burnerloop_longpath.py
```

Observed now:

- `repro_regression.py` reaches `y_BurnerLoop`.
- The first route plan is keyed on the channel `S_StateCurrent`, not the mask word.
- The route starts with `C_Clear`, then coasts, then `C_Reset`, then `C_Start`.
- `repro_burnerloop_longpath.py` reaches and verifies `C_Clear`, `C_Reset`,
  and `C_Start` are all present.

The mask regression is understood. The indirect constant-table stepping coupling
made derived read leaves such as `isStateEnbl__statemask` stepping. That fact is
not inherently wrong. The bug was letting those derived leaves act like
navigable channels. Their compass edges are wildcard edges:

```text
ANY -- C_Start --> isStateEnbl__statemask = 2
```

Wildcard `ANY_FROM` edges are weak evidence. They are not grounded state
transitions and must not be wait/coast authorities. The current dirty tree
contains the tactical fix: grounded routes outrank wildcard routes, wildcard
waits are refused, and `ANY_FROM` is formatted legibly instead of leaking a raw
object repr.

## The New Failure

The current console frontier:

```text
>>> how S_StateCurrent==17 avoid C_Complete
```

currently bails with a misleading ending:

```text
stuck: pilot: unreachable - frontier isStateEnbl_Yes=1 is gated by free word
'A_Alm100_Status' (external, no declared domain); the skiff has no sound probe
values for it. Declare choices= (or min=/max=) on A_Alm100_Status so the prover,
bounds, and skiff can resolve it.
```

Full console shape from the real run:

```text
Planning...
  target: S_StateCurrent=17, 735 steerable tags
  set C_ProductionMode=True, C_UnitModeChgRequest=True  (scan 4)
  progress: distance 2
  set C_Clear=True  (scan 7)
  progress: distance 2
  set C_Reset=True  (scan 10)
  progress: distance 2
  set C_Start=True  (scan 13)
  frontier: distance 15
  hold x_BlowerFB, x_RotateFB
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 3->6)
  coast accepted  (42 scans, scan 55)  S_StateCurrent->6
  regression: reverted, x_LintDoorClosed=True, x_DoorClosed=True
  set C_Start=True  (scan 13)
  frontier: distance 13
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 3->6)
  coast accepted  (802 scans, scan 815)  S_StateCurrent->6
  progress: distance 2
  coast: waiting for S_StateCurrent  (terminal let-run (hold macro-state, coast to target))
  coast accepted  (1040 scans, scan 1855)  S_StateCurrent->6
  regression: reverted, pulse x_RotateSensor
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 6->16)
  coast: waiting for S_StateCurrent  (terminal let-run (hold macro-state, coast to target))
  coast accepted  (1737 scans, scan 2552)  S_StateCurrent->6
  regression: reverted, A_Alm16_Status=1
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 6->16)
  coast accepted  (40 scans, scan 855)  S_StateCurrent->16
  regression: reverted to checkpoint
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 6->16)
  coast: waiting for S_StateCurrent  (terminal let-run (hold macro-state, coast to target))
  coast accepted  (37 scans, scan 852)  S_StateCurrent->6
  regression: reverted to checkpoint
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 6->16)
  coast: waiting for S_StateCurrent  (terminal dwell (re-coast skip: let-run already tried at key))
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 6->16)
  coast: waiting for S_StateCurrent  (terminal dwell (re-coast skip: let-run already tried at key))
  coast: waiting for S_StateCurrent  (let-run S_StateCurrent: 6->16)
  coast: waiting for S_StateCurrent  (terminal dwell (re-coast skip: let-run already tried at key))
  stuck: pilot: unreachable - frontier isStateEnbl_Yes=1 is gated by free word 'A_Alm100_Status' ...
```

That is not the honest explanation. It means PILOT abandoned the state/recipe
current and chased an enable predicate down to an unconstrained environmental
word. `A_Alm100_Status` may be part of a static enable expression, but it is not
the primary bearing for reaching `S_StateCurrent==17` while avoiding the operator
button `C_Complete`.

The transcript matters because it is not a total compass failure. PILOT is
already on the right channel for much of the run:

- it navigates cold start through `C_Clear`, `C_Reset`, and `C_Start`;
- it reaches `S_StateCurrent=6`;
- it repeatedly recognizes `S_StateCurrent: 6->16` as the next state-machine
  bearing;
- it even reaches `S_StateCurrent=16` once, then reverts.

The failure is at the boundary between state navigation and program-directed
current progression. Productive program-owned motion is being classified as
regression, or the investigation response is installing/naming alarm/free-word
causes that are not part of the constructive route. After enough reverts and
re-coast skips, the final stuck explanation is no longer the real frontier.

Very likely specific misread: entering `HELD(11)` during the recipe is something
the program wanted, but PILOT may only see "moved away from the target
`S_StateCurrent==17`" and classify it as regression. The constructive ledger
proves the detour is necessary:

```text
EXECUTE(6) -> HELD(11) -> EXECUTE(6) -> COMPLETING(16) -> COMPLETED(17)
```

So regression reporting must print the channel transition it is reverting, not
just the hypothesized cause. A line like:

```text
regression: reverted, S_StateCurrent 6->11, cause=...
```

would immediately separate a true destructive move (`6->8 Aborting`,
`A_Alm16_Status=1`) from a program-intended detour (`6->11 Held`) that the current
reader should recognize and preserve.

The constructive route exists.

Run:

```powershell
uv run python scratchpad/burner/reconstitute_completed_steps.py
```

Observed:

```text
SUCCESS: reached COMPLETED(17) at scan 2817
reached COMPLETED(17)              : True
every stage landmark asserted      : True
alarm words at cold value throughout: True
```

Important ledger:

```text
ABORTED(9)
Production mode
Clear / Reset / Start
EXECUTE(6)
burner loop achieved
Dry -> Cool -> HoldForShine
HELD(11)
door cycle -> ShineAdded
Unhold -> EXECUTE(6), Step 109 Shine
Shine timer done -> COMPLETING(16)
COMPLETED(17)
```

Red-herring proof from the script:

```text
A_Alm100_Status final = 0
A_AlmExtent final = 0
No alarm status word or trigger bit ever left its cold value
```

The program reaches Completed by recipe progression. The operator does not press
`C_Complete`. The program itself later writes the internal complete command
(`C_CtrlCmd=10`) after the shine step/timer. Therefore the free-word alarm
decline is a symptom of wrong role/frontier selection, not the real blocker.

## Role Model We Need

The key distinction:

```text
stepping fact != channel authority
```

Suggested generalized roles:

- Channel: the authoritative state coordinate being navigated. Here:
  `S_StateCurrent`.
- Request/command pipeline: transient command/request registers consumed by the
  state pipeline. Here: `S_StateRequested`, `C_CtrlCmd`,
  `C_CmdChgRequestBool`.
- Program current coordinate: internal recipe/phase/step state that explains why
  the program will later issue a command or wait. Here: `Internal__Step`,
  `Internal__TransBool`, timer done bits, step flags.
- Enable/predicate leaves: derived facts that must be true for a transition but
  are not the route axis. Here: `isStateEnbl_Yes`, mask words, alarm masks.
- Scratch/projection: scan-local implementation detail, pointer arithmetic, WBR
  elision, affine projections. These should inform analysis but not become
  channels.
- Free/environment input: external unknowns such as `A_Alm100_Status`; these can
  be caveats under a route, not default top-level explanations.

## ExploreContext Is The Clue Store

`_ExploreContext` already computes much of the information role inference needs:

- `elided_tags`: scan-local / write-before-read scratch.
- `functional_dep_projections`: affine projections and scratch pointers mapped
  back to their representative.
- `stepping_tags`: tags that visit multiple values.
- `combinational_tags`: derived combinational facts.
- `init_constant_projections`: constant-ish projections.
- `free_input_names`: unbounded external inputs.

`TransitionEvidence` already imports these into `pilot/evidence.py`:

```text
functional_deps=dict(explore_ctx.functional_dep_projections)
elided=dict(explore_ctx.elided_tags)
stepping=frozenset(explore_ctx.stepping_tags)
free_inputs=frozenset(explore_ctx.free_input_names)
combinational=frozenset(explore_ctx.combinational_tags)
init_constants=frozenset(explore_ctx.init_constant_projections)
```

It also exposes:

```text
evidence.elided_tags()
evidence.is_internal(tag)
evidence.is_stepping(tag)
evidence.classify(tag)
evidence.functional_dependencies()
evidence.affine_projections()
```

That should be promoted from "helpful side data" to a first-class role inference
input. A tag can be stepping and still be a projection/read leaf. A tag can be
in `opaque_loop` and still be a mask/predicate participant rather than the
channel.

Possible API shape:

```python
evidence.role_candidate_kind(tag)
# "channel" | "request" | "current" | "enable_leaf" |
# "projection" | "scratch" | "free" | "unknown"
```

Or, less grandly, tighten `_infer_pipeline_roles_for_context`:

- reject or internalize `evidence.is_internal(tag)`;
- reject tags whose canonical representative is another tag unless the
  representative is not available;
- reject derived/combinational/free tags as channels;
- require at least one route with concrete `from_values` before a tag can be a
  coastable/navigable channel;
- allow projection and enable tags to remain in routes as guards/evidence;
- prefer the canonical representative/root of a functional dependency cluster.

## Generalized Program-Current Recognition

The toy table-detour fixture now has `pilot/currents.py`, but the real requirement
is broader: recognize program-owned progression in arbitrary generated programs,
not just hand-built examples.

The general read-side question:

```text
At the current channel state and program current state, is the program waiting
for exactly one external operator action, or is it self-driving via timers,
internal steps, or program-owned command writes?
```

A generalized current reader should identify:

- command producer rungs: rungs that write command/request registers such as
  `C_CtrlCmd` or `S_StateRequested`;
- command consumers: state-transition rungs that consume those command/request
  values from the current channel state;
- producer source kind:
  - steerable operator action, such as `C_Clear`, `C_Reset`, `C_Start`,
    `C_Unhold`;
  - internal program action, such as a step transition or timer done bit writing
    `C_CtrlCmd=10`;
  - environmental/sensor action, such as a door cycle;
  - ambiguous or free input, which should fail closed;
- active current window: the internal step/state predicates that make a producer
  legal now, not merely somewhere in the program.

The sharper mechanism: a PackML table edge must not be treated as immediately
available just because the destination table contains it. For example, the
static state graph sees:

```text
S_StateCurrent 6 -> 16
```

But the real producer for that edge is guarded by the recipe current:

```python
with rung(rise(S_Shining_tmr.Done)):  # production_execute_steps.py R23
    copy(CmdCompleteRef, C_CtrlCmd)
    copy(1, C_CmdChgRequestBool)
```

So `6->16` is not "be in Execute and wait." It is:

```text
be in Execute
advance Internal__Step to 109 / Shining
run S_Shining_tmr to Done
program writes C_CtrlCmd=10 and C_CmdChgRequestBool=1
then the PackML table edge fires 6->16
```

That producer-side guard chain is the missing leaf. Today PILOT sees the table
edge and thinks it has a clear shot from Execute to Completed. It does not surface
the internal step/timer prerequisites that make the program-owned Complete
command available. As a result, the required detour through Held can look like
"backward progress" instead of progress in the joint current state.

The useful distance is not only over `S_StateCurrent`; it is over the joint
availability state:

```text
(S_StateCurrent, command producer readiness, Internal__Step, timers/counters)
```

Going `Execute -> Held` can be farther from `S_StateCurrent==17` while still
being closer to the only non-avoided producer of `C_CtrlCmd=10`.

Desired behavior:

- If there is exactly one legal non-avoided external action for the active
  current window, surface it as a candidate.
- If the next move is program-owned, prescribe wait/let-run with the channel
  and current context, not a random enable leaf.
- If a channel edge is table-visible but producer-guarded, surface the
  producer-side prerequisites as current leaves before treating the edge as
  waitable.
- If the next move requires an environmental action, name that as the route's
  actual missing current input.
- If all routes are blocked by avoided actions, say that.
- If a free word is genuinely decisive for every viable route, attach it as the
  blocker. Do not let it become the headline just because trace chased an enable
  predicate first.

For this burner route, `avoid C_Complete` excludes the operator button. It does
not imply "the program may never internally write complete command value 10".
The constructive script proves that internal command write is the intended route.

## Acceptance Gates For This Frontier

Keep these as local scratch gates while iterating:

```powershell
uv run python scratchpad/burner/repro_regression.py
uv run python scratchpad/burner/repro_burnerloop_longpath.py
uv run python scratchpad/burner/reconstitute_completed_steps.py
```

Expected:

- `repro_regression.py`: `reached=True`; first route keyed on the channel
  `S_StateCurrent`; no mask hijack.
- `repro_burnerloop_longpath.py`: `reached=True`; `C_Clear`, `C_Reset`,
  `C_Start` all present.
- `reconstitute_completed_steps.py`: `SUCCESS: reached COMPLETED(17) at scan
  2817`; alarm words cold throughout.

New target:

```text
how S_StateCurrent==17 avoid C_Complete
```

Desired outcome:

- Reach `S_StateCurrent==17`.
- Do not press operator `C_Complete`.
- Follow the program-directed current through HoldForShine, Held, door/ack or
  equivalent legal external nudge, Unhold, Shine, program-issued Complete,
  Completing, Completed.
- If it declines, the reason must be state/current-shaped, not a raw
  `A_Alm100_Status` free-word ending unless that word is proven decisive for
  every viable current route.

## Suggested Next Scratch Probes

Useful probes should dump role/evidence facts, not just event logs:

- For every `opaque_loop` tag, print:
  - `evidence.classify(tag)`;
  - `evidence.representative(tag)`;
  - whether it is elided/internal;
  - whether it has functional dependents;
  - number of routes and how many have concrete `from_values`.
- For `S_StateCurrent==17 avoid C_Complete`, dump the route tree frontier at
  every point it switches from channel/current reasoning to enable leaf.
- Dump command producers and consumers around `C_CtrlCmd` and
  `S_StateRequested`, annotated by source kind: operator, internal, timer,
  environmental, ambiguous.
- For the `S_StateCurrent 6->16` edge, dump the command producer selected for
  `C_CtrlCmd=10` and its unsatisfied guard chain. Confirm that
  `rise(S_Shining_tmr.Done)` pulls in `Internal__Step=109 / Shining` and the
  preceding current chain instead of becoming a blind wait from Execute.
- Record when a route decline names a free word; include the owning channel
  route and whether another route avoids that free word.
- Improve regression console payloads: print the channel tag transition(s)
  being reverted, especially `S_StateCurrent old->new`, alongside the named
  cause. The current `regression: reverted, cause` format hides whether PILOT is
  undoing a genuine error or undoing the program's intended detour.

The goal is not to special-case `Internal__Step` or the burner. The goal is to
teach PILOT to read program current structure the same way it already reads
state-transition structure.
