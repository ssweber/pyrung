# jit speed catalogue (2026-11-09, dev becdd32)

The jit is pyrung's guarded scan loop over the compiled kernel. It runs a traced path of the
kernel scan after scan while that path's guards hold. The trio is the speed benchmark. It is
three tests in `tests/tumbler/test_pilot_golden_skeleton.py` (`held_dry_route`,
`first_door_incident`, `internal_route_progress`). Each one drives `how()` on the tumbler program.

Reference points:
- The old fold ran the trio in about 63 s. The fold was the earlier jump-ahead engine, deleted
  on 2026-11-03 (c1fcc76) because it skipped scans without a proof.
- dev ran the trio in 155-161 s on 2026-11-07, after the fold was gone.
- dev runs it in about 71 s on Python 3.14 today.

A word on the numbers. Trio times before the evening of 2026-11-08 may mix Python 3.13 and
3.14, and many ran on a loaded machine (hold-arc ledger, 2026-11-08 19:21). Each saving below
is a before-and-after pair from one branch on one setup. The absolute times from different days
do not compare. "Net lines" counts `src/` only, as added minus deleted at the time of landing.

## 1. Done

### Landed on the trio

| # | Change | Commits | Trio saved | Net src lines |
|---|---|---|---|---|
| 1 | Kernel code shared across runners, and a faster walk for the tags a compiled replay seeds | 903b6dc, 42d4f93 | about 18 s (161 -> 143) | +143 |
| 2 | That walk stops at the `Program` | eaa2a4f | about 2 s (146 -> 144) | +1 |
| 3 | The stop predicate is asked only after a scan that changes what it reads | d172e42 | about 22 s (144 -> 122) | +22 |
| 4 | A repeating cycle of records is stored as one timeline entry | fe60cb0, 803b4cf, 9ca9fed | 0 s on its own | about +400 |
| 5 | A repeat whose whole numbers step each cycle, then the cycle prediction and the cycle jump | bc06543, 56c0fa5, 0438d72, dbc899f, 2cb19d2, c741e6f | about 22 s (122 -> 113 -> 100) | about +1,550 |
| 6 | Jump tuning | df5ea8b, 16a8f40, 7b47fe4 | about 4 s (100 -> 96) | +35 |
| 7 | how()'s prover facts are built once per program, from only the passes how() reads | 5a9dfe0, 7a83568, cd1e389, 634a981 | about 18 s | +121 |
| 8 | A loop run compiles or traces only with room to pay for it | 2e1b743 | about 0.1 s | +13 |
| 9 | A one-scan tag run goes into the records as one push | 9a063b8 | 1.6 s | +6 |
| 10 | A column that takes turns every scan is recorded as one run | 6caa5f0, b7c470a | 3.4 s | +77 |

Notes on each row:
1. Compile time fell from 15.8 s to 4.6 s and trace time from 7.4 s to 5.1 s over the 17 kernel
   compiles. Most of the old compile cost was the walk for materialized tags.
2. One compile fell from 4.65 s to 1.53 s. It also made the seeded tags the same whether or not a
   program graph was built first.
3. Of 629,053 stop predicate calls, 628,843 saw the same values as the call before. Loop time
   fell from 29.3 s to 11.1 s.
4. This entry alone saved nothing. The tumbler's timer values step by 1 each cycle, so a plain
   repeat never matched. Rows 5 and 10 need it.
5. With predictions off the trio took 113 s, and with the jump on it took 100 s. Loop firings fell
   from 702k to 210k. The argument that the jump is exact is in the `cycle_jump.py` docstring.
   That file is 876 lines today. Later rounds trimmed the record code by about 460 lines (a8ace96, b911ad6).
6. df5ea8b walks a repeating run of segments once. 16a8f40 declines a prediction that would ask
   the stop predicate on most scans. 7b47fe4 keeps at most eight compiled iterations per path.
7. This is not a jit change. It was measured on dev before the jit work landed (181 s -> 141 s).
   The prover part of that is about 18 s. A cold build fell from 8.1 s to 4.0 s, and the
   repeated builds fell from 6.9 s to 0 s.
8. The first try (9d9d547) was reverted (5c8756d) because it moved how() answers. how()'s budget
   charged a loop scan and an interpreted scan differently. It came back after 8a623d9 made how()
   charge a scan the same in either engine. The trio barely moves, and a short how() wait is
   8-12% faster.
9. A one-scan run takes `_push` instead of the general run path.
10. `append_tag_run` calls fell from 1.89M to 318k on internal_route. The fix round also made these
    records keep `1`, `1.0` and `True` apart, as stepping does.

The sum of these savings is about 91 s. That matches the fall from about 161 s to about 71 s
within the noise.

### Landed, but not for the trio

| Change | Commits | Effect | Net src lines |
|---|---|---|---|
| jit counters (`PYRUNG_JIT_STATS`) | 942ef6b, a1bc005 | the measuring tool, no speed | +11 plus fixes |
| The tracer iterates a constant `str` (a copy to text) | 7d6cd5c | the follow_extra loop share rose from 11% to 50% | +10 |
| A loop run compiles its kernel at its first attempt | e1f83f3 | a short how() wait is 5-7% faster | +3 |
| Seven interpreted-scan commits (jit-wins) | c1d5b13..0f966da | an interpreted scan fell from 0.202 to 0.147 ms | +42 |
| `SystemState` as a slotted mapping | 2fd1b74, 61ed971, 5d8389b | an interpreted scan fell from 159 to 115 us (N=3) | +191 |
| A program naming no `randint` tag skips the site walk | 3dc6b7e | not timed | +3 |
| Synthesis reads each guard's tags once per build | 293720c | not timed | +3 |

The jit-wins and `SystemState` work landed on 2026-11-03 and 2026-11-04, before the trio was the
benchmark. A 1-in-25 test sample ran 58.3 s -> 49.8 s, then 51.8 s -> 41.2 s.

## 2. Tried and did not clear the bar

The bar is about 1 s on the trio for each ~50 net new source lines. A new soundness assumption
counts as a cost.

### Measured

| Idea | Where | Result | Why not landed |
|---|---|---|---|
| Generated per-path bookkeeping | bob/path-books-spike 4b42f0e | 0.7 s beyond the one-scan push, for about 75 lines | under the bar, and it adds a second, generated copy of the record logic |
| Batched checkpoint flushes | alt-records lead 2 | 3,900 of 178k scan-by-scan scans were warm cycles that a checkpoint cut | not built, since the most it could save is about 0.1 s |
| Inline carry for held and counting tags | alt-records, 17 lines | 0.15-0.25 s (0.53 s at most) | below the 0.34 s that 17 lines need |
| A kernel cache keyed by the identity of each hold rung's inputs | bob/kernel-compiles cd60404 | compiles 20 -> 12, compile time 3.2 -> 1.0 s, trio unchanged within noise | +20 lines for about 0.6 s, and it holds only while every hold input is listed |
| Generated readers and successor checks alone | path-books spike 08983e4 | 0.3 s beyond the push, for about 47 lines | under the bar |
| Trace only when it pays, first try | 9d9d547 | a short how() wait about 20% faster with it, 5-7% without | it moved how() answers through the budget, so it was reverted. It landed later as row 8 |

### Weighed on paper or ruled out

| Idea | Why not |
|---|---|
| Linking paths into a trace tree | At d172e42 it looked worth about 20 s. Tumbler paths take turns every scan, so linking does not shrink the records. The cycle jump took that time instead. |
| A column goes Unknown sooner than stepping | It allowed longer jumps. The user ruled that how()'s candidate scans must equal stepping's, so it was withdrawn (2026-11-08). |
| Keyword arguments to generated helpers | 20-30 lines for 2 scans in the loop corpus. There is no trio effect. |
| Seek newest first | About 11% of a test sample, from fewer independent fills. Left to the user and not taken. |
| A different PMap backend (`immutables`) | 3x slower in its pure-Python fallback. Parked. |
| Keeping traces across runs | The guards are baked into each trace, so the cost would depend on history. |
| The stable-wait jump (an affine model) | It needs special cases for `//`, `%`, `min`, the clocks and float timestamps. The cycle jump covers the sound part. |
| Treating `scan_counter` as a moving cell | Ranked low and not built. |
| Tracing a callable stop predicate's reads | Unsound, because a closure can read the runner. |
| `randint` in the loop | Unsound for the loop's guard argument. |
| A period-2 trace | Rejected. The cycle jump covers it. |
| A self-checking fold | +220 lines, and it rescued single-flip shapes only. |
| A coarser `dt` | Not equal to stepping. |
| SMT over the program | It cannot model nonlinear `calc`, indirect addressing or callables, and it is one more model to keep in step. |
| A sampled online twin audit | The compiled replay of each window eats the win. |
| Skipping quiet rungs, or a rung memo | A raw kernel rung costs 82-200 ns, about what a key compare costs. |
| HashLife, heuristic idle skip, QSS, gem5-style model switching | Not exact (jobs/jit-jump/RESEARCH.md). |
| The prover's classify and elide passes against main | The user ruled to skip it (2026-11-08). how() reads both, so it would need a how() compare. |

## 3. What's left

### How it was measured

- Tree: a detached worktree at becdd32, Python 3.14.6, with the trio's normal limits.
- The machine was loaded. A merge gate (replay-floor-4) held the heavy lock, and all three
  targeted slots were in use. So the seconds below come from shares, not from the loaded run.
- The shares come from one run under a stdlib sampling profiler (`prof/sample_plugin.py`). It
  read the main thread's stack about every 16 ms, which gave 11,929 samples. The sampler and
  the load made the run take 190 s, against about 71 s normally. The sampler's cost is spread
  by wall time, so the shares hold.
- Each share is turned into seconds against a 71 s trio.
- An earlier cProfile run gave the call counts. cProfile slowed the trio about 5x and inflates
  call-heavy code most, so its shares are not used. Under it internal_route hit the test's own
  240 s deadline after 71% of its loop scans.
- One small check timed a PLC build on the tumbler program (`prof/walk_check.py`).

The scripts and raw output are in `scratchpad/jit-ideas/prof/`.

### Where the time goes now

| Part | Share | About |
|---|---|---|
| **The jit, all of it** | **38%** | **27 s** |
| - the kernel's own scans inside the loop | 3.8% | 2.7 s |
| - cycle prediction | 6.9% | 4.9 s |
| - tracing new paths | 6.0% | 4.3 s |
| - building a `CompiledPLC` for each loop attempt | 4.9% | 3.5 s |
| - record writes | 4.6% | 3.3 s |
| - loop control (successor checks, `_blocked`, checkpoints) | 3.5% | 2.5 s |
| - bookkeeping around each firing | 3.3% | 2.3 s |
| - the check before each attempt (`_kernel_cannot_say_what_it_adds`) | 2.2% | 1.6 s |
| - kernel compiles | 1.8% | 1.3 s |
| - captures, jumps, landing, stop predicate | 1.3% | 0.9 s |
| **how() planning itself (pilot code)** | **20.7%** | **15 s** |
| **History replays** (states rebuilt scan by scan for how() and `cause()`) | **16.7%** | **12 s** |
| **Reading records for `cause()` and `effect()`** | **8.1%** | **6 s** |
| **Interpreted scans** | **6.6%** | **5 s** |
| **how()'s prover context** (built once, cold, in the first test) | **6.3%** | **4.5 s** |
| **Building a PLC** (the walk for known tags) | **2.1%** | **1.5 s** |
| Test setup (fixtures) | under 0.5% | under 0.3 s |

What the table shows:
- The jit's real scan work is 2.7 s. Its overhead around those scans is about 24 s, spread
  over nine parts of 1-5 s each. Each part has had at least one round already.
- 62% of the trio is outside the jit. That share is how() planning, history replays, `cause()`,
  the prover context and interpreted scans.
- The interpreted scans include about 1.6 s of test staging in held_dry. Its `Bench.step_until`
  steps 829 scans to set up the drive.
- `_commit_tags` is the largest single function, at about 10% of the trio. It runs once for each
  replayed scan and compares all ~2,700 tags. Almost all of its calls come from history replays.

### Who asks for the history replays

| Asker | Share of the trio |
|---|---|
| `correction_candidates._last_transition_scan` | 8.7% |
| `regression_requirements._owner_progress_obstruction` | 2.3% |
| `causal.recorded._rung_write_projection_at` | 2.0% |
| `causal.crossings_recorded._read_pair` | 1.5% |
| `corrections.causal_channel_guard` | 0.6% |

`_last_transition_scan` finds the last scan in a window where one tag changed. It does that by
replaying every state in the window and comparing neighbours. The committed-tag records already
hold the tag's changes, and `history.previous_transition(tag, at_or_before=...)` reads them.

### A consistency bug found on the way

A PLC built on a program that how() has used knows more tags than one built on a fresh program.

```
cold program:              PLC built in 0.13 s, 3754 known tags
after the prover context:  PLC built in 1.03 s, 5962 known tags
```

`_iter_tags_and_edge_names` (`runner.py`) holds each `Program` back until the rungs are done.
With `beyond_rungs` set, it then walks the program's whole `__dict__`, and that includes the
cached prover context and program graph. eaa2a4f fixed the same kind of walk for materialized
tags. Under the "correctness before features" rule, this one goes first whatever its speed.

### The candidates, ranked

The bar is about 1 s on the trio for each ~50 net new lines. A new soundness assumption counts
as a cost.

| # | Candidate | Expected | Net lines | Soundness risk | Clears the bar? |
|---|---|---|---|---|---|
| 1 | `_last_transition_scan` asks `history.previous_transition` instead of replaying the window | 3-6 s | about 10 | Low to medium. The two must agree on what a change is. how() may take another path, so it needs a how() compare. | Yes, if the compare is clean |
| 2 | `absent_scalars` keeps its per-kernel work on the kernel (the block-cell set, and the scalars that can be absent) | 1.5-3 s | about 8 | Low. It assumes a compiled kernel's `referenced_tags` and `block_specs` never change. A grep finds no write to either after compile. A tag's default must be checked the same way. | Yes |
| 3 | The known-tags walk stops at a `Program`'s rungs and subroutines | about 1.5 s | about 5 | Low. It fixes the bug above. how() sees fewer known tags, so it needs a how() compare. | Yes, and it is a fix first |
| 4 | The other replay askers read records too | 2-4 s | 30-80 | Medium. Each is a different `cause()` question with its own fallback. | Maybe, one asker at a time |
| 5 | A cheaper commit for each replayed scan | about 3 s after #1 (7 s before) | 30-60 | Medium to high. Diffing only the written tags needs the kernel to report every write. A missed write makes history wrong. | Unlikely |
| 6 | One `CompiledPLC` per wait instead of one per loop attempt | 1.5-2 s after #2 | 30-50 | Medium. State carries across attempts. | Borderline |
| 7 | Cheaper cycle predictions | 1-2 s | 40-100 | Medium. Reusing a prediction needs a proof that only the moving cells differ. | Unlikely |
| 8 | Fewer traces | under 1 s within the rules | - | Keeping traces across runs was rejected. | No |
| 9 | Record writes, bookkeeping and loop control | under 1 s each | - | The generated version already measured 0.7 s for ~75 lines. | No |
| 10 | The prover context's cold build | up to 4.5 s, once per process | - | Only the classify and elide passes are left, and the user ruled to skip them. | Out of scope |
| 11 | how() planning itself | unknown, perhaps 1-3 s | unknown | Pilot code, not the jit. `navigation_contracts.capture` and `world_key._semantic_key` show up most. | Needs its own pilot profile |

### The plain answer

Nothing left inside the jit looks likely to clear the bar. Its real scan work is 2.7 s. The
24 s of overhead is spread over nine parts that each had a round already. The ideas left there
either need a new soundness assumption or save under 1 s.

Three cheap changes outside the jit do look likely to clear it. They are #1, #2 and #3, together
about 6-10 s for about 25 lines. #3 is also a fix. #1 and #3 change what how() sees, so each
needs a how() compare before landing. Those three could close the 8 s gap to the old fold. That
gap is a reference point only, because the fold skipped scans without a proof.
