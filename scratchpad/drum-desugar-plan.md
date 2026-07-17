# Drum desugaring plan

Status: feasibility/design note. No implementation yet.

## Decision

Desugar event and time drums at program-build time into ordinary primitive
rungs/instructions. Execution, trace, crossings, accumulator resolution, coast,
proof, and whole-program surveys should consume the lowered program and acquire
no drum-specific arms.

This is feasible, but it is a normalization feature rather than a local builder
rewrite. The authored representation must remain available for source locations,
debugging, CLICK validation, and native drum export. A second, deterministic
lowered representation should drive simulation and analysis.

The intended boundary is:

- **Authored program:** retains `EventDrumInstruction` /
  `TimeDrumInstruction` as source-level syntax.
- **Lowered program:** contains calls and ordinary primitive callee rungs only.
- **Origin map:** maps every lowered call/rung/instruction back to its authored
  drum and source rung.

Once the lowered form is authoritative, delete the drum-specific crossing and
prove/classification machinery, and remove the "future drum profile" seam from
`core/analysis/pilot/accumulators.py`. Frontend, validation, and export code may
still understand authored drums.

## Correct numeric contract

A time drum accumulator is an `INT`, like a timer accumulator, and clamps at
`32_767`.

The current drum implementation's DINT accumulator path and
`2_147_483_647` clamp are wrong. Do not broaden `OnDelayInstruction` to match
that behavior. Instead:

- restrict `TimeDrumInstruction.accumulator` to `TagType.INT`;
- remove the drum `_DINT_MAX` accumulator path;
- preserve timer-style `32_767` saturation.

The step register and preset operand types are a separate API decision; this
correction is specifically about the time accumulator.

## Overall lowered-call shape

An authored drum becomes an internal call at the exact instruction position:

```text
authored flow
  |
  +-- ordinary instructions before the drum
  |
  `-- call_always(@lowered/drum/<stable-id>,
                  auto=caller_power,
                  source_view=caller_rung_snapshot)
```

This cannot be an ordinary powered `call()` without qualification:

- the body must run when caller power is false because reset and edge
  bookkeeping remain active;
- helper conditions must initially read the caller's frozen rung snapshot;
- later phases must take fresh snapshots to observe primitive writes made by
  earlier phases;
- it must execute at the original instruction position so branches,
  subroutines, loops, and later source-order items retain their ordering.

The generic internal call frame therefore carries caller power and the caller's
condition view. Analyses should see an ordinary call edge into primitive rungs,
not a drum instruction.

```text
@lowered/drum/<stable-id>
  |
  +-- source-view prologue
  |     sample reset
  |     sample jump
  |     sample jog
  |     sample per-step events       [event drum]
  |     copy caller power -> __auto
  |
  +-- fresh/live prologue
  |     clear scan-local flags
  |     normalize an invalid Step when __auto
  |
  +-- automatic advance
  |     descending event transition bank
  |                                  [event drum]
  |       or
  |     select preset -> TON -> consume phase Done
  |                                  [time drum]
  |
  +-- priority controls
  |     reset
  |     jump rising edge
  |     jog rising edge
  |
  +-- timer cleanup                 [time drum]
  |     clear Done/Acc/fraction after a phase change
  |
  +-- output decoder
  |     guarded copies from final Step to each output
  |
  +-- event bookkeeping            [event drum, if CLICK requires it]
  |
  `-- return
```

Compiler temporaries must be typed, hidden, non-steerable, and associated with
source/alias provenance. They must not leak into public snapshots, exports,
coverage, diagnostics, or lock projections. Truly stateful lowering variables
must remain in verifier state; scan-local derived temporaries should be elided.

## Event drum lowering

### Descending automatic-transition bank

Generate step transitions in reverse order:

```python
with rung(Step == 3, rise(__event_3)):
    latch(Done)

with rung(Step == 2, rise(__event_2)):
    copy(3, Step)

with rung(Step == 1, rise(__event_1)):
    copy(2, Step)
```

The event conditions are sampled from the authored rung's frozen condition view
into per-step Boolean temporaries. Descending order is the important part:
if the step-1 rung writes `Step = 2`, the step-2 rung has already run, so an
automatic transition cannot cascade through multiple steps in one scan.

After this bank, execute controls in the native priority order:

1. automatic event advance;
2. reset;
3. jump rising edge;
4. jog rising edge;
5. decode outputs from the final live step.

Reset, jump, and jog therefore cannot re-enter the automatic bank during their
entry scan.

### Unresolved CLICK behavior: external step write plus event edge

We need to test this specific snafu on a real CLICK PLC. Pyrung's current
`event_ready` behavior is not evidence of the controller's behavior.

The disputed scan is:

```text
previous scan: Step = 1, Event2 = False
current scan:  a rung before the drum writes Step = 2,
               and Event2 rises False -> True
```

In concrete ladder terms, arrange an earlier rung that copies literal `2` into
the drum's current-step register on the same input scan that the step-2 event
turns on. Then observe the step immediately after the drum executes:

- **Result `Step = 3`:** CLICK accepts the same-scan Event2 edge after the
  external step write. The simple descending transition bank is correct.
- **Result `Step = 2`:** CLICK treats this as entry into step 2 and requires a
  later Event2 edge. The lowering needs entry tracking, such as a
  `Step == __last_step` guard (plus an invalid-step normalization flag).

Run these controls as part of the audit:

1. Earlier rung writes `Step = 2` while Event2 is already high.
2. Earlier rung writes `Step = 2`; Event2 rises on the following scan.
3. Earlier rung writes `Step = 2` while Event2 rises on that same scan.
4. Move the `copy(2, Step)` rung after the drum and repeat, confirming CLICK's
   scan-order effect.
5. Repeat the same-scan case with reset, jump, and jog disabled so only the
   disputed automatic transition can move the step.

Use real hardware as the oracle (or first establish that a CLICK simulator is
firmware-faithful for this instruction). Record CPU/firmware, project, rung
order, input trace, and observed step values with the result.

Do not retain explicit `event_ready` / `event_prev` merely because Pyrung
currently has them. If CLICK advances to 3, remove that semantic complication.
If CLICK stays at 2, reproduce the observed entry rule using generic primitive
state and guards.

## Time drum lowering

Use one ordinary on-delay timer profile:

- public `INT` accumulator;
- internal phase-Done bit;
- internal selected-preset tag;
- source caller power as its advance condition;
- internal accumulator-reset request.

Approximate phase order:

```python
# Source-view sampling
sample(caller_power, __auto)
sample(reset_condition, __reset)
sample(jump_condition, __jump)
sample(jog_condition, __jog)

# Fresh/live normalization and preset selection
clear(__reset_acc)
normalize_step_if_enabled()
select_current_step_preset(__selected_preset)

# Ordinary accumulating primitive
with rung(__auto):
    on_delay(__phase_done, Acc, __selected_preset)

# These fresh rungs see the phase-Done write.
with rung(__auto, Step == 1, __phase_done):
    copy(2, Step)
    latch(__reset_acc)

# One transition per non-final phase...

with rung(__auto, Step == final_step, __phase_done):
    latch(Done)

# Native priority after automatic progress
apply_reset()
apply_jump_edge()
apply_jog_edge()

# Clear Acc and the timer's fractional remainder in this same scan.
reset_phase_timer_if(__reset_acc)

decode_outputs_from_final_step()
```

The cleanup must reset the same timer owner after the transition/control phase,
not introduce a second accumulator owner. It must clear the hidden fractional
remainder as well as the visible accumulator. A plain `copy(0, Acc)` is not
equivalent for fractional scan periods.

Public completion remains a separate sticky latch:

- intermediate phase Done causes a step transition and timer reset;
- final phase Done latches public completion;
- disabling or jumping does not clear public completion;
- reset clears it.

Consequently `resolve_profile(Acc)` resolves directly. A trace from public
completion should descend through its latch guard to internal phase Done and
then reach the ordinary timer profile/coast.

## Analysis and identity requirements

The normalized representation must be the shared input to:

- interpreted and compiled execution;
- fold source collection;
- PDG and causal recording;
- trace and crossings;
- pilot accumulator resolution and coast;
- proof/reachability;
- the wait-edge survey and other whole-program queries.

Generic compiler-temp transparency is required. For example, the wait survey
must understand that `rise(__event_2)` came from an external authored Event2
condition; otherwise it will see `__event_2` as program-written and incorrectly
conclude that the program can supply the wait. Prefer a generic alias/origin
normalization pass, not a drum arm in the survey.

Generated writes must be grouped by authored origin so the following continue
to describe one source drum:

- duplicate/conflicting-output validation;
- readonly validation;
- coverage and rung firing capture;
- DAP stepping and source locations;
- causal writer labels;
- diagnostics and CLICK mapping errors.

Native CLICK and CircuitPython export should consume the authored program unless
there is a deliberate request to export the primitive expansion.

## Implementation arc

1. Add authored/lowered program separation, stable origin metadata, internal
   tags, and the always-invoked lowered-call frame.
2. Implement event-drum lowering with descending transition rungs.
3. Perform the real CLICK same-scan step-write/event-edge audit and pin the
   resulting contract.
4. Restrict time-drum accumulation to `INT` and implement time-drum lowering.
5. Differential-test lowered execution against the old native instructions,
   treating CLICK results as authoritative where they disagree.
6. Switch runner, fold, graph, trace, pilot, proof, crossings, and surveys to
   the lowered representation.
7. Delete drum-specific analysis code and the future-profile seam.

## Acceptance tests

Differential/property tests should cover:

- all simultaneous combinations of automatic advance, reset, jump, and jog;
- the CLICK-audited external step-write/event-edge case;
- multiple events rising in one scan (at most one automatic advance);
- an event already high before a step is entered;
- shared event conditions across multiple steps;
- composite event/jump/jog conditions and frozen source snapshots;
- invalid externally patched step values;
- disabled output freezing;
- sticky completion across disable/jump/jog;
- dynamic presets;
- fractional `dt` and same-scan accumulator reset;
- `INT` saturation at `32_767`;
- branches, subroutines, and loops;
- folded versus scan-by-scan equality;
- trace from an output and from public completion to the timer/event cause;
- wait-edge survey attribution to the authored external event;
- unchanged source locations, rung identities, and native CLICK export.

