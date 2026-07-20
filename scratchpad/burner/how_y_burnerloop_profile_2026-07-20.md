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

