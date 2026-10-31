# Black-box prover: plan (decided 2026-10-30)

## Why, in my words

The prover's job is to say "this ladder program can never do X." To make that
fast, a layer was added that models timer and counter timing so the prover can
skip ahead through long waits. That layer grew to about 6k lines, and nothing
independent checks it: the soundness fuzzer's baseline keeps it switched on,
and a false Proven has no trace to replay. It needed 26 soundness fixes in
eight days, and every change to timer semantics rippled into it. A spike showed
it mostly agrees with plain stepping, found a cache bug nobody was looking for,
and confirmed that exact timing was never real anyway: scan time jitters, and
tag presets can be rewritten over Modbus.

So the layer goes. Timers become "Done may flip on any scan it's enabled",
which is sound because it includes everything the real timer can do. Every
counterexample is replayed on the concrete PLC before it's believed.
Timing-dependent properties stop being Proven, and fold answers timing
questions as a simulator instead. The prover keeps BFS, the kernel, classify,
elision, factoring and lock files, and for the first time has a baseline simple
enough to fuzz against.

This isn't reneging on a promise. The verification guide already says the
prover is "timing-blind by design": it answers "can the alarm fire?", not "does
it fire in time?", and timing is checked with `run_for` tests. The 2026-10-23
"exact idioms" contract had the code promising more than the docs ever did. A
Proven should mean proven under any timing, as the guide already says it does
for clocks and the RTC, not under one imaginary 10 ms scan.

One sentence: the prover should know nothing about time, because nothing about
time was ever certain, and the code that pretended otherwise was the code I
couldn't check.

## Decision

Timers become black boxes whose Done may change at any scan the instruction
allows; counterexamples are confirmed by concrete replay (fold makes long waits
cheap). Supersedes the prover half of the 2026-10-23 contract (common timing
idioms exact). Fold's contract is unchanged.

## User-facing decisions before phase 1

- **`settled=True`.** Today it means "evaluate after pending timers settle", and
  settling goes away. Proposed meaning: evaluate only in states where no Done
  choice is still open (every enabled TON done, every disabled TOF off, every
  running drum step finished). Same user intent ("after timers fire"), simpler
  to implement and explain.
- **How "unconfirmed" is reported (decided 2026-10-30).** `Intractable` with a
  kind field, carrying the abstract trace and where replay diverged. It's a
  different kind of Intractable: not "too big" (fix: add `choices`/`min`/`max`)
  but "the abstraction can't tell" (fix: write a `run_for` timing test). Keeps
  existing three-way `isinstance` checks working.
- **`settled=True` (agreed 2026-10-30):** the meaning above.

## Docs to change (docs/guides/verification.md)

- "How it works": the three-valued `False`/`Pending`/`True` abstraction and
  fast-forwarding go; Done is a free choice while enabled.
- `settled=True` section and the detection-gap recipe that uses it: new meaning.
- Verdicts: add the unconfirmed case and its fix (a `run_for` timing test).
- The note that a few accumulators are always Intractable (`time_event_drum`,
  `debounce`, `flasher`, `timed`, some `time_drum`s): those become ordinary
  black-box timers and the note goes.
- `--depth-budget` paragraph: the "hidden-event acceleration can cover more
  concrete scans" sentence goes; the budget is a plain BFS depth again.
- "Timing-blind by design" paragraph: keep, now literally true.

## Why (evidence)

- Oct 22-30: 48 commits touched `prove/`, 26 `fix(prove)`, nearly all soundness
  holes in the layer that re-models timer/counter/drum semantics (absorb,
  events, cycles, deadness, ~6k lines). Each conformance semantics change
  rippled into it.
- That layer had no independent oracle: `_OptConfig.sound_baseline()` keeps
  `hidden_event_jumping` / `pending_settlement` on, and `_CycleTracker` is not
  gated at all.
- Jump spike (`tests/jump_check.py`, `--jump-check`, uncommitted): caches off,
  9,068 jumps agree with stepping. Of the ~1,160 that don't: 1,111 are an
  unread PackML timer (harmless), 52 skip an intermediate key change, 223
  settle scan counts disagree (untriaged). With the jump/settle caches on, 17
  land on a different state key (cache key too coarse).
- The exact timing assumes a fixed `dt` scan (default 10 ms); a real CLICK's
  scan time varies, so tight timing proofs never held on hardware anyway.

## Model

The prover kernel is compiled with abstract timers (a `compile_kernel` flag
used only by `_pass_compile_kernel`): the instruction keeps its enable and
reset logic, but Done comes from a BFS choice instead of the accumulator.

| Instruction | Enabled | Disabled / reset |
|---|---|---|
| `on_delay` (TON) | Done may go False -> True on any scan; stays True | Done False |
| retentive TON | Done may go False -> True on any scan | Done holds; reset clears |
| `off_delay` (TOF) | Done True | Done may go True -> False on any scan; stays False |
| time drum step | step may advance on any scan | per drum rules |
| Out timed/flasher/debounce | treat their internal timer the same way | |

- Accumulators the guide calls always-Intractable today (`time_event_drum`,
  `debounce`, `flasher`, `timed`, `time_drum` with a jump or a flag that starts
  on) become ordinary black boxes.
- `Acc` read other than through its own Done: any value in its
  comparison-derived domain (classify already infers it) consistent with Done
  (`Acc >= preset` iff Done for TON). No monotonicity in v1; spurious
  counterexamples are caught by replay.
- Preset 0 and tag presets: Done may go True on the first enabled scan; a tag
  preset is just read, nothing resolves it.
- Counters (after S2 they count edges): concrete. Counting is discrete, not
  time. If only Done reads Acc, the key holds `min(Acc, preset)`; otherwise Acc
  is concrete and a large domain hits the state budget -> Intractable. Open:
  whether even the `min` collapse is worth keeping.
- Clock bits and time-derived system tags: already free inputs
  (`system_points.py`), unchanged.
- Tasks / interrupts (`scratchpad/interrupt-plan.md`, not built): a program
  with any task returns Intractable naming it. Mid-scan preemption is out of
  scope.

Sound because each abstract timer allows every Done sequence the real one can
produce under any scan timing.

## Results

- No violation in the abstract space: `Proven`.
- Violation: replay the trace on a concrete PLC. Inputs are applied as
  recorded; a step where the trace flips a Done becomes
  `run_until(Done changes)` with inputs held (fold), bounded. If the replay
  reaches each step's key: `Counterexample`. If not: "unconfirmed: depends on
  timer timing", naming the timer and keeping the abstract trace for the user
  (an `Intractable` of the timing kind, see "User-facing decisions" above).
- `reachable_states()` (lock files) over-approximates: it includes states only
  impossible timing reaches. Locks need a one-time regeneration; the miner's
  lock filter gets less precise.

## What goes, what stays

Delete: `absorb.py` (move `_done_acc_state` out first: `pilot/world_key.py`
imports it), `events.py`, `cycles.py`, `deadness.py`, threshold vectors,
three-valued Done/PENDING in the state key, stop points, co-fire witnesses,
abstract thresholds, `timer_quanta`, `_pass_collect_done_acc_pairs`,
`_pass_find_*_absorptions`, `_pass_build_event_specs` and their stubs, the
hidden-event and settle caches, `tests/jump_check.py`.

Keep: classify (PILOT's `domains_only` prior, `value_domains.py`,
`cmp_conditions.py`), kernel snapshot/step, inputs, independence/factoring,
exclusive inputs, edge compression/demotion, slice elision, split_at, pacing,
lockfile, results, system points.

Key payoff: with nothing reach-extending left, `_OptConfig.all_off()` becomes
the sound baseline. Every remaining reduction (elision, edge compression,
factoring, ...) is then checked by the soundness fuzzer against plain BFS, which
is the independent oracle the timer layer never had.

## Phases

0. Wait for S2 (`bob/s2-counters`) to land; it already removes the counter
   hidden-event sources. Starting earlier conflicts in `events.py`/`cycles.py`.
1. Abstract-timer kernel flag + BFS choice for Done; `all_off` as the sound
   baseline. Run old and new side by side on the suite to list every verdict
   that changes.
2. Concrete replay confirmation (fold) and the "unconfirmed" Intractable.
3. Delete the timer layer; simplify the state key; move `_done_acc_state`.
4. Tests: delete or rewrite exact-timing tests (timer_abstraction, settlement,
   absorption_gap, the timing rows of the matrix) as Proven / Counterexample /
   unconfirmed; keep the counterexample replay rule.
5. Docs (verification guide, prove AGENTS.md, formal foundations), CHANGELOG
   (breaking: timing-dependent properties become unconfirmed; regenerate
   locks), PackML lock time measured.

## Acceptance

- The point of the arc (user, 2026-10-30): conformance stops spilling into
  `prove/`. After this, a timer/counter/drum semantics change touches the
  runtime, codegen and fold, not the prover. If a later conformance change
  still needs a `prove/` edit beyond its kernel flag, that is a design miss to
  raise, not a fix to absorb.
- The false-Proven probes P1a/b/c and P2
  (`pyrung-wt/buildbot/jobs/s2-counters/probes/`, see `REVIEW.md` beside it)
  never return Proven.
- `make test-soundness` and `make test-fuzz` with `all_off` as the baseline.
- Every Counterexample replays (two-oracle rule).
- PackML `pyrung lock` time measured and reported (91 s today).

## Parked

`bob/prove-gaps` stopped mid-diagnosis; unverified WIP at `eede4fc`. Only
harvest its probe shapes.
