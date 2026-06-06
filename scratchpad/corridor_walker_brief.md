# how() Planner — New Direction: Corridor Walker

**For:** Claude Code, anchoring this plan in the actual `prove/` + `how()` + runner code.
**Status:** Design hypotheses. Every claim about an existing function, module, or capability is to be **confirmed against the code** before building on it. Where this brief says "you already have X," verify X exists and does what's claimed; if it doesn't, that's the first finding to report back.

---

## The one-paragraph version

`how()` today is a waypoint planner whose **per-waypoint scoped BFS** does the heavy lifting — and that inner search is what OOMs (~20GB) and exhausts the eval budget on real programs. The new direction keeps the waypoint planner's **front half** (corridor generation) and replaces its **back half** (per-segment scoped BFS) with a **corridor walker**: sequential simulation on the PLC runner that steers forward with projected `effect()`, jumps time at scheduled crossings, diagnoses divergences with `cause()`, learns preconditions, and backjumps to re-fork on conflict. Most segments *walk* in milliseconds with no search, no frontier, no visited set. The scoped BFS survives only as a **per-segment fallback** for the genuinely hard residue, seeded with what the walk already learned. This is not "smarter search" — it's "mostly no search," because PLC programs are corridors and you already built the diagnostic tools to walk them.

---

## Why this changed (the arc, so the reasoning is anchored)

1. The driving pains are **OOM** and **eval-budget exhaustion**, both rooted in the per-waypoint scoped BFS flooding the state space.
2. The real difficulty on PackML-style programs isn't "which of 2^15 inputs" — it's **the abort/divergence**: you flip CmdStart, STARTING goes to ABORTING instead of EXECUTE, and BFS explores the abort subtree without knowing *why* it diverged.
3. A distance heuristic (h_FF) tells you a state is *bad*; it does not tell you *what to flip to avoid it*. `cause()` does — it names the trigger and enabler directly.
4. Most of the conflict-analysis machinery a classical planner needs (Steinmetz & Hoffmann's clause-learning state-space search; see references) exists to *compute* why a state is a dead-end. **In a PLC the program tells you** — `cause()`/`simplified()` read it off the SP-tree. The program is the model.
5. `cause()` needs scan history; the BFS runner has none, but the **PLC runner does** (ScanLog + replay-from-log). The corridor walk is sequential simulation, so it lives in the runner, not the BFS.
6. Timers aren't a wait — **time is a knob**: advance the clock, jumping to scheduled crossings so nothing meaningful is skipped.
7. The common real query is **recovery**: start from a faulted/aborted snapshot, get back to EXECUTE (or "MotorX on"). This is the `why()` → `how()` workflow, and the corridor walker handles it identically to cold start — recovery is just a longer corridor.

---

## Architecture: two planners, shared knowledge, separate infrastructure

### Planner A — Corridor Walker (new; primary; handles the large majority)

Runs on the **PLC runner**, not the BFS infrastructure.

**A1. Corridor generation — REUSE the existing waypoint planner's front half.**
The ordered waypoint sequence IS the corridor. For `how(EXECUTE)` from ABORTED, it's the mode path back to EXECUTE; for `how(_CurStep==5)`, the state-machine waypoints plus the step waypoints. Inputs already built: landmark extraction, the **value-transition graph** (`_build_value_transitions`), SCC ordering, value-stepping. The value-transition graph over a mode/counter tag is effectively a tiny exact pattern database — shortest mode path = waypoint sequence, no h_FF needed. (h_FF only for compound goals where no single tag's value graph captures the corridor.)
*Confirm:* is the ordered waypoint sequence cleanly extractable from the planner without triggering the per-waypoint scoped BFS?

**A2. Per-segment walk — REPLACES the scoped BFS.**
For each consecutive waypoint[N] → waypoint[N+1]:
- Step forward on the runner (interpreted scans; history maintained for `cause()`).
- At each decision point the **event scheduler** surfaces, use projected `effect()` to choose the steer that keeps heading toward waypoint[N+1].
- **Forward = `effect()`** (pick the move), **backward = `cause()`** (diagnose the miss). Same SP-tree machinery, full fidelity because the runner holds history.
- `simplified()` of the segment's enabling condition gives the *complete* precondition list (all required feedbacks at once), not just the one that tripped.
*Confirm:* projected `effect()` / `cause()` reachability semantics — see "Reachability modes" below.

**A3. Time as a knob — REUSE the crossing analysis from `events.py` / `absorb.py`.**
Don't step N scans for a timer; advance the clock to the next **scheduled crossing** (every accumulator threshold any rung reads, plus presets), settle the scan, repeat. Inputs held during a wait ⇒ no branching ⇒ deterministic event-driven simulation (tesseracting with the combinatorial part off). The abort deadline is just another crossing — advance to just-before (did the feedback confirm?) and to the deadline (did the abort fire?) to *test* whether preconditions beat the deadline.
*Confirm:* is the crossing schedule exposed in a form the runner can consume, or is it welded to the BFS explore loop? This is the single most important reuse question.

**A4. Divergence → learn → backjump → re-fork.**
When a steer lands off-corridor (didn't reach the waypoint, or left a "must-stay" state like EXECUTE), `cause()` the divergence. Trigger names the deadline; enabler names the dropped ball. Add to the **precondition set** (monotonic — only grows; guarantees termination, prevents oscillation). Then **backjump**: the cause chain names *where* the conflicting commitment was made (e.g., a latch set back in RESETTING), so rewind to *that* waypoint, not a fixed depth. Re-fork from that waypoint's **checkpoint** and re-walk with the updated set.
*Confirm:* can the runner checkpoint state at each waypoint and re-fork cheaply (one snapshot per waypoint)? If not, rewind = replay-from-start with new constraints (still correct, just slower). This is the key perf detail for the backjump loop.

**A5. "Must-stay" stability is the same mechanism as progress.**
Reaching EXECUTE and staying in EXECUTE are one problem: "the state should be X; is it? if not, why?" The corridor is defined *positively* (where you want to be), divergence is any deviation, `cause()` bridges the gap. No enumeration of failure modes.

### Planner B — Scoped BFS (existing; demoted to per-segment fallback)

Fires only when a **single segment** can't be walked: an **order-independent** conflict (X and ~X both required on the only path — not fixable by reordering), or genuine multi-path branching where the intended corridor is blocked but another may exist. Note: most apparent "conflicts" are *ordering* problems that A4's backjump fixes for free — true conflicts are the residue of the residue.
- Scope is the **stuck hop only**, not the whole query.
- Seeded with the precondition set Planner A already learned, so it's constrained, not blind.
- Backbone for this is conflict-driven dead-end learning (Steinmetz & Hoffmann): `cause()` replaces their expensive conflict-analysis (Algorithm 2 / ExtractX), and the learned no-good set doubles as an **unsolvability certificate** for an honest "can't be done."

### Shared: the precondition set

Planner A's learned preconditions are the bridge to Planner B. Knowledge is shared; infrastructure is not.

---

## Result-type honesty (decide at first commit; painful to retrofit)

- A walk that finds a path → a real `Path` (same output format `how()` already emits: input changes + scan counts).
- A walk that hits an **external blocker** (a feedback that can never confirm) → honest diagnosis: "can't reach EXECUTE — VacuumSwitch never confirms," **not** Intractable. `cause()` names the blocker.
- A true **order-independent contradiction** → `Unsolvable` with the contradiction/no-good set as a checkable certificate.
- A fallback BFS that exhausts budget/beam → `NotFound(reasons=[...])`, **never** readable as "proven impossible."
- `always()`/`never()` keep their existing soundness — none of A's machinery touches them.

---

## Reachability modes (a real gap to resolve against the code)

Projected `cause()`/`effect()` currently treat a ladder-written tag as reachable **only if observed in recorded history** — correct for *diagnosis against a test trace*, wrong for *planning into an unexplored future*.
- **Steering (A2):** observational semantics are right — "given where I actually am on this trajectory, what does this steer do." The walk builds history as it goes; a projection at scan 4 legitimately only knows through scan 3.
- **Regressing a missing precondition (A4):** you want a **constructive** mode — "can *any* rung produce this value, recurse to inputs," not "was it observed." Today projected cause stops at the first unobserved ladder-written tag and reports a blocker (the "wrote a clear rung but never fed it" bug it's proud of catching). For planning, that's a false dead-end.
*Confirm + likely build:* is constructive reachability a changed stopping rule on the existing projected walker, or a separate path? This determines whether A4's regression is a small change or a new pass.

---

## What to map first (before building)

1. **Waypoint planner front/back split** — where does corridor generation end and per-waypoint scoped BFS begin? (`_order_waypoints`, `_run_waypoint_plan`, `_run_single_wp`, `_try_decompose_scc`.) Can A1 get the waypoint sequence without firing the BFS?
2. **The event/crossing schedule** — `events.py` (hidden-event scheduling, settle cascade) and `absorb.py` (threshold absorption). Is the crossing schedule reusable by the runner (A3), or BFS-internal?
3. **Runner capabilities** — ScanLog, `cause()` replay-from-log, `force()`, `step()`, **clock/time advance**, **state checkpoint + re-fork**. A1–A4 assume all of these. Confirm each.
4. **`cause()` / `effect()` / `simplified()` surfaces** — fidelity/caching (full vs timeline; enablers empty on cache miss), projected modes, `assume=`, `blockers`, `held_since`, timer-preset annotation on triggers (the deadline number A3 needs — may need adding).
5. **Result types** (`results.py`) — room for `Unsolvable(certificate)`, `NotFound(reasons)`, and an external-blocker diagnosis distinct from `Intractable`.
6. **Probe machinery** — does the corridor walker's interpreted stepping subsume `_probe_cone_expansion` (which approximates, via input perturbation, what the interpreter resolves directly)?

## Suggested build order

1. **Walk one known-good corridor on the runner**, no learning: generate waypoints (A1), step each segment with time-jumps (A3), assert you reach EXECUTE on `packml_bench`. Proves the runner + scheduler + waypoint reuse all connect.
2. **Add `effect()` steering at decision points** (A2). Confirm it picks corridor-preserving steers.
3. **Add divergence → `cause()` → precondition learning → retry** (A4), first with fixed full-restart rewind. Get an abort-and-recover working on `packml_bench` (e.g. a start feedback that must be forced).
4. **Add checkpoint + backjump re-fork** (A4 perf). Rewind depth from the cause chain.
5. **Add the timed-feedback case** — timer-preset annotation on `cause()` triggers + the deadline-test crossings (the vacuum-switch scenario).
6. **Constructive reachability mode** for A4 regression (resolve the gap above).
7. **Wire Planner B as per-segment fallback**, seeded with the precondition set; add `Unsolvable(certificate)` and honest result types.
8. Only then revisit whether h_FF / novelty / decoupled are needed for any residual segment that still won't walk.

**Guiding principle:** build the walker on `packml_bench` first and measure. A PackML path is a corridor; the program is built to be operated by satisfying interlocks and issuing commands. If the walker takes the current OOM/budget-exhausting case to milliseconds — likely — then the entire scoped-BFS heroics (SCC mega-waypoints, value-stepping, cone widening, eval budget, mega-cone gate) stop being load-bearing for the common case and survive only for the residue.

---

## References (the fallback's backbone, not the primary path)

- Steinmetz & Hoffmann (2016), *Towards Clause-Learning State Space Search: Learning to Recognize Dead-Ends*, AAAI — the conflict-driven learning loop; `cause()` replaces its Algorithm 2. https://fai.cs.uni-saarland.de/hoffmann/papers/aaai16.pdf
- Steinmetz & Hoffmann (2016), *State Space Search Nogood Learning*, AIJ — length-independent sound nogoods (trustworthy `Unsolvable`). https://www.sciencedirect.com/science/article/pii/S0004370216301448
- Steinmetz (2022), PhD thesis, *Conflict-Driven Learning in AI Planning State-Space Search* — convergence + trap learning. https://dblp.org/rec/phd/dnb/Steinmetz22.html
- Lipovetzky & Geffner (2017), *Best-First Width Search* — novelty/memory bound, if a residual segment needs real search. (Saarland/Geffner lineage cross-references cleanly.)
- Timing/deadlines live in the **timed-automata** tradition (Alur–Dill; UPPAAL), separate from classical planning — relevant only to A3's deadline math, not the core loop.
