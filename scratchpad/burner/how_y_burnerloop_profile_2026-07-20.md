# `how(y_BurnerLoop)` profile notes

Profiled 2026-07-19/20 against the then-current dirty worktree.

## Outcome and caveat

The ordinary run finished in 110.2 seconds, but did not reach
`y_BurnerLoop`. It ended `unreachable` at scan 4 after several speculative
10,000-scan coasts.

The cProfile run captured the same dominant path, but hit the driver's
240-second wall budget after 310 seconds. cProfile inflated this call-heavy
workload by roughly 2.8x, so its percentages and call relationships are useful,
but the 110.2-second unprofiled run is the representative user-facing time.

Profile artifact:

```text
C:\tmp\how_y_burnerloop.prof
```

## Where the time went

The profile contained 643,862,729 function calls, of which 634,000,148 were
primitive calls, over 308.5 profiled seconds.

| Operation | Profiled time | Share |
|---|---:|---:|
| `CoastSession.seek` | 260.7 s | 84.5% |
| Ordinary `fold_run_until` | 241.2 s | 78.2% |
| Actual `_run_single_scan` calls | 203.8 s | 66.1% |
| Cyclefold | 18.8 s | 6.1% |
| Backward `trace_back` work | 25.9 s | 8.4% |
| Deviation investigation | 22.5 s | 7.3% |
| Orientation | 13.3 s | 4.3% |
| Initial prover preparation | 9.2 s | 3.0% |

Nested scan cost:

| Scan component | Profiled time | Share |
|---|---:|---:|
| `_prepare_scan` | 101.4 s | 32.9% |
| Ladder execution | 59.2 s | 19.2% |
| `_commit_scan` | 46.8 s | 15.2% |
| Fold `_visible_items` snapshots | 34.1 s | 11.0% |

Most of this is persistent-map/vector traffic. The profile recorded tens of
millions of `pmap`/`pvector` lookups and iterations. In particular,
`fold._visible_items` rebuilds a large visible-state dictionary for every fold
probe.

The three quiet 10,000-scan zooms each took about 25-26 seconds unprofiled.
Ordinary folding was active, but in small jumps:

- 30,200 logical scan IDs advanced.
- 12,090 actual kernel executions occurred.
- 6,041 macro-fold operations occurred.
- About 60% of potential executions were avoided.
- Each macro fold saved only about three kernel executions on average.

This makes ordinary fold probing and scan execution the primary performance
target, rather than cycle detection.

## Existing visibility into folds

`CoastReceipt` exposes:

- `end_scan - start_scan`, which is a usable logical/advanced scan count;
- `real_scans`;
- `folds`.

However, `real_scans` and `folds` are incremented only by the cyclefold branch
of `CoastSession.seek`. The ordinary `fold_run_until` branch does not report its
probe scans or macro folds.

As a result, built-in debug output reported lines such as:

```text
10000 scan-ids, 2 real scans, 0 folds
```

for coasts where the profile showed thousands of runner probes and fold
executions. The receipt fields currently mean "work explicitly counted by the
coast wrapper", not actual kernel work.

Composite receipts also discard the inner fold diagnostics. This limitation is
explicitly asserted by
`TestSettleLandingFolds.test_confirmation_window_folds_scan_ids_still_elapse`.
Tumbler decision skeletons intentionally remove scan IDs, durations, and fold
counters, so the golden skeleton is not a performance diagnostic.

The only trustworthy general mechanism currently in-tree is the test helper
`tests/core/test_fold.py::_count_real_scans`, which wraps
`PLC._run_single_scan`. It counts both ordinary probes and the real execution
inside every macro fold, but it is test-only instrumentation.

## Cyclefold value

Cyclefold itself was effective. A representative large BurnerLoop replay
receipt reported:

```text
31,014 logical scans
914 real scans
3 cyclefold jumps
```

That means:

- 30,100 scan executions avoided;
- 97.1% fewer real scans;
- about 33.9x fewer kernel executions than scan-by-scan.

The profiled prefix contained two equivalent replay coasts. Across all seven
cyclefold calls:

- approximately 62,172 logical scans advanced;
- 1,968 actual kernel scans ran;
- approximately 60,204 executions were avoided, or 96.8%.

Cycle detection itself took only 0.085 seconds. Nearly all of cyclefold's
18.8 profiled seconds was the required real observation and landing scans.
Optimizing `detect_cycle` would therefore not materially improve this drive.

Cyclefold internally collects richer statistics, but they are passed through a
private dictionary. `CoastSession._last_cyclefold_stats` retains only the last
call's values, and composite operations do not aggregate them.

## Recommended diagnostics

Use one runner-level, engine-independent accumulator:

```text
logical_scans
kernel_scans
macro_folds
skipped_scans = logical_scans - kernel_scans

runner_fold:
    logical_scans
    kernel_scans
    macro_folds

cyclefold:
    logical_scans
    kernel_scans
    macro_folds
    scan_by_scan_counterfactual
    saved_kernel_scans
```

`fold_run_until` and `cycle_fold_until` should populate the same accumulator.
Composite coast operations should aggregate it instead of reverting to
dataclass defaults. An opt-in final `how()` diagnostic summary could then
answer both "real versus advanced scans" and "cyclefold versus no cyclefold"
without monkeypatches or cProfile.

## Landing task list

### Landed in the first pass

- [x] Populate ordinary `fold_run_until` work statistics instead of discarding
  its probe scans and macro folds.
- [x] Give both fold engines the same core diagnostic keys:
  `logical_scans`, `kernel_scans`, `macro_folds`, and `skipped_scans`.
- [x] Retain cyclefold's scan-by-scan counterfactual and saved-kernel-scan
  counts alongside its compatibility keys.
- [x] Expose correctly named `CoastReceipt` properties while retaining
  `real_scans` and `folds` for compatibility.
- [x] Aggregate inner ordinary-fold work through composite landing receipts.
- [x] Correct coast debug output to report logical scans, kernel scans, skipped
  scans, and macro folds.
- [x] Stop rebuilding visible-state dictionaries for every plateau and
  post-fold comparison; retain one snapshot and compare later states directly.

### Follow-up visibility

- [ ] Aggregate runner-fold and cyclefold breakdowns separately across every
  composite coast operation.
- [ ] Add an opt-in final `how()` work summary using the receipt totals.
- [ ] Decide when the compatibility names `real_scans` and `folds` can be
  deprecated in favor of the precise names.

### Follow-up performance

- [x] Re-run the full BurnerLoop drive without cProfile. The first post-cleanup
  run completed in 73.6 seconds and reached `y_BurnerLoop` at scan 2,110.
  This is an encouraging checkpoint, but not a controlled comparison with the
  110.2-second unreachable profile run because the search outcome/path changed.
- [ ] Capture a like-for-like quiet 10,000-scan ordinary-fold coast and compare
  it with the 25–26 second baseline.
- [ ] Re-profile after the basic allocation cleanup before changing fold
  heuristics.
- [ ] Use the new counters to evaluate whether larger ordinary macro folds are
  possible without weakening crossing guarantees.
- [ ] Treat `_prepare_scan`, ladder execution, and `_commit_scan` persistent-map
  traffic as a separate optimization pass.

## Working-tree follow-up

A fresh profile on the repaired tree followed the same successful route as the
unprofiled drive and reached `y_BurnerLoop` at scan 2,110. It recorded
364,930,910 calls over 193.1 cProfile seconds (196.8 seconds wall).

The priority changed:

- ordinary `fold_run_until`: 1.58 profiled seconds;
- ordinary visible-state snapshots/comparisons: 0.20 seconds;
- deviation investigation: 143.6 seconds;
- recorded `cause()` replay: 107.4 seconds;
- `_replay_capture_at`: 100.4 seconds, including 4,376 observed target-scan
  executions.

The real BurnerLoop probe then established:

- a bounded replay-capture LRU is not useful: sizes 4–32 avoid only 16 of 4,376
  observed executions;
- those 4,376 executions cover only 1,096 distinct
  `(runner, unchanged tip, target scan)` keys;
- 3,303 executions were repeated across separate `cause()` calls;
- four identical deep `cause(Sts_StateCurrent, scan=1952)` queries each walked
  1,044 historical scans.

Measured landings on the same successful 64-event route:

| Change | Wall time | Observed replay executions |
|---|---:|---:|
| Accurate fold stats + snapshot cleanup | 73.6 s | not probed |
| Reuse the source PDG in reconstructed replay | 69.6 s | 4,376 |
| Skip committing disposable observed replays | 56.0 s | 4,376 |
| Share explicit-scan causal chains across investigation consumers | 35.9 s | 1,199 |

The causal-chain memo retains the much smaller completed `CausalChain`, not
thousands of `ConditionViewCapture` objects, and is cleared whenever PILOT
replaces its synthesis holds.

## Rung-firing comparison proof

The repaired-tree profile attributed 4.35 cumulative seconds to
`rung_firings._same_writes`, across 599,416 calls. A focused benchmark and a
BurnerLoop call-distribution probe in
`scratchpad/burner/benchmark_rung_firing_compare.py` separated profiler
instrumentation overhead from the real opportunity.

The BurnerLoop distribution was:

- 599,416 comparisons;
- 598,715 successful matches (99.88%);
- 467,868 one-write matches (78.05% of all calls);
- 62,288 empty-write matches (10.39%);
- the remaining 11.56% spread across mostly 2–35 writes.

Weighted by that exact distribution, the isolated comparator loop measured:

| Comparator | Time | Improvement |
|---|---:|---:|
| Current per-key `PMap.get` loop | 0.8300 s | baseline |
| Current tiny-map path + native equality for larger maps | 0.5816 s | 0.2484 s |
| Cached plain-dict equality | 0.0601 s | 0.7699 s |

The cached mirror therefore removes about 93% of comparison time, but the
end-to-end ceiling is about 0.77 seconds on the 36.0-second BurnerLoop
(approximately 2.1%), not the 4.35 seconds suggested by cProfile.

`tracemalloc` measured the retained mirror cost at roughly 120 bytes for an
empty pattern, 240 bytes for 1–4 writes, 328 bytes for 8 writes, 520 bytes for
16 writes, and 888 bytes for 32 writes. This supports a small, explicit
active-pattern mirror if the implementation stays local; it does not justify
mirroring every historical or interned pattern.

## Condition-view reuse instrumentation

`scratchpad/burner/probe_condition_view_reuse.py` observed the existing
executor without changing view or capture results. The instrumented
BurnerLoop followed the same successful route and finished at scan 2,110.

Across 1,606,546 normal condition-view creations:

- 761,202 preceding views were exactly equal to the pending scan image
  (47.38%);
- a conservative "any setter invalidates" generation check identified 761,087
  reusable views (47.37%);
- exact dictionary comparison found only 115 additional same-state cases after
  setter activity;
- the existing views copied approximately 6.17 GB of shallow dictionary
  storage cumulatively;
- the generation-safe reusable views accounted for approximately 3.29 GB
  (53.34%) of that copy traffic.

The generation check therefore captures essentially the entire opportunity
without comparing dictionaries or maintaining a cache. A normal rung can reuse
the preceding `ConditionView` when it is in the same execution scope and no
setter has run since that view was created. `continued()` retains its existing,
explicit reuse semantics.

The same run observed 1,610,925 firing-capture finalizations:

- 858,098 journals were empty (53.27%);
- 858,255 returned `None` because there was no effective write (53.28%);
- 77,901 retained firing evidence but filtered its writes to `{}` (4.84%);
- 674,769 returned one or more writes (41.89%).

An immediate empty-journal return can therefore bypass more than half of
`_finalize_capture` calls without affecting the important distinction between
`None` (did not fire) and `{}` (fired, but PDG filtering removed its writes).

## Condition-view reuse decision

The generation-based reuse prototype was not retained. Its wall-time results
were noisy: paired runs ranged from roughly two seconds faster to slightly
slower. One strict CPU-timed pair saved 0.953 seconds (2.64%), which does not
justify adding a condition-snapshot invalidation contract to every tag and
memory setter.

The approximately 3.29 GB figure above is cumulative shallow allocation
traffic, not retained or peak memory. Python's allocator can reuse that storage,
so it is evidence of churn rather than a 3.29 GB memory footprint.

The independent empty-journal fast path remains: `_finalize_capture` returns
immediately when its journal is empty. This avoids pointless dictionary
construction in 53.27% of observed finalizations without adding state or
changing the important `None` versus filtered `{}` distinction.

## Scan-mode CPU attribution

`scratchpad/burner/probe_scan_mode_costs.py` times runner boundaries with
`process_time_ns` and leaves execution results unchanged. On the current
working tree it followed the same 64-event route to scan 2,110 in 35.719 CPU
seconds (36.172 seconds wall).

The additive top-level split was:

| Work | CPU | Share |
|---|---:|---:|
| 5,172 committed PILOT-fork scans | 13.578 s | 38.01% |
| 23 outer `cause()` calls, including replay | 8.547 s | 23.93% |
| Other analysis and control between scans | 13.594 s | 38.06% |

Committed scans broke down into:

| Phase | CPU |
|---|---:|
| Prepare | 0.859 s |
| Natural program execution | 9.203 s |
| Commit | 3.453 s |
| Runner residual | 0.062 s |

The old 5,173 `_run_single_scan` count described committed scans only. Causal
observation executes additional target scans manually, without committing
them. In this run, 2,279 replay-capture requests produced 1,165 cache hits and
1,114 actual observed target executions.

The `cause()` envelope broke down into:

| Work | CPU |
|---|---:|
| Replay-capture envelope | 6.859 s |
| Remaining causal reasoning | 1.688 s |

Within replay capture:

- observed target program execution: 5.922 seconds;
- observed target preparation: 0.172 seconds;
- reconstructed-fork construction: 0.266 seconds;
- three replay-slab fills: 1.281 seconds, including 518 compiled materialized
  steps taking 0.609 seconds.

An observed target's program phase averaged approximately 5.32 ms, versus
1.78 ms for a normal committed scan: observation is about 3.0x slower per
scan. Replay fork construction and state lookup are now secondary.

The largest concrete non-PILOT target is therefore the 5.922-second observed
execution cost. Retaining compact causal evidence—or making the observer
collect only the evidence requested by the current causal hop—has a much
larger ceiling than another context or timeline micro-optimization. General
interpreter dispatch is next (9.203 seconds across committed scans), followed
by state/timeline commit (3.453 seconds). The 13.594-second other-analysis
bucket is primarily the PILOT/proof/control surface currently being changed
separately.

## PILOT/proof/control attribution

A second low-overhead pass split the approximately 38.5% analysis/control
bucket. On the same successful route, 13.344 seconds remained outside committed
scans and `cause()`:

| Boundary | CPU |
|---|---:|
| Cycle-fold control excluding its committed child scans | 4.500 s |
| Trace-tree construction, 172 roots | 3.688 s |
| One-time prover-context construction | 2.188 s |
| Empirical program-write discovery, three calls | 1.391 s |
| Remaining analysis/control | approximately 1.6 s |

The cycle-fold control cost is not cycle detection. Across 5,016 real scans:

- the armed predicates took 0.359 seconds;
- 613 `detect_cycle` calls took 0.188 seconds;
- 75 crossing-surface checks were below timer resolution in aggregate;
- the remaining loop and snapshot work took 3.953 seconds.

`cycle_fold_until` currently materializes a filtered plain dictionary after
every scan:

```python
{k: v for k, v in plc.state.tags.items() if k not in ignore}
```

The BurnerLoop state contains 3,765 tags; only 100 are ignored, so each of the
5,016 scans allocates and fills a 3,665-entry dictionary. A focused
microbenchmark took 0.688 seconds for 1,000 such copies, predicting about 3.45
seconds over the route and closely matching the measured 3.953-second residual.
Precomputing retained names and indexing the persistent map was worse (3.156
seconds per 1,000 copies).

The next high-confidence target is therefore to retain the immutable PMap
snapshots in the cycle ring and give `detect_cycle` the fixed significant-key
set. That removes the per-scan full-map copy and its allocation traffic while
preserving the ignored-tag boundary. It should be benchmarked end-to-end after
the concurrent `pilot/` work lands.

### Retained cycle snapshots

The retained-snapshot change followed the same 64-event route to scan 2,110.
Against the immediately preceding instrumented baseline:

| Measurement | Filtered dict per scan | Retained PMap | Improvement |
|---|---:|---:|---:|
| Total CPU | 34.656 s | 32.203 s | 2.453 s (7.1%) |
| Wall time | 35.071 s | 32.640 s | 2.431 s (6.9%) |
| Cycle control excluding committed scans | 4.500 s | 1.594 s | 2.906 s (64.6%) |
| Unattributed cycle loop/snapshot work | 3.953 s | 0.109 s | 3.844 s |

The net saving is smaller than the copy-only estimate because cycle detection
now reads values from PMaps rather than temporary plain dictionaries:
`detect_cycle` increased from 0.188 to 0.922 seconds. Avoiding 5,016 full
3,665-entry copies still wins comfortably, removes their allocation traffic,
and keeps the change local: the detector accepts an optional fixed significant
key set while preserving its union-of-snapshot-keys behavior for other callers.

The other possible caches were not supported:

- 134 of 172 trace roots repeated the same tag/value, but only nine repeated
  the exact snapshot/tag/value context;
- the three empirical-write calls used different PLC forks and different scan
  intervals, so there were no exact repeated call shapes.

## Interpreter primitive cleanup

The concurrent PILOT work changed the route during this phase, so the
interpreter comparison uses the two adjacent runs that both followed the same
69-event route and finished at scan 909.

The retained changes are deliberately mechanical:

- create condition snapshots with native `dict.copy()`;
- probe pending dictionaries once with `get(..., sentinel)` rather than
  `name in mapping` followed by `mapping[name]`;
- bypass the capture-stack loop when exactly one firing journal is open;
- evaluate `AllCondition` and `AnyCondition` with direct short-circuiting loops
  rather than generator expressions.

| Measurement | Before | After | Improvement |
|---|---:|---:|---:|
| Total CPU | 36.688 s | 35.516 s | 1.172 s (3.2%) |
| Wall time | 37.245 s | 35.748 s | 1.497 s (4.0%) |
| Committed program execution | 10.938 s | 10.594 s | 0.344 s |
| Observed program execution | 5.891 s | 5.391 s | 0.500 s |
| Combined interpreted program execution | 16.829 s | 15.985 s | 0.844 s (5.0%) |

The observer-consumer instrumentation also rejected two more complicated
ideas. `RungRun` evidence initiated almost every historical target replay, so
runs cannot be collected lazily. Disabling the disposable replay's duplicate
normal firing journals saved only a few tenths of a second and was not retained.

## Settled PILOT re-baseline

After the concurrent PILOT fix settled, two consecutive instrumented runs
followed the identical intended route: 64 events, target at scan 2,110, 7,756
committed PILOT-fork scans, and 1,106 actual observed historical target scans.

| Measurement | Run 1 | Run 2 | Average |
|---|---:|---:|---:|
| Wall time | 40.307 s | 40.661 s | 40.484 s |
| CPU time | 39.703 s | 40.047 s | 39.875 s |
| Committed scans | 20.188 s | 20.344 s | 20.266 s |
| `cause()`, including replay | 8.219 s | 7.984 s | 8.102 s |
| Other analysis/control | 11.297 s | 11.719 s | 11.508 s |

The committed scan program phase averaged 13.430 seconds, or approximately
1.73 ms per scan. Observed historical program execution averaged 5.641 seconds
across 1,106 scans, or approximately 5.10 ms per scan. The observer path is
therefore still about 2.95x slower per interpreted scan.

Combined normal and observed program interpretation now consumes approximately
19.07 seconds, 47.8% of total CPU. The causal envelope remains material:

- 2,251–2,252 capture requests;
- 1,106 actual target executions;
- approximately 6.53 seconds in replay capture;
- approximately 1.57 seconds in remaining causal reasoning.

`RungRun` consumers still initiate essentially every capture miss (1,125–1,126
first-on-target requests), while dynamic node reads initiate only four and node
views 25–26. Any observed-execution optimization must therefore retain complete
per-occurrence run evidence; selectively omitting runs is not available.

### Lazy dynamic-read capture decision

A capability-aware replay-cache prototype omitted per-instruction read tracking
from the common runs/views capture and upgraded the cached target with a
read-capable replay only when `_replay_node_reads_at()` requested it.

Both A/B runs followed the identical 64-event route to scan 2,110. The
prototype added 15 observed target executions (1,121 versus 1,106). Against the
two-run settled baseline averages:

| Measurement | Eager reads | Lazy reads | Improvement |
|---|---:|---:|---:|
| Replay-capture envelope | 6.531 s | 6.352 s | 0.180 s |
| Full `cause()` envelope | 8.102 s | 7.899 s | 0.203 s |
| Observed program execution | 5.641 s | 5.555 s | 0.086 s |

The replay-specific saving is only approximately 0.5% of total CPU. Larger
total-time movement came from unrelated committed-scan and analysis variance.
The prototype was therefore backed out: 0.18–0.20 seconds does not justify a
read-capability cache contract and extra target executions.

