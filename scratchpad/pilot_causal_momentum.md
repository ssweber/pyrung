# PILOT: Causal Momentum Redesign

## The Reframe

An engineer commissioning a PLC doesn't measure distance to goal. They
can't — they often don't know the topology either. They measure **causal
momentum**: did my action change something, is it new, did it stick, and
can I keep going from here?

The program's behavior is the map. The engineer reads it one step at a
time.

## The Problem

The current PILOT loop uses `unsatisfied_count()` as both the step-level
gate (accept/reject an action) and the episode-level progress monitor.
It's bad at the first job, which forces six interacting workarounds:
`gate_moves_budget`, `chain_width` escalation, `chain_prereqs`,
`damage_history`, `nogoods.clear()` on acceptance, and multi-branch
acceptance (GATE-MOVED / CHAIN-PROGRESS / ACCEPT-WITH-DAMAGE / NEUTRAL /
REGRESSED).

The worst failure mode: PILOT burns its entire budget retrying actions
that have no effect, or treating accumulator ticks as progress while the
state key never changes. Everything below exists to prevent that, in
layers, where each layer only matters because the one below it holds.

## The Foundation: State Key

The **state key** (from BFS cycle detection) is the observation lens for
the entire redesign. It encodes "what matters" — the projection that
filters out accumulator ticks, scratch movement, and noise.

Every layer below uses the state key as its reference frame. Actions are
evaluated against it. Nogoods are scoped by it. Novelty is defined in
terms of it. It's the spin guard, and everything else builds on that
guard holding.

## The Layers

### Layer 0: Don't Spin (Responsive)

Did the state key change?

No → the action had no effect from this state. Stop. Record a nogood
keyed on the current state key. Try the next candidate. This is the
floor. Without it nothing else matters because you burn budget going
nowhere.

This alone fixes the worst failure mode — the one where PILOT retries
the same action, or actions that look different but produce no
structural change, eating scans until budget exhaustion.

### Layer 1: Don't Cycle (Novel)

Has this state key been seen before this episode?

Key changed (Layer 0 passed) but it's a key we've visited → we're
looping through states. Stop. Nogood.

Novelty is on state keys, not tag values. ABORTED with timer at 500 vs
timer at 1000 = same key, not novel. ABORTED → CLEARING = different key,
novel.

### Layer 2: Don't Hallucinate Progress (Durable + Survivor)

Does the state key hold after the settle window?

Run a few scans past the action, check the key again. Three outcomes:

**Held** — real transition. Proceed to Layer 3.

**Reverted but excursed** (survivor) — the key changed transiently.
Something fired, propagated, then got cleared. This is not a nogood.
The action *does* affect this path but can't stick without help.

Diagnose: scan history at peak excursion, run `cause()` on whatever
cleared it, derive a hold. Retry the action with the hold installed.
Also check for latched side effects — timers started, one-shots fired,
latch-instructions that wrote during the excursion and survived the
revert. Those are real progress made invisibly outside the state key
projection.

**Never moved** — same as Layer 0 (not responsive). Already caught.

### Layer 3: Don't Dead-End (Generative)

Are there new controllable inputs from the new state?

Re-trace (Probe) from the new state. If the action frontier changed —
new steerable inputs available that weren't before — you can keep moving.
If it's the same set of options, you're in a pocket. Backtrack.

### Layer 4: Don't Wander (Trend Monitoring)

Is the sequence of accepted actions converging toward the goal?

After each committed step, re-trace from the new state. The trace tree
gives trend indicators:

- **Satisfied fraction** trending upward — conditions are closing.
- **Deepest unsatisfied node** getting shallower — remaining work is
  closer to the surface.
- **Trace tree shrinking** — fewer nodes, simpler path to goal.

This is `unsatisfied_count`'s proper job — demoted from gatekeeper to
trend indicator. It doesn't decide whether to accept an action (Layers
0–3 handle that). It decides whether the *sequence* of accepted actions
is getting somewhere.

If you commit N novel durable generative steps and the trend flatlines
or reverses, you're wandering despite moving.

**Checkpoints.** Fork is free. A checkpoint is just a kept fork. Every
time the trend indicators improve, snapshot the current state. If the
trend reverses, revert to the checkpoint and try a different branch.

### Layer 5: Don't Repeat Mistakes (Cause Chains)

Understand *why* something regressed, install holds, retry with
constraints.

Checkpoints answer: where do I go back to?
Cause chains answer: **why** did it regress, and what do I hold to
prevent it next time?

On trend regression: revert to checkpoint, chase `cause()` from the
regressed state to find steerable roots, install holds, retry from the
checkpoint with the new constraint. The branch that failed before may
work now because the conflicting input is pinned.

Without cause chains, checkpoint revert is blind retry. With them, each
regression teaches a constraint. This compounds — each failure narrows
the search.

Keep: `_chase_cause_roots`, `_walk_cause_chain`, `_install_holds`,
`forced_holds`. These are powerful and stay.

### Layer 6: Don't Rediscover (Slices)

Observed transitions become known topology. Skip the exploration next
time.

**Early episode** (no topology known): causal momentum is all you have.
Layers 0–5. Depth-first.

**Mid episode** (slices accumulating): observed transitions build a
partial topology. ABORTED→CLEARING→STOPPED→IDLE becomes a known
sequence. `unsatisfied_count` against the slice's ordering becomes
meaningful because the ordering is grounded in observation.

**Late episode / repeat queries** (warm slice library): goal distance
works immediately. Topology is already there.

Causal momentum is the universal fallback. Goal distance is an
accelerator that activates when slices provide ordering. Weight shifts
naturally as slice confidence grows. No mode switch.

Slices also tell you when causal momentum is *misleading* — a novel
durable change that moves further from the goal in slice-space is a
wrong turn. Without slices you'd commit. With them you prune early.

## Nogoods Keyed on State Key

Current nogoods are `set[str]` (just tag names), cleared on every
acceptance. Too broad when they match, too narrow when they don't.

New nogoods: `dict[StateKey, set[str]]`. "Action X from state key K
produced no key-space change." Next time in key K, skip X. Different
key, different nogoods — the scope is structural state, not individual
tag values.

No reset between episodes. No `nogoods.clear()` on acceptance. Nogoods
accumulate across `how()` calls on the same program. First call explores
and logs dead ends. Second call skips them immediately.

Watch: if the state key is too coarse, nogoods over-prune. Too fine,
they never match. Tuning problem on a mechanism that already exists,
not a new design question.

## What Dies in `_pilot_loop`

- **`gate_moves_budget`** — gone. Existed because `unsatisfied_count`
  couldn't see ABORTED→CLEARING as progress. Layer 0 sees it.
- **`chain_width` escalation** — gone. Existed to widen batches when
  single actions were NEUTRAL. Layer 0 is decisive: no key change → skip.
- **`chain_prereqs` / CHAIN-PROGRESS path** — gone. Static detection of
  sequential dependencies in the trace tree. Layers 0–3 handle sequential
  progress naturally; Layer 6 handles it explicitly.
- **`damage_history` / ACCEPT-WITH-DAMAGE** — gone. Layers 4–5
  (checkpoints + cause chains) handle regression recovery cleanly.
- **`nogoods.clear()`** — gone. Nogoods are self-scoping via state key.
- **`snap_changed` as acceptance criterion** — gone. Rough proxy for
  "something meaningful happened." Layer 0 is the precise version.
- **The six-branch acceptance tree** — collapses to four checks
  (Layers 0–3).

~250 lines of branching logic and 5 ad-hoc state variables become ~40
lines and 2 state variables (`nogoods: dict[key, set]`,
`seen_keys: set`).

## What Stays

- **Trace-back** (`trace_back`, expression tracing, writer ranking,
  indirect inversion) — the Probe step. Untouched.
- **Candidate ordering** — trace-guided first, upstream second. Blast
  radius filtering. Same as now.
- **Cause-chain analysis** — `_chase_cause_roots`, hold installation.
  Triggered on trend regression (Layer 5), not on every action.
- **Edge/pulse handling** — `_apply_pulse`, `compute_edge_tags`,
  `compute_resting_values`. Untouched.
- **Fork-and-observe pattern** — the core mechanism. Same.
- **`unsatisfied_count` / `same_tag_chains` / `pivot_tags`** — demoted,
  not deleted. Trend indicators (Layer 4), not gatekeepers.

## What This Is Not

This is not BFS. The trace still leads. Candidates are goal-directed,
not exhaustive. Commitment is depth-first — take the first action that
passes Layers 0–3 and go deeper. Backtrack only when stuck. No frontier,
no queue, no parallel exploration.

## The Pseudocode

```
seen_keys: set = {}
nogoods: dict[StateKey, set[str]] = {}
checkpoints: list[(StateKey, Fork, TrendScore)] = []

while budget:
    if goal satisfied: return success

    key = state_key(work)
    trace = probe(target, work)           # backward trace — unchanged
    trend = score_trend(trace)            # satisfied fraction, depth, tree size

    # Layer 4: checkpoint on trend improvement
    if trend > best_trend:
        checkpoints.append((key, work.fork(), trend))
        best_trend = trend

    candidates = trace_candidates(trace) + upstream_candidates(trace)
    candidates = [c for c in candidates if c not in nogoods.get(key, {})]

    committed = False
    for action in candidates:
        fork = work.fork()
        apply(fork, action)
        new_key = state_key(fork)

        # Layer 0: don't spin
        if new_key == key:
            nogoods.setdefault(key, set()).add(action)
            continue

        # Layer 1: don't cycle
        if new_key in seen_keys:
            nogoods.setdefault(key, set()).add(action)
            continue

        # Layer 2: don't hallucinate progress
        durability = check_durable(fork, key)
        if durability == REVERTED:
            peak = scan_history_at_peak(fork)
            clearing_cause = chase_cause(fork, peak, reverted_tags)
            hold = derive_hold(clearing_cause)
            if hold:
                fork2 = work.fork()
                install_holds(fork2, hold)
                apply(fork2, action)
                if check_durable(fork2, key) == HELD:
                    fork = fork2           # retry succeeded with hold
                else:
                    continue
            else:
                continue
            side_effects = latched_during_excursion(fork, peak)

        # Layer 3: don't dead-end
        if not generative(fork, trace):
            nogoods.setdefault(key, set()).add(action)
            continue

        # commit
        seen_keys.add(new_key)
        work = fork
        committed = True
        break

    if committed:
        # Layer 4: trend check after commit
        new_trace = probe(target, work)
        new_trend = score_trend(new_trace)
        if regressed(new_trend, best_trend):
            # Layer 5: cause analysis + revert
            holds = chase_cause_roots(work, regressed_tags)
            work = checkpoints[-1].fork
            install_holds(work, holds)
        continue

    # nothing worked — step forward (timers/SFCs)
    work.step()
```

## Design Principles (unchanged)

1. Readable — engineer follows output without explanation.
2. Reproducible — output is copyable PLC API commands.
3. Built on the API — `patch`, `force`, `run_until`, `when`, `monitor`,
   `cause`, `diff`, `fork`. No parallel infrastructure.
4. "What would an engineer do?" — poke and watch, track novelty, check
   durability, check the frontier, repeat until stuck or cycling.

## Anti-patterns (unchanged)

Don't build graphs. Don't classify writers. Don't predict outcomes.
Don't add special-case mechanisms. Don't reach for CEGAR/POCL/PDR.
Read the program. Simulate. Observe. Fix. Repeat.
