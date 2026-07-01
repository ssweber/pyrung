# Synthesis = rungs: consolidating harness, holds, and `when().do()`

> Status: **design, agreed 2026-06-30.** Supersedes the 2b/2c/2d/2e increment
> framing in `DESIGN.md` for the *forward plan*. `DESIGN.md` retains the landed
> history (1a/1b/2a/2c) and the original subsystem analysis.
>
> **PIVOT 2026-07-01 (below) supersedes "The bracket model" for the forward
> plan.** The landed `plant`-as-last-rung placement is wrong: feedback is an
> *input*, read at the top of the scan, so the plant moves to a **pre pass**.
> `when().do()` is *removed* from the synthesis vocabulary. The rest of this doc
> (thesis, run_function, two-roots, replay, subsystem-inheritance) stands.

## PIVOT — plant is the input-read pass; the scan is one program of `call()`s

**The mistake we're correcting.** The bracket model runs `plant` as the *last*
rungs (`_commit_scan` → `_evaluate_synthesis(plant)`), so `Fb` commits in the
same scan the command settles. That is non-physical: a sensor cannot respond to
an output *before that output is written*. Outputs are written end-of-scan; the
plant responds; the sensor enters the **input image at the top of the next
scan**. Minimum command→feedback latency is one full scan — always, even at
`on_delay == 0`. **`Fb` is an input**, read at the top, reflecting the
*previous* commit's command.

So the CONSOLIDATION's "feedback one scan earlier (2c's read-lag was an
artifact)" had the sign backwards. The read-lag scan is **reinstated** and
correctly justified as the input-image latency. Observable:

- `on_delay == 0`: program-visible timing unchanged (gated rung still fires
  N+1); only the committed `Fb` value moves — `False` at end of scan N, not
  `True`. (This is the `test_out_driven_command_feedback_lands_next_scan` diff.)
- `on_delay > 0`: the whole feedback response shifts **one scan later** — the
  plant doesn't *see* the command until the scan after it's written.

### The scan is one program of subroutine calls

Not "three programs the runner loops over with imperative patch/force drains
wedged between" — that would force us to teach causal/fold/compile "there are
two programs now." Instead the soft-exec fork scans **one synthetic `__scan__`
program** whose rungs are `call()`s. Every phase is a pass; the only difference
is where each pass's rungs come from:

```
__scan__:                         source of the pass's rungs
  call(__plant__)        ← compiled from harness declarations   (stable per install)
  call(__holds__)        ← compiled from PILOT hold decls        (fork-local, recompiled on change)
  call(__drain_patches__)← conceptual pass; body drains the external patch dict, one-shot
  call(__forces__)       ← conceptual pass; body applies the external force dict
  call(__plc__)          ← the user program                      (stable)
  call(__forces__)       ← re-applied post-logic
  commit                 ← one dt, one commit
```

- **plant / holds / plc** — compiled-from-declarations; content is logic.
- **drain_patches / forces** — *conceptually a pass* (body stays imperative:
  read the outside dict, apply), but **invoked via `call()`** so they appear as
  ordinary subroutine nodes in the one program flow. `call()` is already
  descended by causal (`project_node_firing_capture`) and walked by fold/compile
  — so wrapping the external-dict passes as calls means **nothing new to teach**;
  the flow is a single continuous rung stream. Patch/force *attribution* stays
  exactly as today (recorded nondeterminism in the ScanLog); the `call()` wrapper
  is about *placement in the flow*, not re-teaching attribution.

  *Deferred option (evidence-gated):* the body could be **fully lowered** — a
  `forloop` over the external dict emitting real `copy` writes — making
  patches/forces genuine rungs (causal/fold see each write natively, no
  today's-attribution carve-out). Don't build this up front. When we reach it,
  **instrument first and decide what is actually gained** vs. the imperative body
  behind a `call()` — the uniformity may not pay for regenerating throwaway rungs
  each scan. Start imperative-behind-`call()`; lower to `forloop` only if the
  instrumentation shows a concrete win.

The runner scan loop collapses to *"scan one program of `call()`s, commit once."*
Deleted: `_pre_scan_callbacks` / `_on_pre_scan`, the plant-post special path in
`_commit_scan`, the `_dt_override` peek, the `[*holds, *user, *plant]` bracketed
kernel (runner.py:1579). One dt, one commit, uniform.

### Precedence = last-writer-wins order

`plant < holds < patches < forces`. Plant lays down natural feedback; a hold
overrides it; a patch overrides a hold; **force is the hard pin** applied
**pre *and* post** logic (wins against the program's own writes). **Holds are
pre-only** — they steer an input the program *reads*; forces pin against writes.
Matches "force is the hard user pin, holds is PILOT's steer below it."

### What this unifies / fixes (beyond the phase)

- **analog/bool phase split gone.** Today bool = post (plant bracket), analog =
  pre (`_pre_scan_callbacks`). Plant-first puts *all* feedback in the one
  `__plant__` pass at the top → `_pre_scan_callbacks` / `_on_pre_scan` dissolve.
  This unblocks the "analog migration, blocked on active-latch-under-replay"
  item — plant-first *is* the unblock.
- **holds survive fork.** `__holds__` is a compiled program the fork holds **by
  reference**, so it survives `fork()` for free — no force re-install. Fixes the
  1b workaround ("forces don't survive fork()").
- **`when().do()` de-conscripted.** Removed from the synthesis vocabulary
  entirely — back to being purely a *user post-commit* Python hook
  (`_after_scan_callbacks`, runner.py:221). Synthesis is plant + holds, two
  compiled `call()` passes, never `when().do()`. The "three disguises" thesis
  narrows to "plant and holds are two rung-passes"; `when().do()` is explicitly
  *not* one.

### Preserved vs. changed

- **Preserved:** `core/synthesis.py` — `bool_feedback_rungs` / `copy_hold_rung` /
  `function_rung` are placement-agnostic rung builders. The factory doesn't care
  where in the scan its rungs run.
- **Preserved:** two-roots (deploy/prove scan bare `__plc__`), run_function
  io-gap, "recompute don't record" replay — and replay gets *cleaner*: the
  recorded-nondeterminism passes *are* patches+forces (the external dict = the
  ScanLog on replay); computed passes (plant/holds/plc) recompute.
- **Changed:** runner scan-loop structure (plant post→pre, one `__scan__`
  program of calls), replay kernel (bracketed program → the pass pipeline),
  timing goldens, CHANGELOG (reverses 79c101f/30e9694 — plant is the *input/pre*
  pass, not a post-logic rung).

### Sequencing (green at each step)

- **(a) — LANDED 2026-07-01.** Bool `plant` moved post→pre: `_evaluate_synthesis(
  plant)` runs at the top of `_prepare_scan` *before* `apply_pre_scan`, reading
  the previous commit's command (input-read phase); removed the plant-post eval
  in `_commit_scan`; reordered the `_soft_exec_program` bracket to `[*plant,
  *holds, *user]` so compiled replay matches. Re-pinned 9 timing goldens
  (parity: sustained/on0/out-driven; synthesis_bracket: ton_reproduces_dwell;
  harness: rises/falls/multiple/trigger-match/trigger-leave) to the plant-pre
  phase (committed `Fb` lags one scan; `on_delay>0` response = dwell + one
  input-read scan). Full `make test` 4650 pass / 34 skip / 6 xfail; lint clean.
  **NOT in (a):** analog is *already* pre (`_on_pre_scan` reads the previous
  commit), so folding it into the `__plant__` pass + deleting `_pre_scan_callbacks`
  is deferred to the analog-migration step — the phase correction only needed the
  bool move. The per-scan-*patched*-command replay divergence (compiled drains the
  patch before the plant-first rung, interpreted reads pre-patch) is **step (b)**;
  current parity tests are out()/force-driven, so they agree today.
- **(b) — LANDED 2026-07-01.** Compiled replay now honors the plant-pre phase.
  Root cause: the compiled kernel snapshots all tags into locals, runs the whole
  `[plant,holds,user]` in one `step_fn`, and flushes at the end — so `apply_pre_scan`
  (the drain) ran *before* that function and the plant read *post*-patch (one scan
  ahead of interpreted/forward). Fix = **split the compiled scan into two passes**:
  `compile_kernel(split_after=len(plant))` emits a second `pre_step_fn` for the
  leading plant rungs (one `CompiledKernel`, so referenced-tags/blocks/edge-tags are
  the correct union; each pass renders its own load→run→flush). `CompiledPLC.step`/
  `step_replay` run `pre_step_fn` → `apply_pre_scan` (drain) → `step_fn`, so the
  plant re-reads the previous commit. `split_after` is a general rung count —
  becomes `len(plant)+len(holds)` when holds move pre-drain, no machinery change.
  Verified: compiled == interpreted == forward for a per-scan-patched command
  (was diverging). Tightened `test_synthesis_roots` to sample **transition** scans
  (the old coarse mid-hold sample passed the plant-post bug); confirmed the new
  asserts fail with the split disabled. `make test` 4650 pass; lint clean.
- **(c) — LANDED 2026-07-01** (3 commits: prep `2fa5bcd`, migration `7083e16`).
  Holds are now `__holds__` rungs, not forces. **Prep** (`2fa5bcd`): canonical
  scan order (interpreted evals holds before `apply_pre_scan`; compiled
  `split_after = len(plant)+len(holds)`) + `conditional_hold_rung` multi-branch
  factory (branch guards read the rung-entry snapshot → liveness polarities stay
  mutually exclusive, no mid-scan chaining, compilable). **Migration** (`7083e16`):
  `_ops.py` `_sync_holds` rebuilds a plc's *steady* hold rungs (`copy_hold_rung`,
  every scan) from the registry; `fork_with_holds` installs them on the fork — no
  force re-install (the force-survives-fork footgun is gone). Conditional holds
  stay **coast-only**: `_coast_holding_state` installs them as rungs
  (`_add_conditional_hold_rungs`) — self-releasing guarded copy (1 rule) or
  multi-branch oscillator (liveness) — replacing the reactive `when().do()`
  breakpoints; `cycle_fold_until` is agnostic to *how* the tag oscillates (steps
  scans + observes), so the limit-cycle fold is unchanged. Precedence
  `plant < holds < patches < forces` (steady holds are pre-logic steers, force is
  the hard pin; PILOT holds are on inputs, so steer ≡ pin in practice).
  `investigate.py` two probe installs share one registry (installs rebuild from
  it). Unknown-tag hold keeps a force fallback. Reactive helpers kept for the
  dormant path-replay path (pilot.py:1189 — a follow-up). Full pilot 208, `make
  test` 4653, lint clean.
- **(d) — LANDED 2026-07-01.** CHANGELOG: rewrote the Unreleased bool-coupling
  entry from the (never-shipped) "commits the same scan / one scan earlier"
  phrasing to the final plant-pre model (feedback is an input, lags one scan;
  `on_delay==0` ⇒ next scan; `on_delay>0` ⇒ dwell + one input-read scan). Docs:
  corrected `docs/guides/physical-harness.md` "How bool feedback works" from the
  retired transport-delay heap ("schedules Fb at now+on_delay") to the dwell
  TON/TOF read as a plant input. Two-roots: added
  `test_holds_live_on_overlay_not_program_or_deploy_root` (holds, like the plant,
  never appear in the bare program / deploy kernel). `make test` 4654, lint clean.
  **Deferred follow-ups (not part of this arc):** analog migration (fold analog
  into the `__plant__` pass, delete `_pre_scan_callbacks` + its vestigial `ctx`
  arg); the dormant path-replay reactive holds (`pilot.py:1189`) still use the old
  `when().do()` mechanism.

## Thesis

Harness couplings, PILOT holds, and `when().do()` are **three implementations of
one primitive** — *a guarded write evaluated every scan against state*. pyrung
already has that primitive: **the rung** (`with Rung(guard): <write>`).

So the unification is not "build a fourth overlay system." It is: **stop
hand-rolling the reactive-rule engine three ways, and emit rungs.** The harness
demotes from a runtime to a *rung factory*; holds become rungs; `when().do()`
becomes `Rung(cond) + run_function`. Synthesis is just rungs the factory emits and
the runner *brackets* around the user's program.

```
three disguises of one idea          becomes
─────────────────────────────────────────────────────────────────────────────
bool coupling  (_heap / tick)        TON→TOF rungs                in __plant__
analog coupling(profile_fn + active) Rung(armed): run_function(   in __plant__
                                       profile_fn, ins={cur,en,dt}, outs={fb})
steady hold    (force/forced_holds)  copy(value → input)          in __holds__
self-rel. hold (ConditionalHold)     Rung(goal-unmet): copy(value → input)
reactive when().do() (breakpoint)    Rung(cond): run_function(do, ins, outs)
```

Everything is a rung. The reader / fold / compile / causal subsystems all consume
rungs already, so their support for synthesis is **inherited, not rebuilt**.

## The bracket model

The factory emits up to two subroutines; the runner scans them as bracketing
rungs **on the soft-exec / PILOT fork only**:

```
   call(__holds__)   →   user rungs   →   call(__plant__)
   (pre / input)         (the ship)       (post / plant)
```

Placement *is* the phase — no prior-committed snapshot, no `_dt_override` peek, no
`_on_pre_scan(ctx)` dance:

- **`__holds__` (first rungs)** — runs after input application, before user logic,
  so a held input is visible to the program *this* scan. This is the design's
  "input-targeting rules synthesize the input vector (pre-scan)."
- **`__plant__` (last rungs)** — reads the scan's settled commands/outputs and
  writes feedback into *this* commit; the program reads that feedback *next* scan.
  That is exactly how a real plant works: PLC finishes the scan → drives outputs →
  plant responds → sensors read next scan. **The scan boundary is the plant
  latency.**

### Phase consequence (the one behavior change)

`__plant__`-as-last-rung starts accumulating the scan a command is *active*, so
feedback is **one scan earlier** than 2c's read-lag executor. The dwell semantics,
glitch suppression, and `on_delay`/`off_delay` counts are unchanged; only the
phase shifts. Restated cleanly: **feedback is visible to the program one scan after
the command** (`on_delay == 0` ⇒ next scan the program runs — for free, from the
boundary). The characterization oracle (`test_harness_coupling_parity`) gets
re-pinned to *program-visible* timing; this is the design's sanctioned "option
(a)," and it is the *more* physical model (2c's extra read-lag scan was the
artifact).

## `run_function` closes the last gap

The only action that is not a stock instruction (`copy`/`calc`/timer) is opaque
Python — the analog `profile_fn`, a sandbox probe. pyrung already has the bridge:

```python
run_function(fn, ins={name: tag/value}, outs={key: tag})   # _reads=_ins, _writes=_outs
```

Because `ins`/`outs` are **declared**, the *dataflow is visible* to PDG, fold,
trace, and causal even though the *body* is opaque. So an analog coupling as a
`run_function` rung still participates fully in dependency analysis (En→Fb is
declared); only the numeric curve is hidden, and that is a **uniform compile-time
IO-gap** (`has_io_gaps`), not a bespoke mechanism.

Net: there is **no execution-layer escape hatch left.** `when()` as a runner
breakpoint survives only as live-debug / user sugar — *synthesis never touches it*.
The single irreducible residue is real external hardware / `pyrung live`, where
there is no `fn` to emit at all — the permanent home of `sandbox`, on a different
axis (no model exists) than anything here.

## What comes free (because rungs are what these consume)

| subsystem | inherited support |
|---|---|
| **reader** (`how(Fb>=x)`) | `walk_instructions(__plant__)` yields the AccProfiles; the bespoke `_analog_profile` adapter + `coupling_profiles()` special-case dissolve |
| **fold** | `_collect_acc_sources(__plant__)` finds the timers natively; the dt knob advances them in-scan — the 2c `coupling_acc_specs()` injection **and** the `_dt_override_for_next_scan` peek delete |
| **compile** | `compile_kernel` compiles bool timers natively (interpreted == compiled by construction); opaque `run_function` is the documented IO-gap |
| **causal** | synth rungs are real rungs → `cause()` descends them; feedback attribution sharpens from "harness patch" to "the coupling timer, driven by its command" |

## Two roots — never emitted for deploy or prove

Synthesis is **plant-model + PILOT scaffolding**; it is injected only on the
soft-exec / PILOT fork. Two compilation roots, one discipline:

- **soft-exec root** (interpreted runner; `compile_kernel` for `how()` domain
  inference and compiled replay; coast forks): scans `__holds__ + user + __plant__`.
- **deploy root** (Click ladder export, **CircuitPython / P1AM codegen**) and
  **`prove`**: scan the **user program bare**. The brackets are never emitted to a
  controller (a real PLC has a real plant) and never reach the prover (feedback
  stays free/nondeterministic — a sound over-approximation; do **not** re-introduce
  the deleted `seeding.py` OOM trap by feeding coupling profiles into prover
  seeding).

This is the same "two roots" that already keeps the old separately-installed
harness out of deploy and prove — now structural, because the brackets are a
property of the soft fork, not of the user's `Program`.

## Replay — recompute, don't record (the model inverts)

The ScanLog records *nondeterminism* (patches, forces, I/O, RTC, dt) — **not** rung
writes, which replay re-derives by re-running the logic.

- **Old (patch) model:** harness feedback was a `plc.patch(...)` → recorded
  nondeterministic input → replay injected it, harness absent. "Recorded, harness
  deactivated."
- **New (rung) model:** synthesis is logic → its writes are **recomputed**, not
  recorded. The ScanLog records the *commands the synthesis reads* (pilot/user
  patches+forces); replay re-runs `__holds__`/`__plant__` to regenerate feedback.
  **Keep the synthesis enabled on replay.** Fewer ScanLog entries; provenance
  sharpens.

The boundary, which maps onto a real determinism guarantee:

| synthesis kind | interpreted replay | compiled ReplayKernel |
|---|---|---|
| bool `TON/TOF`, `copy`, `calc` | recompute | **recompute** (compiles) |
| opaque `run_function` (analog) | recompute (pure fn) | `has_io_gaps` → **record the output via `record_io_drain` and inject via `replay_io`**, skip that rung |

So **compilable ⇒ recompute; opaque ⇒ record-via-io-gap.** The opaque case rides
the *existing* I/O record/replay machinery, not a harness-specific deactivation —
the "deactivate the harness on replay" concept disappears entirely. The io-gap line
is the honest divide between "we can re-derive this" and "we must trust the
recording" — the same line as `sandbox`.

## What of 2c survives vs. is replaced

- **Survives (becomes `__plant__` contents):** the real `TON`/`TOF`, dwell
  semantics, glitch suppression, seeding-to-steady, the `force`-overrides-synthesis
  interaction (proven).
- **Replaced:** `_tick_bool_timers` hand-execution → rungs; `coupling_acc_specs()`
  injection → native `_collect_acc_sources`; the `_dt_override` peek → native dt;
  `cb(ctx)` → not needed; `pending_count`/`_profile_couplings.active`/`forced_holds`
  → the brackets *are* the registry; the read-lag goldens → program-visible phase.

## Build order (green at each step)

1. **Programmatic rung/subroutine builder** — the factory's ability to construct a
   `Program`/subroutine from `Physical`/`link`/hold declarations (timer rungs, copy
   rungs, `run_function` rungs). Standalone, unit-tested; no behavior change.
2. **Runner bracket hook** — scan optional `__holds__` (pre) / `__plant__` (post)
   subroutines each scan, soft-exec path only, behind an attribute. Test: a
   hand-built `__plant__` TON reproduces the dwell.
3. **Migrate bool couplings** → factory-emitted `__plant__`; re-pin the parity
   oracle to program-visible timing. Deletes `_tick_bool_timers` /
   `coupling_acc_specs` / peek. Burner + fold + pilot green.
4. **Migrate analog couplings** → `Rung(armed): run_function(...)` in `__plant__`;
   drop the patch tick + `active` flag + the analog monitor. Reader re-points to
   `walk_instructions(__plant__)`.
5. **Migrate holds** → `__holds__`; `forced_holds` dissolves; `fork_with_holds`
   attaches the subroutine (survives fork as a program reference — no force
   re-install). Self-releasing holds = guarded rungs (what `corrections.py` wants).
6. **Deploy/prove exclusion tests** — assert the brackets never appear in Click or
   CircuitPython codegen output, and that `prove` scans the bare user program.
7. **Replay** — confirm bool synth recomputes (interpreted == compiled); analog
   `run_function` rides the io-gap record/replay path; round-trip tests.

## Open questions / risks

- **The bracket hook** is the load-bearing new runner surface — it must scan the
  subroutines under the *same* computed dt and *single* commit as the user rungs
  (so the dt knob and history stay native). Smaller than a two-program scan mode
  (reuse `call()` evaluation), but it is the piece to get right.
- **Force vs. hold-rung ordering** — `force` applies at the pre/post-logic force
  passes; a `__holds__` rung writes at the top. Decide precedence (proposal: keep
  `force` as the hard user pin that wins; `__holds__` is PILOT's steer below it).
- **Goldens migration** — re-pin `test_harness_coupling_parity` to program-visible
  timing; document the one-scan phase change in CHANGELOG (it refines the 2c dwell
  entry, same release).
- **Factory rung builder** — building rungs/subroutines from data (not the DSL
  context managers) is the main genuinely-new code; everything else is deletion +
  rewiring onto existing rung-consuming subsystems.
