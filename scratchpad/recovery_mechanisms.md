# how() Recovery Mechanisms — Pluggable & Composable

Each mechanism is independent. Any subset can be active. They compose by chaining: earlier mechanisms narrow the problem; later ones handle what's left.

**Why pluggable.** Make recover-from-cause a pluggable interface from the start. We are not committing to one recovery strategy — the point is to try, compose, and measure combinations across a library of real programs and let the data decide. So each mechanism below is a unit you can switch on or off independently, and the ordering between them (which to try first) is itself a tunable, not a fixed pipeline.

**Two scopes.** Most mechanisms below operate on a *single corridor* — one walk, one divergence, one repair. But solving a real program means co-advancing *several* coupled corridors that must converge (arrive at compatible phases together). A class of failure lives only at that scope: every corridor individually solved, none ever diverges, yet they can't be made jointly satisfiable at their synchronization points within the deadlines. The single-corridor mechanisms are blind to it. The **Factoring** and **Convergence** sections handle that scope; everything in between is per-corridor and runs inside each subsystem.

## Factoring (decompose before walking)

- **Read the synchronization structure.** Factor the program into weakly-coupled subsystems via the narrow cuts in the dependency graph. Each narrow interface (handshake, shared interlock) is a producer-consumer edge: subsystem A reaching phase P produces signal S that subsystem B consumes to advance. Collect these into a partial order over all phase transitions. This is read, not constructed — it's already in the graph.
  *Prior art: star-topology decoupled search (Gnad-Hoffmann); causal-graph factoring.*

- **Linearize the partial order.** If the synchronization structure is a DAG, topologically sort the subsystems and solve in producer-consumer order — each corridor's outputs feed the next. The common case. Cyclic synchronization (mutual handshake) is the residue; see Convergence.

## Forward (producing the trace)

- **Corridor walk.** Step forward on the runner along the waypoint sequence. The base. Everything else attaches to this.
  *Prior art: directed model checking (Edelkamp, Lluch-Lafuente, Leue).*

- **Steer via `effect()` projected.** At each decision point, fork-and-test each candidate input; pick the one that advances the governing tag toward the next waypoint. Counterfactual but-for test, run by simulation, no solver.
  *Prior art: Halpern-Pearl but-for causality; concolic execution's branch selection.*

- **Helpful-steer ordering.** Before trialing all inputs, read `simplified()` of the next waypoint's enabling condition. Inputs in the formula go first; the rest are deferred. Reduces forks per node from |cone| to |relevant inputs|.
  *Prior art: FF helpful actions (Hoffmann-Nebel), applied via exact structure instead of delete-relaxation.*

- **Time jump at crossings.** Advance the clock to the next accumulator threshold any rung reads (from the event/crossing schedule), settle one scan, repeat. No tick-by-tick stepping. Inputs held ⇒ no branching.
  *Prior art: hidden-event acceleration / timed-automata event-driven simulation.*

- **Link-aware de-energization.** When the plan needs a feedback false, follow the `link=` to its enable and de-energize the cause. Use `Physical.on_delay` as the crossing delay. Force directly only for unlinked/declared-external tags.
  *Domain-specific. No direct prior art.*

## Divergence detection (knowing you're off-corridor)

- **Value-graph distance.** Backward BFS over the governing tag's value-transition graph gives exact hop-distance to the goal at each value. Distance went up ⇒ divergence. Tiny, precomputed, exact.
  *Prior art: pattern databases in directed model checking (Edelkamp).*

- **Must-stay violation.** After reaching a waypoint marked "hold," any change to the governing tag is a divergence. Same mechanism as progress failure — expected value vs actual value.

## Diagnosis (understanding the divergence)

- **Trigger/enabler split via `cause()`.** On divergence, replay from the ScanLog at full fidelity. Trigger = what transitioned (the deadline). Enabler = what was already wrong (the dropped ball). Read off the SP-tree, no solver.
  *Prior art: causality checking (Leitner-Fischer, Leue); Halpern-Pearl actual causality; "Explaining Counterexamples Using Causality" (Beer, Ben-David, Chockler). They formalized this. They don't feed it back into a planner — that's the novel part.*

- **Effect()-confirmed minimal cause.** When `cause()` returns multiple enablers, fork-and-test each with `effect()` to find which is load-bearing. Fix that one. The minimal set is the Diagnosis on failure.
  *Prior art: Halpern-Pearl AC3 minimality condition; polynomial approximation (Beer et al.).*

- **Deadline extraction.** When the trigger is a timer Done bit, annotate with the timer's preset. Gives the planner the deadline number: "establish the enabler within N scans."
  *Needed. Not yet built. Connects to timed-automata fault ascription (Leitner-Fischer, Leue).*

## Recovery (acting on the diagnosis)

- **Alternatives-stack.** If the goal's `simplified()` is `Or(term_A, term_B, ...)`, try the next term before any repair. Cheapest possible recovery — just pick a different rail path. No learning, no rewind.

- **Precondition accumulation.** Add the diagnosed enabler (and its required value) to the monotonic precondition set. The set only grows. Guarantees no-oscillation and termination.
  *Prior art: no-good learning (Steinmetz-Hoffmann conflict-driven state-space search).*

- **Backjump to cause origin.** The enabler's `held_since` / provenance names the waypoint where the blocking commitment was made. Rewind to that checkpoint, not one step back. Re-fork and re-walk with the updated set.
  *Prior art: conflict-directed backjumping (CDCL / CSP); Steinmetz-Hoffmann for planning.*

- **Constructive regression.** Recurse through `simplified()` to find how to establish the missing enabler, bottoming out at inputs. This is the "constructive reachability mode" — stops at inputs, not at first-unobserved-tag.
  *Prior art: System-R regression planner (Bonet-Geffner). They interleave regression with forward progression — same loop.*

- **Inverse regression.** The "make false" variant. Separate code path: de-energize a seal-in latch, break a hold, satisfy a reset rung. Distinct leaves from the "make true" regression.

- **Fault-scenario override (`unlink=`).** Caller declares specific feedbacks as broken. Walker forces them directly, bypassing the link. The plan is the commissioning workaround for that fault.
  *Domain-specific.*

## Convergence (multi-corridor — acting when corridors won't align)

These run *above* the per-corridor mechanisms. Reach for them only when each corridor is individually solvable but they can't be made jointly satisfiable.

- **Convergence diagnosis.** Not "enabler X was false" but "corridor A reaches phase P in 40 scans; corridor B's deadline to consume it is 30 — the producer is too slow for the consumer." A scheduling diagnosis about the *relative timing* of two subsystems' phases, not one subsystem's blocked guard. Built from per-corridor `cause()` results plus the deadline extraction, compared across the synchronization edge.

- **Divest-as-sync-edge.** A per-corridor divest point that lands on a narrow interface *is* a convergence constraint — the moment one corridor releases what another must pick up. This is the bridge: precondition accumulation reveals a corridor's phases (its divests); the divests that fall on interfaces are where one corridor's recovery becomes another's scheduling constraint. Watch the precondition set's non-monotonic points; the ones on interfaces are convergence points.

- **Reschedule, don't re-fix.** When corridors are individually solved but jointly infeasible, the repair is a different *linearization* of the partial order — start the slow producer earlier, or co-advance two subsystems concurrently instead of sequentially — not a precondition fix inside any corridor. New recovery class, sits above all the per-corridor ones.

- **Co-advance cyclic synchronization.** When two subsystems each produce what the other needs (an SCC of subsystems), they can't be ordered — advance them together, respecting each one's deadline. The subsystem-level analog of the SCC mega-waypoint, made harder by clocks. Rare and small (cycles are usually 2–3 subsystems), but the genuine hard residue.

## Termination & failure

- **Spin guard.** If the precondition set hasn't grown since the last attempt from this checkpoint, stop. Identical set + identical state + still failing = not an ordering problem. Report the contradiction. Second clause for multi-corridor: each corridor individually solved but convergence still infeasible after rescheduling = a coordination contradiction, not a precondition gap — report *that*.

- **Diagnosis as output.** On failure, return `Diagnosis`. Single-corridor: the precondition set (what was tried), the contradicting enablers (why it's stuck), the specific blockers (what the user can act on). Multi-corridor: the subsystems, where they couldn't align, and the producer that was too slow. Not Intractable. Not NotFound. An explanation — and for the multi-corridor case, it tells the operator the machine's *coordination* is the problem, not a single interlock.

## What's novel (as far as the search reaches)

The individual mechanisms all have prior art. The **closed loop** — actual-cause attribution as the repair signal in a solver-free forward planner over the executable program, aimed at producing an operator-executable plan — appears to be open ground. The analyzer is Halpern-Pearl. The planner is directed model checking. The regression is System-R. Wiring them together, without a solver, because the program is the model: that's the contribution.
