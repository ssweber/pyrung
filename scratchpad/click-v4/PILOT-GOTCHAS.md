# CLICK v4: PILOT gotchas per feature

Read-only audit at `dev` HEAD `0a57b20` (2026-10-22). Companion to `RESEARCH.md` and `TOUCHPOINTS.md`.
TOUCHPOINTS lists the files each feature has to touch. This file lists what PILOT (`how()`, under
`src/pyrung/core/analysis/pilot/`) and the core machinery it leans on (fold, PDG, steerability, crossings,
the prover context) would get **wrong** once the new instructions exist. Most of these are not on the
TOUCHPOINTS list, because nothing in them names the sibling class.

**Severity.** S1 = silent wrong answer: a wrong REACHED witness or dwell, a wrong CANNOT_REACH, a wrong
steerable lever, or a fold that skips real behaviour. S2 = decline: `STOPPED`, budget burned, a route
missed. S3 = cosmetic, or performance only.

**Confidence.** *seen* = I read the code at that line. *probed* = I ran a throwaway script; the scripts are
in the session scratchpad and nothing was written to the repo. *inferred* = I followed the pattern but did
not trace or run it.

**Why most problems are declines, not wrong answers.** PILOT verifies every act by executing it on a fork.
Static mis-modelling therefore usually costs trials (S2), not correctness. The S1 cases are where that
oracle itself is bypassed:
1. **Folding.** Ordinary fold, cyclefold and the sterile-cycle shortcut advance logical time without
   executing every scan.
2. **Steerability.** PILOT is allowed to patch a steerable tag directly.
3. **The two CANNOT_REACH proof sites.** `multitarget.analyze` and `target_route.linked_feedback_block`
   (`api.py:141-149, 223-241`).
4. **Determinism.** Replay and forks are assumed to reproduce a scan from state alone.

---

## Top findings

| # | Sev | Finding | Features | Conf. |
|---|---|---|---|---|
| X1 | S1 | **Fold skips unregistered time-driven instructions.** Fold only knows about time through an exact-type registry. Its frozen-rung analysis then hides the new instruction's writes from the plateau guard, and a single scan with a big `dt` runs over its behaviour. Probe: `run_until` with a flasher stand-in counted **2 flashes folded vs 6 unfolded**. | 3, 4 (and 2) | probed (run_until); inferred for the PILOT coast |
| X2 | S1 | **Writes the PDG cannot see become steerable levers.** If the Timer UDT is stored as one field instead of separate Done/Acc tags, `Timer.Done` has readers but no writers, so it is classed as a **steerable input**. Probe: `T5_Done` came back in `compute_steerable`. A nested-loop index tag missing from `_writes` would fall into the same hole. | 4, 5 | probed |
| X3 | S1→S2 | **The fold registry is the only place timed progress is certified.** Cyclefold's ring adds `_frac:` only for registered sources (`cyclefold.py:541-547`). A coarse-unit timer that is not registered looks like an exact cycle. The sterile shortcut then either returns "never reachable" or stamps time forward without executing anything. | 3, 4 | seen |
| X4 | S2 (S1 latent) | **`ote_writes` is PILOT's only "follows its rung" fact.** Everything not in it is treated as retentive. That gives the wrong polarity for False targets, the wrong default routes, and a preserve walk that never expects a self-clobber. The CANNOT_REACH proof in `multitarget._writes_off` is safe only while the new crossings' `forward()` stays `UNKNOWN`. | 4 | seen |
| X5 | S2 | **Edge semantics are invisible to PILOT.** `edge_tags` comes only from `rise()`/`fall()` in conditions. A toggle or timed rung whose level guard is already true is read as "ready writer", and the drive coasts until it STOPs. Hold expiry and re-assert also creates unrequested edges. Probe: toggle with the button held came back `STOPPED`. | 4 | probed |
| X6 | S2 | **`AdvanceProfile` allows one accumulator and one monotone Done.** Debounce and flasher own two timers, and a flasher has no "done". The consumers assume one accumulator per owner. | 3, 4 | seen |
| X7 | S2 | **Loop bodies are flattened, and body instructions have no enable condition.** Probe: `count_up` inside `forloop` gives `STOPPED`, while the same counter flat gives `REACHED` in 31 scans. Body rungs make this worse, and execution journals have no address for a body rung or iteration. | 5 | probed (pre-existing) / inferred |
| X8 | S1 | **randint breaks fold soundness** for a calc body that has no tag reads (the frozen-rung rule), breaks the scan_id alignment of the landing scan, and the seed does not ride along on a runner fork. Details are in the randint section. | 2 | seen (sub-audit) |
| X9 | S1 latent | **A `SumExpr` subclass or alias would forward as `Aggregate(operation="sum")`.** `crossings/calc.py:133` dispatches on `isinstance(expr, SumExpr)` and builds the default `"sum"` operation, so MAX/MIN/MEAN/MEDIAN would forward as sums. | 1 | seen |
| X10 | S2 | **The whole-program prover context fails silently.** One instruction anywhere without a compile or prover arm drops every `how()` to the fallback: no nd_domains, a pivot-tag world key, no transition evidence. | all | seen |

---

## Cross-cutting (ordered by severity)

### X1. Fold: time enters only through `_ensure_registry` (S1, probed)

- **`core/fold.py:133-150`.** `_SOURCE_REGISTRY` is keyed by **exact type**, and `_collect_acc_sources`
  (`:153-185`) uses `registry.get(type(instr))`. Only on_delay, off_delay, count_up, count_down and
  time_drum are registered. A subclass is not registered, and neither is a new class. *(seen)*
- **`core/fold.py:483-519` `_frozen_rung_writes`.** It seeds `varying` from accumulators, churn, mirrors and
  harness feedback, then propagates read to write. A rung whose reads don't meet `varying` is "frozen", and
  **all of its writes are removed from the plateau guard**.
  - A flasher, debounce, timed or time_event_drum rung reads only its rung contacts, which are constant in
    a plateau.
  - So the instruction's output, its Timer Acc/Done, and everything downstream of them are classed frozen.
  - Declaring the acc in `_reads` does not help: the acc is not in `varying` either. *(seen)*
- **`core/fold.py:1317-1342`.** The plateau guard compares `state.tags` only. Memory, including `_frac:`,
  is not compared. *(seen)*
- **`core/fold.py:1564-1585` `_do_fold`.** The skip is executed as **one scan with `dt = skip * normal_dt`**,
  then `scan_id += skip - 1`. A flasher, debounce or timed stand-in sees one huge dt. It can change phase at
  most once, and its preset crossing is not in the skip bound, which comes only from registered sources and
  clocks. *(seen)*
- **Probe** (`fold_flasher.py`).
  - Setup: a `FakeFlasher` instruction (reads `_dt`, toggles a bit every 500 ms of rung-true time) next to an
    `on_delay(Tmr, 5000)`. A rung `rise(Beacon) -> calc(Flashes+1)` counts the flashes.
  - Result: `run_until(Tmr.Done)` with `fold=True` ended at scan 500 with `Flashes=2`. With `fold=False` it
    gave `Flashes=6`. A spy on `_do_fold` showed one fold of 497 scans. *(probed)*
- **PILOT exposure.**
  - PILOT coasts through `CoastSession.seek -> cycle_fold_until`, which drives `_OrdinaryFoldStrategy`
    (`coast.py:446-471`, `cyclefold.py:400-420`), so it has the same machinery.
  - The same program under `plc.how(Tmr.Done)` reached with `Flashes=6` and made **no** `_do_fold` call. The
    coast did not fold in that shape, and I did not chase why.
  - Treat PILOT as exposed (*inferred*): any coast whose fold context contains a registered source and an
    unregistered time-driven writer has the same proof hole.
  - `run_for`/`run_until` (default `fold=True`) are exposed today (*probed*).
- **Units make it worse.** Even if the new acc were somehow visible, a `unit="sec"`/`"min"` acc (or a drum
  `"hour"`/`"day"`) holds the same integer for many scans while `_frac:` grows in memory. The tags-only
  guard then sees a plateau. *(seen: guard is tags-only; inferred: the consequence)*
- **Fix (pick one, in this order):**
  1. Register every new time-driven class as a fold source, with its own crossing arithmetic:
     - timed: one preset;
     - debounce: the active phase's preset;
     - flasher: a *cyclic* source whose crossings are the phase boundaries;
     - time_event_drum: `_StepPreset` with `None` steps.
  2. At minimum, add a generic opt-in such as `READS_DT = True` (or detect `_dt` and `_frac:` use) that seeds
     `_frozen_rung_writes` `varying` with the instruction's writes **and** forces `skip <= 1` while it is
     active.
     - Caveat: a running flasher anywhere then breaks the plateau every scan. That disables folding for every
       long PILOT coast and burns `max_scans` (S2).
     - So option 1 is needed for flasher in particular.
- **Gate.** Add a test that fails when a concrete `Instruction` subclass reads `_dt` but is neither in
  `_ensure_registry` nor explicitly opted out. Also add a fold-vs-unfold parity case per new instruction (the
  probe above, as a test).

### X2. Steerability is "no writer in the PDG + some reader" (S1, probed)

- **`analysis/steerable.py:197-201`.** A tag with no writers and at least one reader is steerable: "pure
  input". *(seen)*
- **`analysis/write_sites.py:72-87, 90-104`.** Writers come only from `_writes` and `_status_fields`
  **leaves that are `Tag` or `BlockRange`**. A field holding a `Timer` UDT instance yields no names. *(seen)*
- **Probe** (`udt_field_probe.py`). A `FakeTimed` with `_writes = ("target", "timer")` and
  `self.timer = T5`, plus a rung `T5.Done -> out(Alarm)`:
  - `pdg.writers_of["T5_Done"]` was `None`;
  - `compute_steerable` returned `['Go', 'T5_Done']`;
  - `how(Alarm)` returned STOPPED. It tried and failed, because the instruction rewrites the bit every scan.
  - *(probed)*
  - With a writer that skips some scans, PILOT could REACH by patching an internal timer bit (S1, inferred).
  - The hang-forever survey reads the same predicate ("might never happen") and would mis-report too.
- **Same hole for For/Next.** Per-depth loop index tags must be in `_writes`, as `ForLoopInstruction`'s
  `idx_tag` is today (`instruction/control.py:123`). An index read by `Src[loop.idx]` but not declared
  written becomes a "lever".
- **Fix.**
  - Store Done and Acc as separate Tag fields, as `on_delay` does via `builders._extract_done_acc`
    (`program/builders.py:989`).
  - Debounce needs `on_done/on_acc/off_done/off_acc`; flasher the same; timed `done/acc`.
  - List them all in `_writes`, and EN/TT in `_status_fields`.
  - Gate: a test that every concrete Instruction's `instruction_write_targets()` resolves to names
    (`static_write_target_names` non-empty) for non-indirect fields.

### X3. Cyclefold certifies timed progress only for registered sources (S1/S2, seen)

- **`pilot/cyclefold.py:536-547`.** The ring holds `tags.update(_acc_totals(...))`, so only registered sources
  get `Acc + _frac`.
  - The comment there states the exact failure: without it, "a live timer [becomes] a false sterile cycle".
  - An unregistered debounce, flasher, timed or T&E-drum timer with a coarse unit (or `dt` below one unit)
    shows constant tags. `detect_cycle` then certifies P=1, exact.
- **`cyclefold.py:584-605` (sterile path).** It uses `kernel_budget`, which applies by default when synthesis
  holds are active (`:438`).
  - Then it returns `_finish(False)`: the coast says "unreachable". That feeds the sterile-wait world nogood
    (`world_key.py:153`, `wait_edge_nogood`), and PILOT never waits there again (S2, durable).
  - Otherwise it stamps `scan_id`/`timestamp` forward **without executing**. The unregistered timer's `Acc`
    and `_frac:` don't move, and the landing state never existed (S1 for recorded counterfactual replay).
- **`cyclefold.py:403-417`.** `monotone_allowed` and the quanta come from registered sources only. A ramping
  unregistered acc breaks detection, which is safe but means step-mode.
  - Flasher periods are longer than `max_period=64` scans at 10 ms (500 ms phases give a 100-scan period).
    Cyclefold then never certifies the oscillation, so coasts with a flasher running step scan by scan (S3/S2
    on budget).
- **Fix.** The same as X1, option 1. Once the timers are fold sources, the ring sees `Acc + _frac`. Also raise
  `max_period` from a registered cyclic source's period, as is already done for clocks at `:400`.

### X4. `ote_writes` is the only "level follower" fact; everything else is "retentive" (S2, S1 latent)

`pdg.py:960-961` puts a write into `ote_writes` only when `isinstance(instr, OutInstruction)`. The Out
variants are deliberately **not** subclasses. That is right for crossings and the prover, but PILOT reads
"not in ote_writes" as "held register (latch, copy)". The new classes are neither: debounce and flasher drive
the bit every scan, timed drops it by itself, and toggle holds it but flips on an edge.

| Site | Assumption | Effect for the Out family | Conf. |
|---|---|---|---|
| `pilot/trace.py:1714-1717` | `tag=False` through a writer means negating the guard **only** for OTE | For `Valve=False`/`Beacon=False`/`Horn=False` the trace asks for the rung **true**. That is the wrong polarity for debounce and flasher (rung false gives False), and right-ish only for timed. The result is a wasted trial and a decline. | seen |
| `pilot/trace_routes.py:123, 406, 551` | same negation in route enumeration and route conditions | same; route choice for False targets is inverted | seen |
| `pilot/trace_routes.py:255-268` `writer_route_eligible` | non-OTE means retentive, a "safe default route" | debounce, flasher and timed writers are preferred as default routes although they aren't retentive | seen |
| `pilot/trace.py:2028-2033` `_preserve_children` | a non-OTE establish only needs protecting from **other** writers | timed (drops while the rung stays true) and flasher (self-oscillation) clobber themselves. The preserve walk adds suppression prereqs for other writers and never models the self-drop. Later regression, earned-work erase and restore lead to STOPPED. | seen / inferred |
| `pilot/trace.py:569-580` `_needs_prior_program_stage` | non-OTE write under a guard means "persistent program state" | marks Out-family targets as `data_flow="enable"` stages | seen |
| `pilot/multitarget.py:90-102` `_writes_off` (**CANNOT_REACH proof**) | non-OTE writer whose `forward()` cannot produce `val` is a retentive clobber | Safe **only** while the new crossings return `UNKNOWN` from `forward()`, because `_can_produce(UNKNOWN)` is True. A crossing that returns `Literal(...)` for a variant (for example "timed writes False after T") would make `how(Horn, X)` a provable "mutually exclusive" (S1). | seen |
| `pilot/route_options.py:410`, `pilot/program_facts.py:239`, `analysis/steerable.py:152, 217`, `pilot/availability.py:181`, `pilot/effect_observation.py:551` | OTE as "program authors it" or "combinational" | these sites exclude the Out family. That direction is safe; the variants simply don't get OTE shortcuts. | seen |

- **Fix.** Replace the `tag in ote_writes` polarity test with a crossing query. Ask `crossings.reverse(instr,
  rung, Eq(tag, False))` whether it returns `CondAttr(expected=False)`; only then negate. Register explicit
  crossings for the four classes, with `forward() = UNKNOWN` and **inexact** reverses. Add a regression test
  for `multitarget.analyze` with a timed/flasher writer.

### X5. Edge semantics are invisible (S2, probed)

- **`pilot/program_facts.py:173-196` `compute_edge_tags`.** It collects only `rise`/`fall` atoms in rung
  SP-trees. Instruction-level edge detection is not represented:
  - toggle flips on the rung's OFF->ON;
  - timed restarts on a new edge (O5);
  - this already applies to `oneshot` coils and copies, and to `forloop(oneshot=True)`. *(seen)*
- **`pilot/route_options.py:759-800` `_managed_boolean_rungs`.**
  - When a guard expires, "Boolean input-image lowering returns the input to False" and the next need
    re-asserts it.
  - For a tag in `edge_tags` it adds a one-scan pulse guard. For a toggle's level contact it re-asserts a
    level.
  - So every expire and re-assert cycle toggles the target again (S2: departure, correction churn).
  - *(seen: mechanism; inferred: the toggle consequence)*
- **Satisfied level guard is read as a ready writer.**
  - `trace.py:1564` returns "satisfied" for the guard's level. Candidate building then authorizes a
    `ContinuationRead` of kind `READY_WRITER`/`TRACE_READY` (`candidate_read.py:291-310`), and the seek waits
    for an edge that never comes.
  - **Probe** (`toggle_probe.py`, a `FakeToggle` with a lazily created prev-rung memory key):
    - from cold: `how(Lamp)` REACHED at scan 2;
    - with `Btn` already held and Light off: STOPPED, "still waiting on ... Light=True";
    - with Light on and Btn held: `how(Lamp==False)` STOPPED.
  - A technician would release and re-press. The guidance survey tries only same-scan assignments, so it
    can't find the two-scan release/press either.
- **Fix.**
  - Add an instruction capability (e.g. `EDGE_ON_ENABLE = True`, also true for `_oneshot`) that makes
    `compute_edge_tags` include the rung's condition reads.
  - Give the toggle and timed crossings a reverse with a prior-scan requirement: rung false at N-1 (a
    `Prior` on the rung condition) and true at N. Trace then demands a fresh edge instead of a satisfied
    level.

### X6. `AdvanceProfile`: one accumulator, one monotone done, one owner per channel (S2, seen)

- **`core/instruction/advance.py:58-77`.** `AdvanceProfile` has a single `accumulator` and a single `done`.
  `monotone_profile` (`:213-366`) encodes `done == (acc >= preset)` (or `<`) with one advance demand and one
  restore demand.
- **Consumers that assume one accumulator per owner:**
  - `pilot/steer.py:603-614`: pens = Done bits minus accumulators. A second timer's acc becomes a pen when it
    sits in the watch set, and every fold collapses to step-mode (S3).
  - `pilot/pilot.py:494-499`: journal accumulator names.
  - `pilot/earned_work.py:443-451`: the `profile_acc_names` exclusion; an unlisted second acc can become an
    "ordinal" component.
  - `pilot/corrections.py:996-1060`: the accumulator arm asks `plan(Eq(done, False))` for exactly one reset
    demand.
  - `pilot/trace.py:806-812, 851-852`, `requirement_derivation.py:164-206` ("unique post-advance accumulator
    write").
- **Debounce and flasher don't fit.** They have two timers, the active phase decides which acc moves, and the
  output is not a function of one acc. A flasher has no done: it never completes.
- **`pilot/advance.py:84-104` `build_advance_index`.** If a variant declares its **target** as a channel,
  `_advance_frontier` (`trace.py:743-887`) takes the profile path **before** the writer walk. Other writers of
  the same Y (a `reset(Y)` elsewhere) are then ignored, and the easy route is lost (S2).
- **Fix (per class):**
  - **toggle:** a stage profile (`linear=None`): `plan(Eq(Y, v))` gives `AdvanceStep(until=Eq(Y, v),
    pulse=ConditionDemand(rung))`, or no profile plus the X5 crossing.
  - **debounce:** a stage profile on Y: `until=Eq(Y, v)`, `holds=(rung == v)`, and `progress` = the active
    phase timer's TT, if any.
  - **flasher:** a stage profile on Y: `until=Eq(Y, v)`, `holds=(rung True)`. Never `monotone_profile`.
  - **timed:** `monotone_profile(done=Horn, done_at_boundary=False)` almost fits, but restore needs a rung
    *edge*. Use a custom plan.
  - Do **not** declare the variants' Timer Done bits as `profile.done`. See X10 on earned work.
  - If two timers really need two profiles, extend the contract to `advance_profiles()` (plural), as the
    harness already does (`advance.py:79-80`).

### X7. Flat rungs and loop bodies (S2; probed for the existing behaviour)

- **The PDG flattens loop bodies.** `pdg.py:968-970` walks `ForLoopInstruction.instructions` into the
  **outer** rung node. `condition_reads` come only from the outer `rung._conditions` (`:936-937`) plus
  instruction `_conditions`. Body-rung contacts vanish from `condition_reads`, `sp_tree()` and every guard
  the trace builds. *(seen)*
- **Body writers are found by the PDG but missed by `_instructions` walkers.**
  - `sp_values._writer_for_tag` (`sp_values.py:211-226`) iterates `rung._instructions` only, so a body writer
    becomes `None` and then `UNKNOWN`.
  - The same pattern appears in `trace._owner_call_gate_nodes` (`trace.py:712`), `corrections.py:818` (latch
    detection), `steerable._literal_write`, `tide_tables.py:430, 471`, `static_expressions.py:290-292`,
    `program_facts.py:47-155` and `effect_observation.py:515` (bail on ForLoop).
  - All of these are safe-direction (UNKNOWN or skip) but blind. *(seen)*
- **Body instructions have no enable condition.**
  - `ForLoop.__enter__` captures the body into a `RungLogic` with **no conditions**
    (`program/context/_control_flow.py:148-156`, `_state.py:40-47`). So `_get_combined_condition()`
    (`rung.py:130-142`) returns `None`.
  - Timers and counters in a loop body get `enable_condition=None` / `up_condition=None`. Their
    `advance_profile` then asks for **no** hold (`_trace_demand` returns `[]` for a `None` condition,
    `trace.py:594-596`).
  - **Probe** (`loop_enable2.py`): `count_up(Ctr, 30)` under `Rung(Enable)` reached in 31 scans flat. Inside
    `forloop(3)` it was STOPPED, "still waiting on Ctr_Done". *(probed, pre-existing)*
  - Body rungs must inject the **outer and body** combined condition into body instructions' enable fields,
    or the profile contract breaks the same way.
- **Execution journals.**
  - `requirement_derivation._instruction_runs` (`:279-288`) skips nested `RungRun` items, and
    `effects._instruction_path` (`:353-366`) only descends `InstructionRun | LoopIterationRun`.
  - If body rungs appear as nested `RungRun`s inside a `LoopIterationRun`, their instructions drop out of
    requirement derivation and effect paths.
  - Producer addresses are `(subroutine, rung_index, branch_path)` (`effect_observation.py:497`). There is no
    slot for "body rung k of loop i, iteration j", so iterations alias (same branch_path, N firings per scan).
    `RungFiringTimelines` are per rung per scan, so the last iteration wins. *(seen: types; inferred:
    aliasing)*
- **Fix.**
  - Keep `.instructions` as a flat compatibility view **and** expose an ordered body.
  - Teach `pdg._extract_rung_node` to fold body-rung conditions into a per-write guard. The simplest correct
    first step is to add them to `condition_reads` of the outer node, which over-approximates and is the safe
    direction for PILOT.
  - Give `_writer_for_tag` a loop-aware walk.
  - Decide a journal shape for body-rung runs before PILOT's effect and requirement code sees them.

### X8. Hidden instruction memory (S2, seen)

- **`pilot/world_key.py:131-150` `_pilot_state_key`.** It projects **tags only**, on the prover's
  `stateful_names` (`drive_setup.py:343-372`). Toggle's prev-rung memory, a debounce or flasher phase flag,
  and `_frac:` don't enter the key.
  - Worlds that differ only in memory share Compass transitions, probes and sterile-wait nogoods.
  - The concrete `ReadIdentity.rejection_scope` (`:44-61`) does include memory and scan_id, so act nogoods are
    safe.
- **`pilot/intrascan_research.py:681-690` `_entry_value`.** It reads a hidden memory key's entry value with
  the default `False` only for `_oneshot:`. A lazily created key (toggle's prev) reads `None`, and intrascan
  research mis-reads it, which declines.
- Cyclefold's ring and the fold plateau guard are tags-only (X1, X3).
- **Fix.**
  - Keep variant state in tags where CLICK has a register (TD and the target). Otherwise write the memory key
    **every scan** (TOUCHPOINTS already asks this for the prover's one-scan key discovery).
  - Extend `_entry_value`'s defaults through a registry, not prefix strings.

### X9. Corrections key on class (S2, seen)

- **`pilot/corrections.py:1469-1472`.** The precise-cause "minimal conductive cut" skips every
  `INERT_WHEN_DISABLED=False` writer unless it is `OutInstruction` writing True.
  - Toggle *is* edge-and-inert in output (it retains when the rung is false), so a cut of its guard would be
    a valid correction, but toggle is skipped.
  - Debounce rising is also a valid cut.
  - Prefer a capability ("this writer changes the target only while its rung is true") over the class test.
- **`pilot/corrections.py:770-818`.** The coil/latch-exposure arm uses `isinstance(i, LatchInstruction)` on
  top-level `rung._instructions`. A toggle used as an alarm latch is not seen, and a latch inside a loop body
  is not seen (X7).

### X10. Earned work treats Done reads and edge-written Bools as discrete events (S2, seen)

- `pilot/earned_work.py:244` treats any read of a profile `done` as a "discrete event".
- `pilot/earned_work.py:278-293` `_discrete_relay` treats a Bool as a pulse relay when every writer's rung
  contains an edge.
  - A toggle written under `rise(Btn)` is a **level** that persists. So would a latch, which is pre-existing.
  - If debounce declared its Timer.Done (or Y) as `profile.done`, a `calc(Step + 1)` gated on it would be
    blessed as event-earned while it actually increments every scan the level holds. The result is mis-banked
    progress.
- **Fix.** Relay only through writers that drive the value back every scan (OTE). Keep variant Done bits out
  of `profile.done`.

### X11. The prover context is all-or-nothing and silent (S2, seen)

- `pilot/drive_setup.py:300-385` runs `compile_kernel(program, blockless=True, proof_metadata=True)` and
  `_build_explore_context` on the **whole program**.
- On any exception, or an `Intractable`, it returns an empty `ProverContext()` and logs at `debug`
  (`:383-385`). PILOT then loses:
  - `nd_domains`, so tide-table completeness turns every word into PUNT;
  - `key_config`, so the world key falls back to pivot tags;
  - transition `evidence`.
- New classes without compile or prover arms (all of 3, 4 and 5 until they land), and randint as a new ND
  dimension, degrade **every** `how()` on any program that contains them, even when the target is unrelated.
- **Fix.** Land compile and prover classification together with each feature. Raise this log to `info`. Add
  a PILOT test that asserts `ProverContext.key_config is not None` on a program using each new instruction.

### X12. The CANNOT_REACH proof sites are safe today but fragile

- `multitarget.analyze` (`multitarget.py:184-224`): see X4. It depends on `forward()` staying `UNKNOWN` for the
  variants and on `ote_writes` membership.
- `target_route.linked_feedback_block` (`target_route.py:38-82`) assumes the harness holds `fb` in lockstep
  with `en`.
  - That is already approximate with link delays (`e9af4c4`).
  - A timed or flasher `en` makes short pulses natural, and those can be shorter than a link delay.
  - The proof would then call a reachable target blocked. *(inferred, low confidence)*

---

## 1. Range aggregates (`.max/.min/.median/.mean`)

1. **S1 latent. `analysis/crossings/calc.py:133-134`.** `_raw_forward` does `isinstance(expr, SumExpr)` and
   returns `Aggregate(tags=...)`, whose operation defaults to `"sum"` (`core/crossing.py:70-75`).
   - TOUCHPOINTS suggests keeping `SumExpr` as a subclass or alias of `RangeAggregateExpr`. Either way, every
     MAX/MIN/MEDIAN/MEAN would forward as a **sum**.
   - Consumers that then compute wrong values:
     - `writer_selection._concrete_written_value` (`writer_selection.py:118-124`, via
       `crossing.evaluate_forward`). `evaluate_forward` gates on `"sum"` at `:121-122`, but it would see
       `"sum"`.
     - The writer ranking and codemand-clobber checks that consume it (`writer_selection.py:127+`).
     - `trace._decompose_sum` (`trace.py:1893, 2109-2152`). For example, MAX==0 does not imply every element
       is 0.
   - **Fix.** Make it a sibling class. Emit `Aggregate(operation=kind)`. Keep the `"sum"` gates in
     `evaluate_forward` and at `trace.py:1893`. *(seen)*
2. **`crossings/calc.py:227, 268`.** Reverse on `SumExpr` is a FALLTHROUGH (the "Phase 3 seam"). A new class
   that misses these arms reaches `calc_reverse_edge`, which returns None for anything that isn't a
   `BinaryExpr` or `UnaryExpr` (`reverse_edges.py:64-128`), so it is safe.
   - Do **not** make the aggregate a `BinaryExpr`/`UnaryExpr` subclass. *(seen)*
3. **`_decompose_sum`'s "sum == 0 means every element is 0"** assumes non-negative members. This is
   pre-existing for signed DS sums and becomes visible with signed `.mean()`. Verification catches it (S2).
   *(seen)*
4. **`pilot/evidence.py:269-280`** drops `Aggregate` writers as route sources. Safe. *(seen)*
5. The prover's `_expr_reads_tag` ignores `SumExpr` (TOUCHPOINTS H). Pilot's `stateful_names` and
   `nd_domains` come from that prover pass, so range members read only through an aggregate can be missing
   from the PILOT world key (S2). *(inferred)*

## 2. `randint(lo, hi)`

Most of this comes from a sub-audit; I checked the fold logic at the cited lines.

1. **S1. `core/fold.py:483-519` `_frozen_rung_writes`.**
   - `calc(randint(...), X)` has **no tag reads**, so its rung counts as frozen. X and its downstream cone are
     dropped from the plateau guard.
   - `scan_derived_names` (`fold.py:871-881`) is built only from **condition** atoms, so the existing "disable
     fold" guards (`fold.py` scan-derived path, `cyclefold.py:383`) never fire for a calc body.
   - This already affects `sys.scan_counter` used inside a calc.
   - **Fix.** Give `RandIntExpr` a synthetic resolved-on-read name (for example `sys.__rand`), add it to
     `_SCAN_DERIVED_NAMES`, and compute scan-derived reads over data reads too. *(seen)*
2. **S1. `_do_fold` (`fold.py:1576-1585`) and compiled `step_replay(logical_scans>1)`.**
   - The landing scan executes at `scan_id = N+1` and is then stamped `N+skip`, so its draw is H(N+1), not
     H(N+skip). Draws for the skipped scans never happen.
   - The plateau probe is one empirical comparison, and `randint(0, 1)` repeats with p=1/2.
   - Replay (`runner._replay_seek`/`_replay_slab_fill`, which use `fold=True`) can then rebuild states that
     never existed. PILOT's `cause()`/investigation replays read those states. *(seen, sub-audit)*
3. **S1. Seed as a runner field** would not survive `PLC.fork` (`runner.py:2186-2247` copies fields by hand).
   Every PILOT world is a fork: `overlay.fork_with_pilot_rungs`, `types.snapshot_world/load_world`, skiff,
   guidance `CompiledPLC`.
   - **Prefer a `SystemState.memory` key.** It rides through forks, checkpoints, `rejection_scope` and
     `CompiledPLC`. Reboot must re-seed it.
   - A REACHED witness is seed-bound. Say so in the `Plan` docs. *(seen, sub-audit)*
4. **S2. Abstract world key** (`world_key.py:131-150, 218-248`) has no scan_id and no memory.
   - Abstract-key evidence (continuation observations, probe exhaustion, region-wide nogoods) aliases worlds
     whose future draws differ.
   - A sterile-wait nogood (`wait_edge_nogood`) learned while waiting for an unlucky draw is permanent for
     that key.
   - **Fix.** Treat randint dest tags as uncontrolled ND, kept out of the key, and keep randint-dependent
     evidence on the concrete `rejection_scope`. *(seen)*
5. **S2. `causal/projected.py:345-390`** answers "can this rung write V?" with **one** draw at
   `state.scan_id`. That gives false "no producer" verdicts in projected `cause()`, and PILOT declines
   (sub-audit). Model a randint-sourced write as producible over `[lo, hi]`.
6. **Crash risk. `sp_values._expr_tag_names` (`sp_values.py:380-400`).**
   - It returns names for `(UnaryExpr, MathFuncExpr)`. A `RandIntExpr` that subclasses `MathFuncExpr` would
     let `tide_tables` evaluate it through `_SnapshotView`, which has no scan_id.
   - That raises `AttributeError`, which is not caught (only `TypeError`/`ValueError`/`ZeroDivisionError`).
   - Worse: if `evaluate` silently defaults `scan_id` to 0, `tide_tables.solve_calc_preimage` and
     `guard_verdict` would treat a random calc as an exact deterministic function. That fabricates pins or
     `GUARD_DEAD` (S1).
   - **Fix.** Use a fresh `Expression` class, and have `evaluate` raise without a real scan context.
7. **Do not make `RandIntExpr` a `LiteralExpr`**, even for `randint(k, k)`. `reverse_edges.literal_value_from_value`
   (`reverse_edges.py:37-47`) would then invert `X + randint(..)` as an affine step, giving wrong earned-work
   strides and wrong trace inversions. *(seen)*
8. **Safe** (sub-audit, spot-checked):
   - `rejection_scope` includes scan_id and memory.
   - The guidance survey resets scan_id per preview and confirms against ordinary execution.
   - Intrascan research and skiff run real `_prepare_scan`.
   - PILOT's causal memos are keyed on immutable epoch history.
   - The scan log reproduces scan_id.

## 3. `time_event_drum`

1. **S1. Fold (X1).** `_ensure_registry` is exact-type.
   - An unregistered T&E drum's rung is frozen, so the step, outputs and acc leave the plateau guard. The
     fold then runs one big-dt scan across step boundaries.
   - Drums take `sec/min/hour/day` units, so coarse acc granularity is the norm.
   - If it is registered as a `_StepPreset` source, `None` presets resolve to `None` (`fold.py:1103-1111`).
     That steps one scan when Done is read, and leaves the source unbounded when it is not
     (`fold.py:1234-1247`). Bounding then relies on the event tag being visible; it is, because events are
     condition reads. Acceptable once D1 (does the acc keep counting?) is answered. *(seen)*
2. **S2. Advance profile.**
   - Copying `TimeDrumInstruction.advance_profile` (`drums.py:586-657`): `int(resolve(None))` hits a TypeError,
     so the plan returns `None` for acc targets on event-only steps (decline).
   - Copying `EventDrumInstruction`'s (`drums.py:394-448`): `pulse=ConditionDemand(event)` is **wrong for a
     level event**.
     - `_mark_pulse` (`trace.py:562-566`) makes the event input oscillate.
     - The advance then happens only if the oscillation is high on the scan the time elapses, which misses
       or delays it.
     - And a level event held across consecutive event-only steps with the same tag advances one step per
       scan, overshooting the `current_step >= current + 1` boundary.
   - **Fix.** Plan per step:
     - preset `None`: `holds=(auto, event True)`;
     - event `None`: `holds=(auto,)` with `reports_progress`;
     - both: `holds=(auto, event True)` with progress.
     - `until` is the next step.
   - *(seen: sibling plans; inferred: consequence)*
3. **Do not subclass `TimeDrumInstruction`.** The fold registry uses exact type, so a subclass is merely
   unregistered (X1). But `crossing_for` walks the MRO and would inherit `_DRUM`. That is harmless, since
   `DrumCrossing` falls through (`crossings/drums.py:27-38`). The coverage test forces an explicit
   `register` anyway.
4. **Prover absorb** (`__drum_preset`; TOUCHPOINTS H) feeds PILOT's `key_config` and nd_domains (X11). If
   absorb mis-models a T&E drum, the world key masks the wrong acc positions (`acc_indices`,
   `drive_setup.py:349-354`). *(inferred)*

## 4. Out family

### Common to all four

- **X2.** Store each Timer as separate Done and Acc `Tag` fields in `_writes`, or they become steerable.
  *(probed)*
- **X1/X3.** Debounce, flasher and timed read `_dt`. Register them as fold sources (flasher as a cyclic
  source), or the fold runs over them. *(probed for the flasher stand-in)*
- **X4.** They are not in `ote_writes`. That is correct for the prover, but PILOT's False polarity, default
  routes and preserve walk then treat them as latches.
- **X6.** One profile per instruction, one acc, monotone done. Use stage profiles on Y instead.
- **Crossings MRO** (TOUCHPOINTS addendum): don't subclass `OutInstruction`.
  - `OutCrossing` (`crossings/boolean.py:64-75`) would claim an **exact** `CondAttr(expected=value)`.
  - `effect_observation._certified_out_producer` (`effect_observation.py:529`) would certify "coil == rung"
    timelines and prune projection scans on that basis (S1).
  - `sp_values.writer_value_facts` (`sp_values.py:294-301`) would mark the bit a combinational
    `is_ote_bit` alias.
  - A non-subclass with an explicit crossing avoids all three.
- **Corrections** skip all four as precise-cause writers (X9).

### toggle (a stepper-like Bool)

- **S2, probed.** A satisfied level guard means a continuation that never toggles (X5). Re-asserting an
  expired hold toggles again (X5, `_managed_boolean_rungs`).
- **S2.** `earned_work._discrete_relay` blesses toggle-written Bools under `rise()` as pulses (X10).
- **S2.** The prev-rung memory key: write it every scan, and add `_entry_value` default `False` (X8). O1 (first
  scan with the rung already true) also decides what a PILOT fork from a mid-run snapshot does. A fork keeps
  memory, so a fork is fine; a reboot is not.
- **Folding is fine.** Toggle does not read `_dt`, and with constant reads in a plateau it doesn't change.
  Under an oscillating PILOT hold (`route_options._oscillating_rungs`, `:740-755`), Light has period 4 and
  `detect_cycle` handles it as boundary-stable. *(inferred)*

### debounce (delayed follow both ways)

- **S2.** The trace polarity for `Valve=False` is inverted (X4).
  - Without a profile, a held input does not produce `Valve` on the verifying scan. The effect is observed
    `ABSENT` (`effect_observation.observe_expectation`, `:37-75`), a failed-effect requirement follows, then
    theory repair. Expect STOPPED.
  - Needs the stage profile from X6: `holds=(input == v)`, `until=Eq(Valve, v)`.
- **S2.** PILOT's edge-driver holds (`trace_tree.pulse`, `_oscillating_rungs`) on an input feeding a debounce
  re-arm the debounce every scan, so it never fires.
  - Only relevant if a debounced bit drives a counter, but that is a natural CLICK pattern. The profile should
    declare the bounce reset (O3) so the counter route does not choose oscillation upstream of a debounce.
    *(inferred)*
- **S2.** RESEARCH's per-phase `.Done` meaning: the on-side Done stays True while steady. Do not export it as
  `profile.done` (X10).

### flasher (a self-oscillating output)

- **S1, probed (stand-in).** Fold runs over it (X1).
- **S3/S2.** Cyclefold can't certify a period over 64 scans (X3), so every coast with a flasher running steps,
  and long dwells exhaust `max_scans`.
- **S2.** There is no done and nothing is monotone, so `monotone_profile` is unusable. Use a stage profile:
  hold the rung true until `Beacon == v`. `how(Beacon == False)` is then usually "wait one phase".
- **S2.** If `Beacon` lands in the trace's watch set, `steer._pen_tags` (`steer.py:593`) + `CoastSession.note_pens` (`coast.py:286-305`)
  records a transition every phase. Bearing-departure classification sees churn and spawns corrections.
  *(inferred)*

### timed (drops while the rung stays true)

- **S2, probed (by analogy with toggle).** Once timed out with the rung still true, `how(Horn)` reads the
  guard as satisfied and continues forever (X5). It needs a fresh edge (O5).
- **S2.** The preserve walk (`trace.py:2028`) and earned work assume an established non-OTE value persists.
  Timed drops it by itself, so multi-target `how(Horn, X)` regresses, restores, and STOPs.
  - This is honest only if PILOT can't prove reachability.
  - With `maintained=True` the semantics flip ("at least T, then follow"). The crossing and profile must
    branch on `maintained`.
- **S1 latent.** A `forward()` that returns `Literal(False)` (tempting: "after T it writes False") enables the
  `multitarget._writes_off` clobber proof (X4). Keep `forward() = UNKNOWN`.

## 5. For/Next: body rungs, `break_loop`/`continue_loop`, nesting

- **X7** is the core issue: flattening, missing body-rung conditions, `None` enable conditions (probed,
  pre-existing), and journal addressing.
- **`return_early` has a guard model; break and continue don't.** `trace.py` adds `return_early_guard_exprs`
  as writer prerequisites (`trace.py:1788-1800`, `analysis/return_guards.py`), and `steerable._rung_unconditional`
  consults it.
  - Break and continue need the same "no earlier break fired in this iteration" prerequisite. Without it the
    trace claims a writer is reachable when an earlier break always fires, so it is verified and then declined
    (S2).
  - `tide_tables` `GUARD_DEAD` stays sound: extra conjuncts only make the outer guard harder to satisfy.
    *(seen: return_early path; inferred: break)*
- **Index tags.**
  - Nested loops need distinct per-depth index tags, **declared in `_writes`** (X2), or they become
    steerable.
  - `_forloop_idx` is shared across all loops today. Its scan-end snapshot value is the last iteration's, so
    `trace._trace_back`'s satisfied check (`trace.py:1564`) can call a mid-scan index requirement satisfied.
    This is pre-existing and gets worse with nesting.
  - `tide_tables.invert_indirect_copy` only walks top-level `_instructions`, so it doesn't reach body copies.
    That is safe and blind. *(inferred)*
- **Per-iteration one-shots (F14).**
  - PILOT matches one-shot memory by `instruction.memory_key("_oneshot")` (`intrascan_research.py:555-556`,
    `requirement_derivation.py:419-420`). Per-iteration keys would not match, so the explanation is UNKNOWN
    and PILOT declines.
  - If F14 says "once per scan", the existing key is fine.
- **Counters inside a loop count N times per scan (F8).**
  - `CountUpInstruction.advance_profile` uses `rate_per_scan=lambda _dt: 1.0` (`counters.py:122-140`), so
    `estimate_scans` is off by a factor of N. That affects estimates only; fold measures the delta.
  - Note that today the up condition is `None` inside a loop anyway (X7).
- **Body-rung condition views (F13).** `RungRun.view` is one `ConditionView` per rung (`executor.py:149-160`).
  PILOT's guard evaluation and intrascan research read guard truth from it. Per-iteration body-rung views need
  their own run records, or PILOT evaluates body guards against the outer rung's view. *(inferred)*

## Tests and gates

From a sub-audit; file and line references are in it.

- **Only one test fails on its own:** `tests/core/analysis/test_crossings_coverage.py:115-146`.
  - It walks `Instruction.__subclasses__()` and requires exact set equality with `EXPECTED_WITH_CROSSINGS |
    EXEMPT`, plus `registered_classes() == EXPECTED_WITH_CROSSINGS`.
  - It **does** force an explicit `register()` even for an MRO-inherited crossing, because
    `registered_classes()` reads `_REGISTRY`, not `crossing_for`. It never checks *what* `crossing_for`
    returns.
  - **Blind spot:** classes in modules that `test_crossings_coverage` does not import are missed. It imports
    `pyrung.core.instruction`, `send_receive._core`, `click.nop` and `click.raw`. A new `out_variants.py` must
    be imported from `pyrung/core/instruction/__init__.py`.
- **Hand-kept lists that pass silently:**
  - `tests/core/test_advance_profile.py`: one test per owner, no registry.
  - `tests/core/analysis/test_pilot_advance.py:101-147`: drum/shift replays.
  - `tests/core/analysis/test_pilot_examples.py:95-106`: six "capability family" smoke cases.
  - `test_instruction_analysis_coverage.py` `PUBLIC_RUNG_INSTRUCTIONS`, which does not touch PILOT.
- **Tumbler** is a fixed Click-CSV program that uses only existing verbs, with no generator. Goldens change
  only if PILOT's behaviour on existing instructions changes.
- **Fuzz** never exercises PILOT. `strategies.emit_instruction` silently emits nothing for an unknown kind.
- **Recommended new PILOT-side gates:**
  1. Every concrete Instruction's declared writes resolve to concrete names (catches X2).
  2. Every class that reads `_dt` is in the fold registry, or explicitly opted out (X1).
  3. Fold-vs-unfold parity for each new instruction, as in the probe in X1 (X1/X3).
  4. For every class, `advance_profile().channels` is a subset of its declared writes (X6).
  5. `ProverContext.key_config is not None` for a program containing each new instruction (X11).
  6. Extend `test_pilot_examples.py` families:
     - toggle with the button already held (X5);
     - debounce both polarities;
     - flasher `how(Beacon == False)`;
     - timed after timeout;
     - T&E event-only step;
     - counter inside `forloop` (currently STOPPED, X7);
     - body-rung writer.

## Probes run (session scratchpad, not in the repo)

- `fold_flasher.py`: unregistered dt-reading flasher stand-in. `run_until` gave folded `Flashes=2` vs
  unfolded `6`, with one `_do_fold(497)`. `how(Tmr.Done)` gave `Flashes=6` and made no fold call.
- `udt_field_probe.py`: Timer UDT stored as one field. `T5_Done` has no writers and is classed steerable.
  `how(Alarm)` STOPPED.
- `toggle_probe.py`: toggle stand-in. Cold start REACHED; with the button already held, STOPPED both ways.
- `loop_enable2.py`: `count_up` flat REACHED at scan 31; inside `forloop(3)` STOPPED. The up condition is
  `None` in the loop.
