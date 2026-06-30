# Handoff — Coupling/hold executor refactor (pilot synthesis overlay)

You are picking up the **executor half** of the coupling/`when().do()` unification
in `pyrung`. The reader half and the foundations are already committed; your job
is to lower the harness's private execution machinery onto public pyrung
primitives. **Read `scratchpad/coupling_when_do/DESIGN.md` first** (the full design
+ a LANDING section with every decision), and the memory file
`project_coupling_when_do_unification.md`. This doc is the operational handoff.

## North star (do NOT re-litigate — these are decided)

- **Clean design + every PILOT fork-action reproducible by public pyrung
  primitives.** The harness demotes from a private runtime to a *factory* that
  emits a public **synthesis overlay** (run before each program scan): holds +
  couplings as `when().do()` rules and real timer rungs.
- **Bool couplings = dwell.** Feedback responds to a *sustained* command, with a
  one-scan response floor (`on_delay==0` → Fb on the next scan; the current
  `max(1, on_delay_scans)` timing is already right). A sub-`on_delay` glitch must
  **not** fabricate Fb. Today's transport-delay `_heap` leaves Fb stuck-True from a
  glitch — that is a **correctness bug** this refactor fixes (CHANGELOG-worthy).
- **No new primitive.** Not `.after()`, not `.synthesizing()`. Bool delay = a real
  TON/TOF; analog = `when().do()`; holds = `force`/`when().do()`.
- **Forces do NOT survive `fork()`** (deliberate — keeps replay/history forks
  unpolluted). Interventions are re-installed per fork via the seam
  `_ops.py::fork_with_holds`. That seam is where the overlay install grows.

## Already landed — DO NOT redo (all on `dev`)

| commit | what |
|---|---|
| `27a13d7` | **Reader (1a)**: `Harness.coupling_profiles()`/`_analog_profile()` → analog coupling exposes an `AccProfile`; `iter_profiles`/`resolve_profile` take optional `harness=`. `accumulating.py` adds `KIND_APPROACH` + `_NoDone`. |
| `646512a` | **Planner (1b)**: `how(Temp>=5.0)` solves + replays. Coast leaf attaches the coupling driver as a steerable sibling; terminal let-run coasts on the predicate + re-installs steady holds. |
| `cb1442b` | **Seam (2a)**: `fork_with_holds(source, forced_holds)` — the single fork+re-install seam (the overlay-install point). |
| `7c6ddd1` | **Parity oracle**: `tests/core/test_harness_coupling_parity.py` — pins per-scan feedback; the glitch test is `xfail` and FLIPS to `xpass` when 2c lands. |
| `1c3831e` | (other agent) `pilot/cyclefold.py` — limit-cycle fold. |
| `a8d1dc3` | cyclefold wired into `_coast_holding_state` (orthogonal to you, but it reads the fold context / harness, so keep it green). |

## Your increments

Recommended order: **2c → 2b → 2d → 2e** (2c is cleanest: dwell is fully specified,
parity oracle is ready, reader comes free, the timer is fold-native; 2b carries a
phase decision; 2d/2e build on both).

### 2c — bool coupling → real TON/TOF (dwell)  [START HERE]

- The factory emits a real **TON** (rising / `on_delay`) and **TOF** (falling /
  `off_delay`) into the synthesis overlay, writing the Fb input. Dwell semantics:
  En must be sustained for `on_delay`; a glitch resets the accumulator and Fb never
  rises. Keep the **1-scan floor** (`on_delay==0` → next scan; a bare TON with
  preset 0 latches *same* scan, so the synthesis phase / emission must add the +1).
- **Retire** `_heap`, `_ScheduledPatch`, and the bool branch of `_make_en_callback`
  / `_drain_due` / `_on_pre_scan`.
- **Reader comes free**: a bool coupling that *is* a TON is walked by
  `walk_instructions`, so its `accumulating_profile()` is the emitted timer's own
  method — no adapter. (Only analog needed the bespoke adapter; already done.)
- **Fold**: retiring `_heap` removes the `_harness_nearest_scan` bool bound, but a
  real TON is an accumulator the runner fold + cyclefold handle natively via
  `acc_names`. Verify `_ensure_fold_context()` picks up the emitted timers.
- **Parity**: `test_bool_glitch_is_suppressed_under_dwell` (xfail) must flip to
  **xpass**; `test_bool_sustained_rise_and_fall_parity` and
  `test_bool_on_delay_zero_is_next_scan` must stay **green** (timing unchanged).
- Twin/parity exposure → run `make test-parity` and the twin suite; CHANGELOG.

### 2b — analog coupling → `when().do()`

- Replace `_tick_analog_with_provenance` + the analog branch of `_on_pre_scan` with
  one reactive rule per coupling: `plc.when(<enabled>).do(<patch Fb = fn(cur,en,dt)>)`.
- **Activation latch matters**: today `coupling.active` (set by the En-edge monitor)
  gates the tick — without it, ticking from scan 0 with `en=False` decays Fb *below*
  its rest value (`_thermal(0,False,dt) < 0`). The rule's guard must reproduce this
  (guard on `active`, keep the monitor; or a "still settling" guard).
- **PHASE DECISION (open — needs a call):** `when().do()` fires **post-scan**, but
  the analog tick is **pre-scan** today (Fb computed before the program reads it,
  using this scan's `dt`). Moving to post-scan introduces a **1-scan lag** — invisible
  to a `>=` margin check, but it changes exact per-scan values and the fold's
  crossing arithmetic, and it would change `test_analog_ramp_and_decay_parity`'s
  golden `[0.0, 0.1, …]`. Either (a) accept the lag and update that golden with a
  documented rationale, or (b) keep analog execution on pre_scan and only
  restructure it as a rule. Decide before coding.
- **Fork propagation**: `fork_onto` currently re-appends `_on_pre_scan`; it must
  instead re-install the `when().do()` rules (and add eager-first if you go
  post-scan). Track rule handles like `_monitors`; remove in `uninstall()`.
- **Fold**: a `when().do()` that patches Fb each scan breaks the plateau each scan →
  no folding through the ramp = same as today (the fold already refuses active
  profiles, `fold.py:~1453`). Confirm cyclefold/fold still bound correctly.

### 2d — holds dissolve into overlay rules

- `forced_holds` becomes a view over installed overlay rules. A steady hold stays a
  `force` (pin); a self-releasing hold = `when(<goal unmet>).do(input := value)`
  (strictly more expressive than the unconditional `forced_holds`, and what the
  corrections layer wants — see `pilot/corrections.py`).
- The install point is `fork_with_holds` (2a) — one seam installs the overlay
  (holds + couplings) onto each fork. The precedent already exists:
  `_ops.py::_install_reactive_holds` lowers `ConditionalHold` to
  `plc.when(...).do(...)` (line ~155).

### 2e — compiled-surface parity

- The factory emits the overlay into the **shared compilation unit** (the program,
  or a sibling synthesis program) that **both** `compile_kernel` (from
  `circuitpy.codegen`) and the interpreted runner consume — NOT a runner-side
  `install()`. Today `compile_kernel(self._program)` never sees the
  separately-installed harness (`runner.py:~1122`).
- **Two roots, one compiler**: deploy compiles `program` only (no overlay shipped
  to a controller); soft-exec (replay / `how()` domain inference / coast forks)
  compiles `program + overlay`. Bool-as-TON compiles natively; analog
  `when().do(profile_fn)` is arbitrary Python → `has_io_gaps` → interpreted
  fallback (acceptable — analog feedback + holds are plant-model scaffolding,
  never deployed).
- `make test-parity` should assert interpreted == compiled scan-by-scan for a
  bool-coupling scenario.

## Verification (run for every increment)

- `make test-pilot` (currently 202 pass / 31 skip / 3 xfail) — the regression gate.
- `tests/core/test_harness_coupling_parity.py` — the executor parity oracle (the
  glitch xfail flips on 2c; everything else stays pinned unless deliberately changed).
- Burner anchors: `scratchpad/burner/sample_pilot_events.py` (cold) and
  `pilot_rotate_liveness.py` (pre-positioned) must still reach `y_BurnerLoop=True`
  with the same verdicts. Run via `uv run python …`; grep `reached: True`.
- `make test-parity` + twin for 2c/2e (codegen/compiled exposure).
- `make lint` (ruff + ty); `make` for the full pass.

## Subsystem safety (already verified — preserve these invariants)

`prove/`, `causal/recorded.py`, `scan_log.py` have **zero** harness references.

- **prove/ BFS — untouched.** It proves the *program only*; feedback tags are free
  inputs (sound over-approx). Keep the overlay out of prove (same "two roots" as
  deploy). **Landmine:** do NOT feed coupling profiles into prover seeding to
  constrain feedback — that's the already-deleted `seeding.py` OOM trap (see memory
  `project_how_analog_step_domain`).
- **causal-replay round-trips** (feedback recorded as ordinary state deltas). The
  one change: attribution of a feedback move shifts from "harness" to "the coupling
  timer/rule, driven by its command" — more precise, but `cause()` output for
  feedback tags changes → update causal tests.
- **how()'s own BFS**: bool-as-timer rides the existing timer done-bit /
  threshold-vector absorption (a win). Analog Fb **must** ride threshold-vector
  crossing absorption or the continuous domain OOMs the BFS (same `seeding.py`
  lesson; the AccProfile already carries the crossing thresholds).

## Key file map

- `core/harness.py` — the factory. `_BoolCoupling`/`_ProfileCoupling`/`Coupling`;
  `_make_en_callback` (monitor → `coupling.active` + `_heap` schedule, `delay_scans
  = max(1, ceil(delay_ms/dt_ms))`); `_on_pre_scan` → `_tick_analog_with_provenance`
  (analog tick = `fn(cur,en,dt)`); `_heap` of `_ScheduledPatch`; `install`/
  `fork_onto`/`uninstall` (lifecycle — fork_onto re-creates the harness on a fork);
  `coupling_profiles()`/`_analog_profile()` (the reader); `_profile_registry` +
  `@profile`.
- `core/instruction/accumulating.py` — `AccProfile`, `KIND_ON_DELAY/OFF_DELAY/
  APPROACH`, `_NoDone`. `OnDelayInstruction.accumulating_profile()`
  (`timers.py:139`) is the shape to mirror.
- `core/fold.py` — `profile_fb_names` exclusion, `_harness_nearest_scan` (~1151),
  the `any(c.active for c in _profile_couplings)` fold-refusal (~1453).
- `core/runner.py` — `_BreakpointBuilder.do` (220, post-scan via
  `_evaluate_breakpoints` ~2735/2770); `force` vs `patch`; `_pre_scan_callbacks`
  (~644); `CompiledKernel`/`compile_kernel` usage (~1119/1543).
- `core/kernel.py` — `CompiledKernel` (compiled `step_fn`) + `ReplayKernel`.
- `core/analysis/pilot/_ops.py` — `fork_with_holds` (the seam),
  `_install_reactive_holds` (the `ConditionalHold`→`when().do()` precedent),
  `_coast_holding_state` (now cyclefold-aware).
- `tests/core/test_harness_coupling_parity.py` — the parity oracle.

## Working-tree note

Commit only the exact files you change (`git add <paths>`, never `git add .`,
never `git stash`). There may be other agents' uncommitted work in the tree
(`scratchpad/burner/*`, example tests). Conventional Commits; end messages with the
`Co-Authored-By:` trailer.
