# Couplings as `when().do()` + `accumulating_profile()` — design sketch

## STATUS

- **Increment 1a — LANDED 2026-06-30.** Reader plumbing, zero behavior change:
  `KIND_APPROACH` + `_NoDone` (accumulating.py), `Harness.coupling_profiles()` /
  `_analog_profile()` (harness.py), optional `harness=` on `iter_profiles` /
  `resolve_profile` (accumulators.py, default `None` = existing callers unchanged).
  Tests: `tests/core/analysis/test_pilot_coupling_profile.py` (5 green); full pilot
  suite 180 pass. `resolve_profile("Temp", prog, harness=plc._harness)` →
  accumulator-match, `scans_to_eject == 500` analytic, `advance` reads `Enable`,
  nonlinear → empirical.
- **Increment 1b — LANDED 2026-06-30** (commit 646512a). `how(Temp>=5.0)` with the
  driver *not* in the goal now solves and replays. Coast leaf attaches the driver
  as a steerable *sibling*; prereq/command split broadened to `_is_coast`; terminal
  let-run coasts on the *predicate* and re-installs steady holds on its coast fork
  (forces don't survive `fork()` — the core bug); let-run step records the steady
  driver hold so the path replays. Pilot suite 196 pass, prover 555 pass; 50
  `test_walk_*` failures are pre-existing (deprecated engine, disabled).
- **Next — executor refactor:** bool couplings → real TON/TOF (dwell), `_heap` /
  `forced_holds` / `_pre_scan_callbacks` dissolve into the synthesis overlay,
  compiled-surface parity. No new primitive.

## LANDING (current conclusion)

Driven by the goal: **clean design + every PILOT fork-action reproducible by
public pyrung primitives.** Where it lands:

- **One declaration, three views.** A `Physical`/`link` coupling is, like a timer:
  a runtime rule (executor), a static `AccProfile` (reader), and a factory that
  emits the rule. The **harness demotes from a private runtime to a factory** that
  emits a public *synthesis overlay* run before each program scan.
- **Private machinery dissolves:** `forced_holds`, the harness `_heap`, the
  `_pre_scan_callbacks` special path, and the `_harness_referenced_names`
  exclusion all become *installed public rules* the trace can read.
- **Reader is the shippable payoff** (Axis 2): add `accumulating_profile()` to
  couplings + one edit to `iter_profiles` (W4). Then `how(Temp>=5)` /
  `how(MotorRunning==True)` resolve by *reading*, through the existing accumulator
  resolver. **The Temp<-Enable case is solved statically** — which closes the
  original sandbox thread: once couplings are readable, sandbox is never reached
  for them.
- **Phase question: closed.** No `.synthesizing()` tag. Post-commit of scan N ==
  pre-scan of N+1, so one phase + an eager first-assert at install (already done
  in `_ops.py:158`) is the synthesis pass. Time doesn't bite: coupling/hold
  guards read stored tags, not clocks; `dt` is fixed on soft forks; pre/post and
  the timestamp tick are *one* knob — fix the tick convention once.
- **Bool coupling semantics — DECIDED: dwell.** "Feedback responds to *sustained*
  commands" was the intended design; today's fire-and-forget `_heap`
  (transport-delay) is an implementation accident that drifted from it. So:
  - A bool coupling emits a real **TON** (on_delay) / **TOF** (off_delay) into the
    overlay. Reuses an existing primitive; the accumulator is a real register
    (**W1 vanishes**); **no `.after()`, no `.synthesizing()`, no new primitive.**
  - Its `accumulating_profile()` is the emitted **timer's own** method — *zero new
    reader code* for bool couplings. Only the **analog** coupling needs a bespoke
    `accumulating_profile()` adapter.
  - This is a **correctness fix, not just cleanup**: a sub-delay command glitch
    currently fabricates feedback that was never supposed to exist. CHANGELOG-worthy.
    Guard the migration with twin/parity — a break there is most likely the
    *unintended* behavior surfacing.
  - `feedback="deadtime"` stays available as a per-coupling opt-in for genuine
    signal dead-time (the minority case).
- **Compiled-surface parity (REQUIRED).** PILOT runs on more than the interpreted
  runner — `how()` compiles a `CompiledKernel` for domain inference
  (`runner.py:1119`) and coast/history use compiled replay. The overlay must
  produce identical scan-by-scan results on every surface. This is a *third*
  argument for emit-a-real-timer: see the section below.
- **Residue:** real hardware / `pyrung live` / deliberately black-boxed components
  keep `sandbox` as the only oracle. Everything on a soft fork is primitives.

**Sequencing:** reader first (payoff, low risk: analog adapter + edit W4 →
`how(Temp>=5)` solved). Then the factory refactor — bool couplings become real
TON/TOF rungs (dwell), `_heap`/`forced_holds`/`_pre_scan_callbacks` dissolve into
installed public rules. No `.after()` to build.

## Compiled-surface parity (requirement)

PILOT executes on more than the interpreted runner: `how()` compiles a
`CompiledKernel` for domain inference (`runner.py:1119-1122`), and history/coast
use compiled replay (`_compiled_replay_supported_kernel`). **Every surface PILOT
drives must produce identical scan-by-scan results with couplings installed** —
and our tests must be able to assert that parity.

This is a third, decisive argument for **emit a real timer** (on top of "no new
primitive" and "W1 vanishes"):

- A bool coupling as a real **TON/TOF** is compiled natively by `compile_kernel`
  (timers are core instructions), so it runs *identically* on the interpreted
  runner and the `CompiledKernel` **by construction**. A private `_heap` /
  `_pre_scan_callbacks` is runner-only — the compiled kernel can't see it, so it
  becomes a `has_io_gaps` fallback or a silent divergence. Real timers make
  compiled-vs-interpreted parity **structural**, not hoped-for.

**The required edit:** the factory must emit the coupling overlay into the
**shared compilation unit** — the program (or a sibling synthesis program) that
*both* `compile_kernel` and the interpreted runner consume — NOT as a runner-side
`install()`. Today `compile_kernel(self._program)` (`runner.py:1122`, `:1543`)
never sees the harness (it's installed separately on the PLC), so couplings would
be absent from the compiled kernel and the two surfaces diverge. The emission
point moves to what both compile.

**Caveat — the non-timer overlay parts.** Analog `when(en).do(fb := profile_fn)`
and reactive holds are runner-side Python; `compile_kernel` can't emit an
arbitrary profile fn. The kernel either represents them or treats them as an IO
gap → interpreted fallback. Acceptable for correctness (analog feedback + holds
are *plant model / pilot scaffolding*, never **deployed** to a real PLC), but be
explicit so parity tests know which scenarios run compiled vs. fall back. This is
also why bool-as-real-timer matters most: the timer is the part that *does*
transpose and compile everywhere.

**Tests (`make test-parity`):**
- bool-coupling scenario runs on BOTH compiled and interpreted (no fallback) and
  must agree scan-by-scan — the real-timer path is the one we can fully verify.
- analog/hold scenarios assert the interpreted (or fallback) path agrees with
  itself across replay.
- twin (`tests/twin/`) is the *other* parity axis: the overlay as a faithful
  plant model (soft program+overlay vs. real program+real plant).

## Subsystem impact: prove / causal-replay / how-BFS

Verified against the tree: **`prove/`, `causal/recorded.py`, and `scan_log.py` have
zero harness references.** That single fact settles both worries:

- **prove/ BFS — untouched.** The prover proves the *program only*; the harness is
  never installed during proving. Feedback tags (unwritten by any rung) are already
  treated as **free/nondeterministic inputs** — a sound over-approximation (it
  explores feedback the plant can't produce, so it never misses a bug). `split_at`
  (prove/CLAUDE.md) is the existing knob to promote a stateful coupling tag to
  nondeterministic. The **same "two roots" discipline** that keeps the overlay out
  of circuitpy deploy keeps it out of prove (prove compiles the *program* root, not
  soft+overlay). **Landmine:** do NOT feed coupling profiles into prover seeding to
  *constrain* feedback — that is the already-deleted `seeding.py` OOM trap
  (memory: project_how_analog_step_domain). Keep prove's feedback-free stance.

- **causal-replay — round-trips; attribution sharpens.** `scan_log` is
  harness-agnostic: synthesized feedback is recorded as ordinary state deltas, so
  replay (interpreted or compiled) reproduces it as data — no re-run of the harness
  needed. The only *change* is causal attribution: a feedback change moves from
  "harness patch" to "the coupling timer/rule, driven by its command" — strictly
  more precise (names Enable as the cause of Temp, which is the whole point), but
  cause() output for feedback tags changes. Flag for causal tests.

- **how()'s OWN BFS — the real action, and a net win for bool.** how() installs the
  harness on its forks (`pilot.py:1134/1567/1635/1709`) and searches with couplings
  ACTIVE, using prove's absorb for state-key projection. Bool-as-real-timer slots
  into the EXISTING timer done-bit / threshold-vector absorption → *more* tractable,
  not less. Analog feedback (continuous Fb) MUST ride threshold-vector crossing
  absorption (the comparisons the program reads on it) or the continuous domain
  blows up the BFS — again, the deleted `seeding.py` is the cautionary tale.

---


Two sketches, two **independent** axes of the same unification. Run both:

```
uv run python scratchpad/coupling_when_do/sketch_scheduled_do.py        # executor
uv run python scratchpad/coupling_when_do/sketch_coupling_accprofile.py # reader
```

## The thesis (what the sketches are arguing)

A harness coupling is **two views of one declaration**, exactly like a timer:

- a **runtime executor** — how a fork *runs* it (Sketch 1)
- a **static reader** — how trace/pilot *reads* it (Sketch 2)

A timer already has both (`OnDelayInstruction.execute` + `.accumulating_profile()`).
Couplings should too. Today they have neither cleanly — the harness has a private
`_heap` executor and is *invisible* to trace (`_harness_referenced_names` is a
special-cased exclusion set, not a readable model).

## Axis 1 — executor: `when(trig).after(N).do(action)` (Sketch 1)

The bool link is a **scheduled crossing**, and the harness already implements it
privately as `_heap` of `_ScheduledPatch`. Lift it to a first-class runner
primitive whose payload is a *callback* instead of a *(tag,value)*. Then the bool
coupling is one consumer of it; `ConditionalHold`/liveness is `when().do()` with
N=0; the analog profile is `when(en).do(accumulate)` with N=0.

Three decisions the running demo pins down:

| | decision | bool link's answer | the other primitive it would be |
|---|---|---|---|
| D1 | edge vs level trigger | rising edge (fire once) | level = a per-scan cascade |
| D2 | cancel vs fire-and-forget | **fire-and-forget** (transport delay) | cancel-on-drop = TON / dwell |
| D3 | fold bounding | expose nearest-due scan | — (same role as `_harness_nearest_scan`) |

D2 is the crux from the conversation: the bool link is **transport delay**, not
**dwell**. `when(En.held(N)).do(...)` would be the *dwell* (TON) cousin — a
different primitive that suppresses sub-delay glitches. The demo proves the
distinction: En drops before the flap fires, and Fb fires anyway.

## Axis 2 — reader: coupling `accumulating_profile()` (Sketch 2)

This is the **load-bearing half** — the executor alone leaves trace blind. Both
coupling kinds become `AccProfile`s the *existing* `accumulators.resolve_profile`
/ `scans_to_eject` consume with no isinstance, no new path:

- **bool link** -> on-delay/off-delay profile, `done = Fb`, shape-identical to a TON.
  `how(MotorRunning==True)` resolves to "hold MotorCmd, coast 200 scans".
- **analog link** -> continuous profile, `accumulator = Fb`. `how(Temp>=5.0)`
  resolves to "hold Enable, coast 1000 scans" (linear) or empirical fork
  (nonlinear). **This is the motivating Temp<-Enable case solved by reading.**

## The three time-slots (why this isn't "one missing primitive")

`when().do()` gains a time dimension, but time enters at three non-interchangeable slots:

| slot | form | is | home |
|---|---|---|---|
| guard-dwell | `when(c.held(t)).do(x)` | TON-as-condition (debounce) | not a coupling; a timer |
| **action-delay** | `when(trig).after(t).do(x)` | dead-time, fire-and-forget | **bool link** (Sketch 1) |
| action-rate | `when(c).do(x.approach(v,r))` | continuous accumulate | analog link (`do` + AccProfile) |

## Independence (confirmed)

The sketches share **nothing**:

- Sketch 2 runs **zero scans** — `resolve_profile` + `scans_until` are pure reads.
- Sketch 1 reads **no profile** — it schedules opaque callbacks.

A coupling exposes both, but neither calls the other. Axis 2 lands with the
harness's current `_heap` untouched and immediately gets `how(Temp>=5)` solved;
Axis 1 lands as runner work without touching trace. They compose but don't depend.

## Deciding principle — reproducibility (Sketch 3)

Stated goals: **clean design**, and **every action PILOT takes on a fork is
reproducible by public pyrung primitives** (no private pilot-only machinery).

Sketch 3 audits PILOT's whole action vocabulary against that bar:

| action | today | public primitive? |
|---|---|---|
| steady hold | `force(tag, val)` | YES |
| conditional/liveness hold | `when(g).do(patch)` | YES — already lowered (`_ops.py:155`) |
| coast / zoom + eject guard | `run_until(fold=True)` + `when(ej).pause()` | YES |
| command pulse | `force`/`patch` (edge) | YES |
| **harness bool coupling** | `harness._heap` | **NO — private** |
| **harness analog coupling** | `harness._pre_scan_callbacks` | **NO — private** |

The *only* non-reproducible part of PILOT's action set is the harness. So
reproducibility **mandates** lowering it to public primitives — Axis 1 is not
optional cleanup, it's required by the goal. This overturns the earlier "do it
second, or never": it's **second, and required.**

### The clean architecture the goal forces

**One primitive family:** `when(guard).do(action)`, with two orthogonal
modifiers — `.after(n)` (scheduling) and a phase tag (`.synthesizing()` =
pre-scan input synthesis vs. post-scan). Plus `force` (pins) and
`run_until(fold=True)` (coast). PILOT's entire fork-action set is points in this
space. The clean phase split (from the earlier thread): **input-targeting rules
synthesize the scan's input vector (pre-scan); everything else is post-scan.**
Holds and couplings both live in synthesis — co-authors of one input vector.

The **harness stops being a private runtime** and becomes a *factory*: it reads
`Physical`/`link` declarations and emits public `when().do()` / `when().after().do()`
rules. `accumulating_profile()` (Axis 2) reads that same rule set for trace. One
declaration, three views (executor rule / static profile / the factory that emits
them) — exactly a timer's `execute` + `accumulating_profile()` shape.

Sketch 3 runs a fork with a self-releasing hold + an analog coupling + a bool
coupling all installed through the same public calls, and asserts PILOT touched
only `{when, do, run_until}`. Goal reached; vocabulary closed.

### The residue (irreducibly non-reproducible)

Two cases have no `f` to replay, so they keep `sandbox` as the only oracle:
real hardware / `pyrung live`, and a deliberately black-boxed component. That —
and only that — is sandbox's honest, permanent home. Everything on a soft fork
is primitives.

**Recommendation:** land Axis 2 first (payoff + smallest, edit W4 only), then
Axis 1 (required by reproducibility) as the harness→primitive-factory refactor.
`forced_holds` and `_heap` both dissolve into installed public rules.

## Wrinkles surfaced by building it (honest, from Sketch 2)

- **W1** bool coupling has no live accumulator register — "elapsed" is in the
  heap. So `acc_now == 0` always: the profile reports the *full* delay from a
  fresh assert. Correct for planning (PILOT newly holds En), an overestimate
  mid-flight. Fixing mid-flight fidelity means materializing the elapsed counter
  as a real register — i.e. running an actual TON as the executor (ties Axis 1
  fidelity to Axis 2 accuracy).
- **W2** a bool coupling yields *two* profiles on the same `done` tag (Fb): the
  resolver must disambiguate by target value (True->on-delay, False->off-delay).
  Sketch 2's `resolve_among(want_value=...)` shows the minimal change.
- **W3** nonlinear analog profile -> `rate_per_scan` raises -> `scans_until`
  returns None -> existing empirical Tier-2 fork. No new fallback invented.
- **W4** the *one* integration edit: `accumulators.iter_profiles(program)` walks
  instructions; couplings aren't instructions. It must also yield
  `harness.coupling_profiles()`. That single change wires both `how()` payoffs.

## Open questions for discussion

1. ~~Is `.after()` worth a first-class primitive?~~ **Resolved by the
   reproducibility goal: yes, required.** The harness's private heap breaks
   reproducibility; `.after()` is the public primitive it lowers to.
2. **Phase:** uniform post-scan `when().do()` (one-scan input latency, simplest)
   vs. a `.synthesizing()` pre-scan tag (matches the harness's current phase,
   the "input vector co-authors" model). Sketch 3 uses the pre-scan tag. Does
   `force` stay as the distinct "pinned steady override," or also become a
   synthesis rule `when(True).do(...)`? (Leaning: keep `force` — a pin and a
   reactive re-assert are genuinely different, both public.)
3. **W1:** accept the from-zero overestimate, or materialize the elapsed register
   — which makes the executor a *real* synthesized TON and folds Axis 1+2 into
   "run an on-delay timer on the fork." The reproducibility goal nudges toward
   materializing (a real TON is itself a public primitive).
4. `KIND_APPROACH` for analog — does the prover's absorb layer need to learn it,
   or does "no done bit" keep it out of the done-abstraction entirely?
5. `iter_profiles` gains a `harness` param, or couplings register as first-class
   pseudo-instructions in the program walk (cleaner if the harness is already a
   rule-factory — the rules *are* the walkable units).
