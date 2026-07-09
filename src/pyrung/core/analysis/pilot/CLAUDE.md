# pilot/ — a harbor pilot for PLC programs

The captain (the user) picks the destination. The ship (the PLC program) has its own
mass, inertia, and habits. PILOT comes aboard, reads the charts, and works the passage.

Under the metaphor it's WWAED — *what would an engineer do*. Everything here is what a
real PLC tech does at a fault: trace backward from the symptom, force a bit, watch what
the program does with it, pull up the trends when something moves that shouldn't have.
The pilot is that engineer with three superpowers: **perfect memory** (the scan log),
**perfect understanding — in theory** (static analysis reads the whole ladder at once;
*in theory*, because live masks humble it exactly the way they humble the human), and **free forks**.

## Theory

You share the helm with a program you can't fully read. Same registers, same scan, no
locks. The scan is atomic and you're on one side of it: set inputs, the ship moves, look
at what happened. The same command means different things in different waters (Reset
from STOPPED ≠ Reset from EXECUTE), and your actions ripple through rungs you haven't
traced. Everything below falls out of those facts.

- **Sail by bearing, not by route.** Stored plans go stale the moment the state changes.
  Knowledge is a direction you re-check every iteration (the compass), never a script.
  When the loop wanders — oscillating, stuck on a plateau — the fix is *consult the
  compass*, not another acceptance heuristic.
- **Knowledge is observed, never invented.** A reader that can't resolve something says
  UNKNOWN. And nothing is believed until the verify pipeline watches who moved what —
  after a scan a register changed, and you can't tell by looking whether it was you or
  the ship.
- **Guess freely, reject only on proof.** Every proposed action is replay-verified
  before it's believed, so a guess can't hurt. A *no* — unreachable, DEAD, route
  pruned — is never re-checked by anything downstream, so a no requires a provably
  complete domain (Bool, prover `nd_domains`, declared `choices=`). Can't prove it?
  Punt, and let the next instrument have a go.
- **Every stall is pointable.** The acceptance bar: if the pilot is stuck you should be
  able to dump the state, name the tag it's stuck on, and say what kind of stuck. A
  mechanism that can stall somewhere you can't point at doesn't ship — and the exits
  that still miss this bar are listed honestly in "How we fail".

## The happy path

Trace the tree. Find the blocking leaf. Pulse the lever. The program advances. Distance
drops. Repeat until zero. The compass learns edges as you go. No holds, no skiff, no
investigation.

Concretely, one iteration of the loop (`pilot.py`):

```
ORIENT   — trace the target backward to a tree of steerable prerequisites
           (trace.py). Distance = the tree's unsatisfied-leaf count. Rank
           this iteration's candidates (candidates.py).
ACT      — press the top candidate: command pulse, prescribed batch, or
           zoom through a timer/counter dwell (steer.py).
VERIFY   — inside every attempt: who moved what? (verify.py → outcome.py)
           1. I moved it where I wanted.   → confirmed
           2. I moved it wrong.            → bad edge; correct the compass
           3. The ship moved it wrong.     → my command was a no-op
           4. Nothing happened.            → unmet prerequisite; trace why
RECORD   — the sole compass write path: instruments return
           CompassObservation values, the loop applies them (Compass.apply)
           unconditionally, before ASSESS can revert the world. Always
           bearings, never plan steps.
ASSESS   — trend + checkpoint + revert (progress.py). Improved → checkpoint;
           plateau → escalate a reading tier; sustained decline → revert.
```

Route choice happens once, *before* the loop (`_prepare_route`, pilot.py): a concrete
value target gets a deterministic default route, redirectable with `avoid=` / `via=`; a
relational target (`State > 5`) drives without one.

**Scheduling is triggers, not positions.** The phase names say what *kind* of work
something is, not when it runs — VERIFY→RECORD→ASSESS run per-trial inside ACT's
candidate loop, and the skiff fires only at the two stuck exits. An escalation's loop
position *is* its trigger condition ("no reading left" is only knowable there); don't
tidy the ladder into a sequence. The coherence test for loop changes is **does every
decision have exactly one owner?** Route: `_prepare_route`. Writer: `_rank_writers`.
Candidate order: `_build_candidates`. Escalations: their trigger sites. Knowledge
commit: `Compass.apply`.

## Read before you run

Three instruments, all answering *"I need (tag = value); what must I do?"* — differing
only in how much of the path is readable. Escalate only when reading isn't enough:

1. **trace** (trace.py) — read the ladder backward, run nothing. Transparent walk of
   writer conditions / copy / calc down to steerable inputs; establish + preserve for
   retentive targets; value navigation over declared-constant tables (charts.py,
   tide_tables.py) for the opaque-but-constant stuff — a `stateMask & disabledMask`
   gate *looks* runtime-computed but is constant-table-backed, so the tide tables read
   it. Punts the moment a genuinely live word gates enablement.
2. **let-run / zoom** (steer.py) — sometimes you just watch it run. When the bearing
   points at a self-advancing frontier (a timer or step counter completing on its own
   under the held state), hold heading and let scans pass, with an ejection guard for
   surprises.
3. **skiff** (skiff.py) — bench test. When a guard is genuinely unreadable, fork, pin
   everything outside the frontier's upstream cone, try one unprobed lever (singles,
   then pairs), step, observe. A control run first proves the frontier is stuck without
   you. Results feed the compass as observations — a learned edge is a bearing, never a
   plan step.

## What breaks the happy path, and what handles each break

Each is a named response to a named failure — not a peer system.

| The break | The response |
|---|---|
| The trace picks a rung that can't fire from where the machine actually is — it looks shortest on paper, but its conditions belong to a different state | Prefer the rung whose conditions are already mostly true *right now* (`_rank_writers`, trace.py + availability.py). Re-orders what to try first; never throws anything away. |
| The "easiest" way to get the value is the init/reset rung — like fixing a fault by power-cycling the machine. Works, wrecks everything else | Rank it last; prefer the rung that advances the sequence normally. The reset stays available as a last resort (trace.py). |
| A momentary pushbutton gets latched on — but the program clears it every scan; it was built to be pressed and released | Spot the press-and-release idiom (`compute_clear_only`): pulse it, never hold it. |
| Firing a rung to satisfy one condition stomps a value another condition needs (it copies 2 into the command word while a sibling needs Cmd == 5) | Among otherwise-tied rungs, sink the one that provably stomps a sibling's need (the clobber tie-break, trace.py). Can't prove the stomp? Don't punish it. |
| A rung is gated by a value computed at runtime — indirect address, live mask. You can stare at the ladder all day and not know what turns it on | Bench test it: the skiff, bounded probes, learned edges only. The static reader (`guard_verdict`) punts first; the skiff is its escalation. |
| The gate compares against a word nobody declared a range for — there's no honest list of values to try | If it's an equals/mask gate: stop, *name the word*, and ask for a `choices=` declaration. If it's a greater/less-than: try the exact boundary value and report it as an example — the relation is the requirement, not the number. |
| The thing you're waiting on is a timer or counter that finishes by itself | Don't push — hold everything steady and let it run (zoom / let-run, steer.py). |
| To keep the machine alive you're wiggling something every scan (petting a watchdog, faking an encoder) — so fast-forward can't fast-forward: "something changed every scan" | cyclefold: jump the slow ramp ahead, then run one real period of the wiggle at normal speed. Fails closed — step, never mis-fold. |
| Something moved and you didn't touch it. The program has its own ideas — an alarm reset it, a watchdog tripped | Investigate like a tech with a trend screen: one incident, competing explanations, and only the first one that survives a replay gets a force installed — *alone*, never as a bundle (investigate.py, corrections.py). |
| The force you installed to get here is now the thing blocking the next step | Don't install a force that blocks what the plan still needs (`hold_defeats_needed`); on rollback, drop it instead of faithfully re-installing it. |
| The captain said "get there without touching X" | Three gates: never plan a route through it, never press it (checked *before* the pulse), never even let it blip mid-travel (every in-between scan is checked). `avoid=(A, B)` avoids either; `And(A, B)` only the pair together. The decline names a member it actually caught in the way (witness-based — see "How we fail"). |
| The state register is loaded by a jump-table copy — the backward trace hits it and goes blind | Compass bridge (causal.py, opt-in): check what earlier runs *recorded* actually firing there — never guess an unconfirmed hop — and pick the trace up on the far side. |
| A tag that looks like an operator lever is actually written by the program — force it and you'll be overwritten next scan | Empirical veto (causal.py): if the recording shows the program wrote it when you weren't touching it, stop treating it as yours and trace through it instead. Evidence only ever removes levers, never invents them. |
| The machine is mid-sequence, just waiting on one acknowledge button — and the backward trace can't see that | currents (currents.py): read the command/transition structure and find the one legal button for this state. If it's not unique, offer nothing. |
| Reaching goal B undoes goal A | Multi-goal pre-pass (multitarget.py): prove what can't coexist, do the clobberer first, then drive each goal alone. The final all-goals-at-once check is the honest referee. |

## How we fail

The bar: every spinning mode drains a named finite budget, and every stop points at a
named leaf. Here is where that honestly stands today (audited 2026-07-09).

**Where we are.** Every spin mode is finitely bounded, but not all by a *named* budget:

- The skiff is the model citizen: `_SKIFF_KEY_BUDGET` laps per stuck key, a lap only
  counts if the compass actually changed, a decline requires a control run first (the
  frontier really is stuck without you), and the stuck exit reverts to the last
  checkpoint and reports honestly.
- The work budget (`max_scans`, default 3000) counts *committed* scans — and it rewinds
  on revert, so it bounds forward progress, not revert churn.
- Revert cycles and zoom ejection have **no counter of their own**; they terminate
  because knowledge accumulates (nogoods, installed holds), not because a budget drains.
  Zoom ejection is the thinnest ice — no per-key guard, and it misses let-run's
  ejection-investigation special-case.

On a failed single-target `how()`, the `Plan` always carries `reason`, plus whatever
breadcrumbs apply: `skiff_decline` (names the frontier tag and the free word that needs
`choices=` — a caption from the first such frontier, not a proven unique culprit),
`avoid_names` (only when proven; witness-based for a union), `lever_notes` (relational
reports with their example values), and the full `journey` / `hold_log`.

**Where we want to go** (delete each as it lands; see `scratchpad/burner/handoff.md`):

1. `"budget exhausted"` names nothing and doesn't revert — the max-scans terminal
   (pilot.py) gets no frame, so it can't name the outstanding frontier. Route it
   through the frame and revert to the checkpoint like the stuck exit does.
2. `pilot_drive` discards `loop_reason` — a live failure with no harness link returns
   `reason=None`, the one bare False left in the module.
3. Multi-target failure `Plan`s carry only `reason` — thread `journey` / `hold_log` /
   `lever_notes` through like the single-target path does.
4. `"stuck: trace_opaque"` names a *category*, not a tag — carry the frontier pair into
   the reason string so the Plan is pointable without reading the event stream.
5. Reverts and zoom ejection are knowledge-bounded, not budget-bounded — add per-key
   counters (and route zoom's ejection through the same investigation special-case as
   let-run), or keep this doc saying exactly what it says now.
6. Declines are witness-based, and witnesses are lossy — a union `avoid=(A, B)` decline
   names whichever arm the terminal frame saw; the answers are sound, the explanations
   order-dependent. Aggregate over `journey` / `hold_log` (they already survive onto
   the `Plan`) instead of reading the last frame.

## The reefs (charted; don't sail into them)

- **"What's still needed" is three different questions**, asked at three stations:
  `frontier_pairs` (whole chosen tree, after selection), `_writer_projection` (is this
  candidate a dead branch? — projected fire-time overlay), `_expr_availability` (how far
  from firing? — live snapshot). They're composed, not duplicated — #2's counterfactual
  verdict is an *input* to #3 — and `test_pilot_needed_vocabulary.py` pins the
  relationships. Don't merge them.
- **Rejection soundness lives at the call sites.** `tide_tables.py` has softer
  plausible-value fallbacks inside; they're unreachable from the rejection arm only
  because `_writer_guard_verdict` (trace.py) pre-screens every free tag to a complete
  domain before `guard_verdict` may enumerate. A new caller that skips the pre-gate
  fabricates proofs.
- **The compass's persistence is semantics, not a perf problem.** Tables are tiny and
  off the hot path; the `pyrsistent` value semantics mean knowledge never mutates under
  a holder. Don't "optimize" it back to shared dicts.
- **Writers that could produce the value are never suppressed** (`_can_produce` in the
  preserve walk), and availability **orders, never rejects** — prescribed edges keep top
  priority, no candidate is dropped.

## The shipyard rule

No instrument gets wired without a hand-driveable, honestly-failing gate program first,
born strict-xfail, flipped when the mechanism lands. The standing gates:

- **burner Starting→Execute** — trace + let-run end to end. If a change makes the skiff
  look needed here, the bug is in trace's writer selection.
- `test_pilot_sandbox_gate.py` — the skiff pair (command-selected mask passing;
  free-word declining by name until `choices=` is declared).
- `test_pilot_avoid_gates.py` — one program per avoid gate, plus the union semantics.
- `test_pilot_free_word_lever.py` — the fill shape (relational lever on an undeclared
  Real).
- `test_pilot_self_defeating.py` — checkpoint-frontier feed for hold self-defeat.
- the command-detour pair in `test_pilot.py` and `test_pilot_table_detour.py` — currents
  and the transparent-machine refinements.
- `test_pilot_compass_bridge.py`, `test_pilot_empirical_veto.py`,
  `test_pilot_needed_vocabulary.py` — bridge, veto, vocabulary pins.

The full burner drive (`how(y_BurnerLoop)` from cold, reached ~scan 2011) is the live
check — machine-local (`scratchpad/burner/repro_regression.py`), not CI.

## Module map — who owns which decision

- `pilot.py` — the conductor: the loop, both stuck exits, route choice
  (`_prepare_route`, one-shot pre-loop), terminal honesty (reason assembly).
- `trace.py` — writer choice (`_rank_writers`), the backward walk, route enumeration,
  steerability (`compute_steerable`, `compute_clear_only`), `frontier_pairs`.
- `availability.py` — how far is this writer from firing? (4-valued verdict; read-side,
  imports lower layers only, never trace.py).
- `candidates.py` — what do we try this iteration, in what order
  (`_build_candidates`)? Each candidate records its own rank rationale into the events.
- `steer.py` — press it: pulses, batches, zoom; try-verify wrappers that *return*
  observations (Act never writes the compass).
- `verify.py` — accept or reject a trial (SPIN / CYCLE / DEAD-END gates + the avoid
  scan gate).
- `outcome.py` — who moved what; sole assigner of `Outcome.CONFIRMED` (and sole minter
  of CONFIRMED compass provenance — a guarded path, currently unexercised).
- `compass.py` — the notepad: learned transitions, one entry per
  `(tag, from_val, cause)`, provenance as lifecycle; `Compass.apply` is the loop's only
  write path and returns `(compass, changed)` so a round that taught nothing can't buy
  another lap.
- `charts.py` — static value graphs (`CompassGraph`) + opaque-pipeline detection.
- `tide_tables.py` — constant-table predicate solvers (`guard_verdict`,
  `solve_calc_preimage`).
- `evidence.py` — static route/role expansion that trace reads.
- `currents.py` — the one button a program-owned current is waiting for.
- `skiff.py` — isolated fork-pin-step experiments (`probe_live_guard_frontiers`).
- `progress.py` — trend, checkpoints (with frontier capture), reverts.
- `investigate.py` — what just went wrong, and which single hold explains it.
- `corrections.py` — the corrective-hold vocabulary (FLIP / FREEZE / OSCILLATE),
  dispatched by instruction class + profile, never by name.
- `accumulators.py` — which held input really drives a timer/counter, and how many
  scans to eject (analytic, then empirical).
- `cyclefold.py` — fold soaks without breaking the oscillation that sustains them.
- `multitarget.py` — the `how(A, B, …)` pre-pass.
- `causal.py` — cause-chain walkers shared by gates, outcomes, and investigation; home
  of the compass bridge and the empirical steerable veto.
- `physical.py` — harness/feedback install on forks.
- `types.py` — shared types; the World/Knowledge split (`_World` is the revertible
  half); the `WalkContext` seam.
- `_ops.py` — PLC primitives: holds (`ConditionalHold`), pulses, delayed-effect
  settlement, `_coast_holding_state`.

**Where new read-side capabilities live:** a static reader — reads the charts, never
runs the ship — names `WalkContext` (types.py: `snapshot` / `pdg` / `program` /
`steerable` / `opaque_loop` / `prior`) in its signature, lives in its own module
importing only lower layers, and trace.py imports *it*, never the reverse.
availability.py was born inside trace.py and had to be carved out; a capability born on
the seam never needs extraction.

## Vocabulary

- **hold** — a `ConditionalHold`: drives its tag *while* a guard holds (vs pinning it).
- **edge** — a compass edge is a learned transition; a rise/fall edge is a tag read
  through `rise()` / `fall()`.
- **pin** — a fire-time pin: the source value a writer forces the scan it fires.
- **clear-only** — an ack-cleared momentary command: the program only ever resets it, so
  the operator supplies the active value. Pulse-and-release; never a hold.
- **frontier** — the tree's outstanding non-steerable `(tag, value)` needs
  (`frontier_pairs`, BFS-ordered); distinct from the single frontier *tag* a stall dump
  points at.
- **cone** — the upstream cone is a tag *region*; cone settlement is the *operation* of
  coasting over it.
- **widening** — two unrelated uses: `_try_widening` (steer.py) grows the candidate
  set; "Or-widens / And-narrows" (trace.py) is boolean-domain math.

## Future direction

See `scratchpad/burner/handoff.md`. The graceful-failure gaps live in "How we fail —
where we want to go" above; delete each as it lands.
