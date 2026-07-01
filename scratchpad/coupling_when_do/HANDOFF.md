# Handoff — synthesis-is-rungs: what's done, what's deferred

> Written 2026-07-01 after the **plant-pre + holds-as-rungs** arc landed. Read
> `CONSOLIDATION.md` (the PIVOT section + sequencing a–d) for the full design and
> rationale; this file is the *actionable* next-agent scope.

## Status: the pivot is DONE

Steps (a)–(d) are landed and committed on `dev` (all green: `make test` 4654,
`make lint` clean):

- **(a) `188e0f8`** — plant moved post→pre (the input-read phase); feedback is an
  input that lags the command by one scan.
- **(b) `ed70a84`** — compiled replay split into `pre_step_fn` (plant) → drain →
  `step_fn` (holds+user) so compiled == interpreted == forward.
- **(c) `2fa5bcd` + `7083e16`** — canonical scan order (`plant < holds < patches <
  forces`), `conditional_hold_rung` branch factory, and PILOT holds migrated
  force→`__holds__` rungs (survive `fork()`; conditional holds are coast-only
  rungs replacing the reactive `when().do()` breakpoints).
- **(d) `82d5d06`** — CHANGELOG + `docs/guides/physical-harness.md` corrected;
  holds two-roots exclusion test.

The scan is now one uniform pipeline (`plant → holds → drain → user → forces`),
identical on the interpreted runner and the compiled replay kernel. `when().do()`
is out of the synthesis vocabulary entirely — user post-commit hook only.

## Deferred work (the next agent's scope)

### 1. Analog coupling migration — the last non-uniform feedback path (biggest)

**Goal:** fold analog couplings into the `__plant__` pass as `run_function` rungs,
so *all* feedback (bool + analog) is uniform plant rungs — then **delete
`_pre_scan_callbacks` / `_on_pre_scan`** and its now-vestigial `ctx` arg.

**Current analog mechanism (to replace):**
- `core/harness.py`: `_profile_couplings` (list of `_ProfileCoupling`);
  `_on_pre_scan(ctx)` → `_tick_analog_with_provenance()` computes `fn(cur, en, dt)`
  and `plc.patch({Fb: ...})`; `_install_monitors()` puts an **enable-edge monitor**
  that latches `coupling.active` (so the profile ticks only after activation);
  `coupling_profiles()` / `_analog_profile()` is the reader adapter `how()` uses.
- `core/runner.py`: `_pre_scan_callbacks` invoked `cb(ctx)` at the top of
  `_prepare_scan` (registered by `Harness.install` / `fork_onto`). `_on_pre_scan`
  **ignores its `ctx` arg** (reads `current_state`, patches) — that's the vestigial
  `ctx` to drop once this migration deletes the callback list.

**Target:** a `synthesis.function_rung(fn, ins={cur,en,dt}, outs={Fb}, guard=<armed>)`
per analog coupling, appended to `plc._synthesis.plant` (built in
`Harness._refresh_synthesis`, next to the bool `bool_feedback_rungs`). The declared
`ins`/`outs` keep dataflow visible to fold/trace/causal; the numeric body is opaque.

**The historical blocker — and why plant-pre helps.** The `active` latch is set by a
runner **monitor**, and monitors are suppressed under `_replay_mode`, so the analog
tick didn't reproduce on replay ("active-latch-under-replay"). As a **rung**, the
"armed" state must live in the *guard* (a state bit the plant sets/reads), not a
monitor — then it recomputes deterministically like the bool timers. Plant-pre
already made the plant the uniform pre-scan input-read, so analog just joins bool in
the same pass; the remaining work is expressing `active` as rung state.

**io-gap / replay.** A `function_rung` is opaque → `compile_kernel` trips
`has_io_gaps` → `_compiled_replay_supported_kernel` returns `None` → that fork uses
**interpreted** replay (which re-runs the analog via `fork_onto`). That's the honest
"opaque ⇒ record-via-io-gap" divide from `CONSOLIDATION.md`; confirm the split-kernel
path (`pre_step_fn`) degrades cleanly when the plant has an io-gap (it should: whole
kernel → None → interpreted). Do **not** feed analog profiles into prover seeding
(the deleted `seeding.py` OOM trap — see memory `project_how_analog_step_domain`).

**Payoff when done:** `coupling_profiles()` / `_analog_profile()` reader adapter can
dissolve (the reader walks `walk_instructions(__plant__)` natively), `_pre_scan_callbacks`
deletes, and the analog/bool split is gone.

**Verify:** `make test-pilot`, `tests/core/test_harness.py` analog tests
(re-pin if the phase shifts — analog already reads the previous commit, so likely
stable), `make test-soundness`, twin (`tests/twin/`), burner anchors.

### 2. Path-replay reactive holds (`pilot.py:1189`) — small, dormant

`_annotate_reachability_steps` (pilot.py ~1178-1201) still installs
`_install_reactive_holds(fork, holds)` for a `ReachabilityStep.reactive_holds`
replay. It's a **no-op today** ("command paths carry none"), but for consistency it
should build `_add_conditional_hold_rungs`-style rungs like the live coast does.
`_install_reactive_holds` / `_reactive_guard` / `_reactive_patch` in `_ops.py` are
kept **only** for this path — they can be deleted once this migrates.

### 3. OPEN API decision — `when().do()` callback arity

`when(cond).do(callback)` passes the committed `SystemState`
(`runner.py:220`). It is now user-only. **Recommendation:** keep passing
`SystemState` (a reactive hook wants the state that fired it; it's a convenience,
not coupling — the state is `plc.state` anyway), but optionally accept **either
arity** so `do(plain_callable)` and `do(lambda s: ...)` both work (arity-detect via
`inspect.signature`, fall back to passing state for `*args`/builtins). Do *not* drop
`SystemState` — that makes the common "react to what happened" case more verbose.
Decide with the user before touching.

## Gotchas the next agent should not relearn
- **`cycle_fold_until` is oscillation-mechanism-agnostic** — it steps scans via
  `_run_single_scan` and observes snapshots, so it doesn't care whether a tag
  oscillates via a reactive patch or a holds rung. Don't try to "teach" it about
  rungs.
- **Branch guards read the rung-entry snapshot** (`ConditionView`), which is why the
  multi-rule liveness `conditional_hold_rung` stays mutually exclusive with no
  mid-scan chaining. If you build hold rungs any other way, preserve that.
- **`split_after`** in `_compiled_replay_supported_kernel` is `len(plant)+len(holds)`
  — a general rung count. If the pass layout changes, keep it in lockstep with the
  interpreted `_prepare_scan` order.
- **Two roots:** deploy (Click/CircuitPython) and `prove` compile the **bare**
  `self._program`; synthesis (plant + holds) is only on the soft fork's
  `_synthesis`. Keep it that way (tests in `test_synthesis_roots.py`).
