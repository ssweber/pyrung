# Interpreted PLC scan — hot-path profile & target list

**Scope.** Bare interpreted forward scan on the burner Click project (2672 tags,
78 top-level rungs, 32 subroutines). This is the universal interpreter cost that
dominates `PLC.how()`/pilot coast. Measured read-only; no `src/` files changed.

**Method.** `scratchpad/burner/diag_interp_profile.py` (cProfile a warmed
2000-scan loop of `plc._run_single_scan(consume_pause_request=False)`),
`diag_interp_buckets.py` (aggregate self-time into logical buckets +
cumulative phase split), `diag_lever_micro.py` (validate the two top levers on
real burner objects).

## Headline numbers

- **Wall-clock bare scan: 2.935 ms/scan** (no profiler). Matches the prior
  `diag_scan_speed.py` figure (2.9 ms) — this is the universal interpreter cost;
  the coast scan (5.65 ms) is this + coast-specific holds/monitor/recording tax.
- cProfile inflates to 9.38 ms/scan (**~3.3x** — the scan is *call-bound*:
  **~19,700 Python-level calls per scan** for 78 rungs; profiler tax ≈ 0.5µs/call
  ≈ 10 ms, i.e. the entire inflation). Ranking is trustworthy; treat absolute
  `ms/scan*` as profiled (÷3.3 for wall intuition).
- Cumulative phase split (profiled): **execute_program 63.8%**,
  **_commit_scan 34.2%**, `_prepare_scan` 1.8%.

## cProfile top-20 (bare interpreter, N=2000)

### By tottime (self)
```
 ncalls   tottime  cumtime  func
4081540    0.938    2.165   {builtins.isinstance}
1025100    0.828    3.384   pyrsistent/_pmap.py:156 _get_bucket
3443500    0.789    1.056   {builtins.len}
306000     0.681   11.274   executor.py:328 _execute_rung
1025100    0.545    0.545   pyrsistent/_pvector.py:303 _node_for
801020     0.535    1.051   pyrsistent/_pvector.py:51 __getitem__
1416100    0.506    0.859   {_abc._abc_instancecheck}
789020     0.501    2.687   pyrsistent/_pmap.py:162 _getitem
428000     0.485    8.597   executor.py:424 _execute_instruction
100000     0.483    7.119   executor.py:474 _execute_call_instruction
132020     0.415    1.680   pyrsistent/_pmap.py:525 _turbo_mapping
304000     0.399    0.525   context.py:485 _finalize_capture
1416100    0.368    1.228   <frozen abc>:117 __instancecheck__
785020     0.359    3.026   pyrsistent/_pmap.py:172 __getitem__
306000     0.317    8.826   executor.py:377 _execute_rung_body
304000     0.308    0.681   executor.py:302 _new_condition_view
948240     0.303    3.200   {dict.get}
  2000     0.297   12.574   executor.py:261 execute_program
612000     0.280    1.389   {builtins.next}
304000     0.279    0.279   context.py:59 ConditionView.__init__
```

### By cumtime (top of tree)
```
 ncalls  cumtime  func
  2000   19.602   runner.py:2951 _run_single_scan
  2000   12.574   executor.py:261 execute_program
306000   11.274   executor.py:328 _execute_rung
306000    8.826   executor.py:377 _execute_rung_body
428000    8.597   executor.py:424 _execute_instruction
100000    7.119   executor.py:474 _execute_call_instruction
  2000    6.635   runner.py:2686 _commit_scan
142040    3.694   rung_firings.py:194 append        <- recording (commit side)
1025100   3.384   pyrsistent _get_bucket
304000    2.586   rung.py:170 _evaluate_conditions
```

## Self-time by logical bucket (this is the real ranking)

| Bucket | self% | ms/scan* | calls/scan | Note |
|---|---:|---:|---:|---|
| **pyrsistent (PMap/PVector)** | **33.3%** | 3.12 | 5275 | immutable state read+write machinery |
| **builtins** (isinstance/len/hash/next…) | **26.7%** | 2.50 | 9928 | ~11% is ABC-`isinstance`; ~5% is pyrsistent-internal `len`/`hash` |
| **executor.py** traversal | 14.9% | 1.40 | 1557 | recursive `_execute_rung`/`_instruction`/`_call` + NOOP observer calls |
| **context.py** (ScanContext) | 9.3% | 0.87 | 1080 | get/set_tag, capture journal, `_finalize_capture` |
| contextlib (with-blocks) | 3.5% | 0.33 | 608 | `capturing_rung`/`capturing_node` @contextmanager machinery |
| instruction/* dispatch | 3.5% | 0.33 | 406 | `execute()` bodies + resolvers |
| condition.py | 2.7% | 0.25 | 331 | contact/compare `.evaluate` |
| rung_firings.py (recording) | 2.1% | 0.20 | 142 | timeline `append` |
| runner.py prepare/commit | 1.7% | 0.16 | 98 | |
| system runtime | 1.1% | 0.11 | 97 | on_scan_start/end |
| rung.py | 0.9% | 0.09 | 152 | `_evaluate_conditions` loop |

Combining related lines: **pyrsistent state access + its internal len/hash ≈ 38%**;
**ABC-`isinstance` dispatch ≈ 11%** (profiled self: `isinstance` 4.8% +
`_abc_instancecheck` 2.5% + `__instancecheck__` 1.9% + `_abc_subclasscheck` 1.0%
+ `__subclasscheck__` 0.7%).

## Lever validation (micro-bench on real burner objects)

- **PMap read vs dict read** (`diag_lever_micro.py`, burner `state.tags`, 2672 tags):
  `PMap[k]` = **804 ns/lookup**, `dict[k]` = **28 ns/lookup** → **PMap 28.4x slower**.
  ~392 base-state reads/scan go through PMap (condition contacts + instruction
  operands that miss `_tags_pending`) ⇒ ≈ **0.30 ms/scan** wall spent purely on
  the PMap-vs-dict delta.
- **ABC `isinstance` vs `type is`** (real instruction objects): 2×`isinstance`
  (CallInstruction, ForLoopInstruction — both ABCMeta subclasses) = **385 ns/instr**;
  2×`type(i) is Cls` = **30 ns/instr** → **12.9x slower**. At ~214 executed
  instructions/scan ⇒ ≈ **0.08 ms/scan** in the executor dispatch alone (more in
  the resolvers, which `isinstance`-check the `Expression` ABC).

## Ranked target list (inside execute_program)

| # | Hotspot (file:line) | ~wall/scan | What it does | Why it's hot | Optimization hypothesis | Confidence |
|---|---|---:|---|---|---|---|
| 1 | **Base-state reads via PMap** — `context.py:256` `ScanContext.get_tag`, `context.py:72` `ConditionView.get_tag`, ⇒ `pyrsistent/_pmap.py:156/162/172` | ~0.30 ms | Every contact/operand read that misses `_tags_pending` walks a hash-bucket PMap | 2672-tag PMap → 804 ns/read, 28x a dict; ~392 reads/scan | **Incremental plain-dict read mirror**: runner maintains `self._tags_mirror: dict` (and memory mirror), updated with pending on commit (commit already iterates changed keys); ScanContext + ConditionView read from it. Reads become 28 ns. Fork copies the dict once (O(ntags)/fork, not /scan); PMap stays as the history/identity structure. Mirrors compiled path's structural-sharing commit (9ab2f02). **Payoff ~10%; risk: fork + time-travel consistency.** | **code-confirmed** (28.4x measured) |
| 2 | **Per-scan firing capture** — `executor.py:272` `capturing_rung`, `:508` `capturing_node`; `context.py:485` `_finalize_capture`, `:516` `rung_firings` pmap-rebuild; `rung_firings.py:194` append | ~0.25 ms bare, **more in coast** | 78 `capturing_rung` + 148 `capturing_node` @contextmanagers/scan, journal-diff each, rebuild `pmap({i:pmap(w)})` for ~71 firings, intern+append to timeline | @contextmanager is generator-heavy (608 contextlib calls/scan); firing PMaps are rebuilt + hashed + eq-interned every scan (`_turbo_mapping` 66/scan, `pmap()` 146/scan) | During coast/`how()`, per-scan rung+node firing capture largely feeds causal APIs that the forward-sim doesn't query every scan. **Gate `capture_rungs`/`capturing_node` off (or to a lightweight sink) on coast scans**; replace @contextmanager with a plain try/finally class. **Payoff: large for coast specifically.** | **code-confirmed** structurally; coast-need is design judgement (speculative) |
| 3 | **ABC `isinstance` dispatch** — `executor.py:439/452/467` (`CallInstruction`/`ForLoopInstruction`/`ReturnInstruction`), resolvers' `Expression` checks | ~0.10 ms | Per-instruction type discrimination in `_execute_instruction`; per-operand in `resolve_*_ctx` | `Instruction`/`Expression` are `abc.ABC` ⇒ `isinstance` hits ABCMeta Python slow path (385 ns/2 checks) | Add cheap class flags (e.g. `Instruction.IS_CALL = False`, overridden `True` on `CallInstruction`) or a `type(instruction)`-keyed dispatch; check `type(x) is Tag`/attr before ABC fallbacks in resolvers. **12.9x on the check; ~4% of scan. Low risk.** | **code-confirmed** (12.9x measured) |
| 4 | **NOOP observer calls** — `executor.py:343/350/360/437` `observer.begin_*` | ~0.05 ms | `begin_rung`/`begin_condition`/`begin_instruction` fire every rung/instruction even with `NOOP_OBSERVER` | ~500 empty 6–8-arg method calls/scan on the hot path | Guard with `if observer is not NOOP_OBSERVER:` around each `begin_*`. Trivial, zero behavior change. | **code-confirmed** |
| 5 | **ConditionView alloc per rung** — `executor.py:302` `_new_condition_view` → `context.py:59` `ConditionView.__init__` | ~0.05 ms | Allocates a frozen view + `dict(_tags_pending)`/`dict(_memory_pending)` copy per rung (152/scan) | `_new_condition_view` does a `getattr(ctx,"_new_condition_view",None)` miss on a `__slots__` class every rung (AttributeError-catch); eager pending copy even for branchless rungs | Drop the `getattr` factory hook (no ScanContext subclass provides it) — call `ConditionView(ctx)` directly. For rungs with no branches and no `.continued()`, skip the freeze and evaluate conditions against `ctx` directly. **Small; low risk for the getattr part, subtle for the freeze-skip.** | getattr-miss **code-confirmed**; freeze-skip speculative |

## `_commit_scan` note (~1.0 ms bare / 1.45 ms coast, 34% of scan)

`runner.py:2686`. What's inside, by cost:

- **Firing-timeline recording** is the dominant sub-cost (`rung_firings.py:194`
  append cum 3.69 ms* — the single largest cum line in commit). Two parts:
  (a) `ctx.rung_firings`/`ctx.node_firings` properties (`context.py:516/527`)
  **rebuild `pmap({i: pmap(w)})` for every firing every scan** (`_turbo_mapping`,
  `pmap()`, `__hash__`, `__eq__` — the interning path); (b) `append` to the per-rung
  and per-node timelines (`runner.py:2735-2740`). This is **low-hanging for coast**:
  the pmap rebuild + intern is pure recording tax (see lever #2).
- **Structural-sharing commit** — `context.py:553` `ctx.commit` → `evolver.persistent()`
  ×2 + `state.set` + `state.py:38` `next_scan` (a second evolver). Already only
  materializes changed keys (the evolver diff), so this is close to irreducible while
  history needs immutable PMaps. Not low-hanging.
- **Edge `_prev` capture** (`_capture_previous_states`, `runner.py:2640`) — bounded by
  `_edge_tag_names`, small.
- **Monitors/breakpoints** (`runner.py:2780/2793`) — empty in the bare loop;
  `sorted(self._monitors_by_id)` each scan is trivial at zero monitors but grows with
  registrations (coast installs an ejection monitor → part of the coast tax).

## Single highest-leverage lever to try first

**Lever #1 — the incremental plain-dict read mirror.** It attacks the largest and
most rigorously-confirmed bucket (pyrsistent state access ≈ 38% of scan; 28.4x
per-read measured), it is *path-universal* (helps every interpreted scan, not just
coast), and it directly parallels the already-proven compiled-replay win
(structural-sharing commit, 9ab2f02) — the interpreter is paying the exact PMap
read tax that the compiled kernel avoids with plain dicts. Keep the immutable PMap
as the history/fork-identity structure; add a rolling `dict` mirror updated only
with the per-scan changed keys (which `commit` already enumerates), and point both
`ScanContext.get_tag` and `ConditionView.get_tag` at it. The only real risk is
keeping the mirror consistent across `fork()` (copy the dict once per fork) and
time-travel/seek (rebuild the mirror from the target PMap on jump) — both O(ntags)
one-time costs, not per-scan.

**Runner-up for coast specifically — lever #2** (gate per-scan firing capture off
during `how()`), because the coast scan's extra 2.75 ms over the bare scan is
largely the holds overlay + ejection monitor + this per-scan recording, and the
recording half (pmap-rebuild + timeline intern) is skippable when the forward-sim
isn't querying causal APIs on every scan.

## Stake-test outcome (2026-07-02) — lever #1 IMPLEMENTED, faithful, REVERTED

Built the plain-dict read mirror for real (`context.py` `_tags_mirror` on
ScanContext + ConditionView; runner identity-guarded lazy build + roll-forward on
commit). `make test` 4644 pass, `make lint` green — faithful across
fork/seek/replay/DAP/prove (the identity guard self-heals on any out-of-band
`self._state` reassignment). Same-process A/B on the burner:
**ON 2.74 vs OFF 3.00 ms/scan = +8.5%** read-side (`diag_mirror_ab.py`),
matching the stake-test's ~7% projection.

**Reverted anyway — it's aimed at the wrong 12%:**
- +8.5% of the *bare* scan is only ~4.4% of a *coast* scan (5.65 ms), and coast
  is only ~12% of `how()` (the causal/incident layer is ~76%) ⇒ ~0.5–1% on `how()`.
- By construction it misses the `how()` bottleneck: causal/incident replay
  (`analysis/causal/projected.py`) builds **cold** `ScanContext(state)` with no
  mirror, so the 76% path is untouched.
- Memory is a non-issue: 50.8 KB per live fork, shallow (keys/values shared —
  `diag_mirror_memory.py`). The real cost was the standing second-source-of-truth
  coherence invariant in the hottest path, not RAM.

So the pyrsistent **read** lever is real for scan-bound work (twin / sim /
`run_for`) but **inert for `how()`**. The backend-swap side is settled too — see
`persistent_map_migration_note.md`: immutables' PURE fallback reads **3× worse
than PMap**, rpds is dominated.

**Where the real wins likely are — lower in the interpreted stack, not the map.**
Every interpreted-scan lever is capped at ~12% of `how()`, so for `how()` the
target is the causal/incident layer, not the scan at all. For SCAN-bound speed,
the un-tried levers sit *below* the map: #2 (gate per-scan firing capture; replace
`@contextmanager` with plain try/finally), #3 (ABC `isinstance` dispatch → class
flags / `type()`-keyed), #4 (NOOP observer calls), #5 (ConditionView alloc) — the
executor/dispatch/recording machinery, none of which this arc touched.

---
Diagnostics: `scratchpad/burner/diag_interp_profile.py`, `diag_interp_buckets.py`,
`diag_lever_micro.py`; stake-test: `diag_backend_stake.py`, `diag_mirror_ab.py`,
`diag_mirror_memory.py`, `diag_immutables_fallback.py`. `*` = profiled ms (÷3.3 for wall).
