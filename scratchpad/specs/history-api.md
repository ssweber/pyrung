# History readers: caller sweep and API proposal

Investigator report, 2026-11-09, on dev 63af332. It reads source, tests, docs and git history only, and ran nothing.
Every cost is read from the code. "Inference" marks a claim that needs a test. (The hook refused the investigator's
write to this path, so the orchestrator saved it from the hand-back.)

## Summary

- **The 8.7% has a plain cause.** `_rank_hypotheses` computes `proximity()` once per hypothesis
  (correction_candidates.py:149-155, 178). For each tag it calls `_last_transition_scan`, which replays the same window
  `[anchor, channel_scan]` again through `history.range()`. Nothing is memoised. bob/tail-speed fixes the recorded case
  with private readers.
- **A cold `history.at()` is not a lookup.** It replays a slab of 1,600 states that ends at the scan asked for
  (runner.py:94-95, 3010-3065), and only one slab is kept. A test pins this (tests/core/test_replay_capture.py:194-198).
  - Backward walks are cheap after the first miss.
  - Forward walks and `at(s-1), at(s)` pairs refill the slab on every step (inference).
  - `effect()` and pilot's sparse pairs walk forward. (The DAP miner, condenser and `log` verb did too. They were
    deleted after 5d0569a.)
- **`range()` may disagree with `at()` on IO scans.** `_replay_range_compiled` (runner.py:3984-4031) never applies the
  recorded `send`/`receive` writes. The other replay paths do (runner.py:3640-3645, 4084-4091, 3491-3494).
  - CHANGELOG.md:342 promises recorded answers for `at`, `replay_to`, `cause` and `effect`, not `range`.
  - No test covers it.
  - bob/replay-one-fork leaves this function unchanged.
- **Projected `cause(to=)` and `effect(from_=)` read the records without settling.** They return before
  `_settle_compiled_ranges` (runner.py:2365-2381, 2458-2478). projected.py:584, 895 and 1261 use the records reader,
  which never checks for unrecorded scans (history.py:196). After a long compiled run, `held_since` can be stale
  (inference).
- **`previous_transition` settles every unrecorded scan from the oldest one** (history.py:238). It has no lower bound.
  That is why bob/tail-speed went past it.
- **The same "last change of a tag in a window" question is written three times:**
  - correction_candidates.py:106
  - pilot/causal.py:64
  - dap/handlers/history_seek.py:243, the only one that uses records

## 0. Three facts the rest depends on

**A cold `at()` builds 1,600 states.**
- `_state_at` (runner.py:2979) tries each of these in order:
  1. the tip
  2. the recent cache
  3. a checkpoint
  4. the one kept slab (`_REPLAY_SLAB_MAX_ANCHORS = 1`)
- On a miss, `_replay_slab_fill` builds a slab of `_REPLAY_SLAB_SCANS = 1600` states that ends at the miss.
- The slab for scan s is `[s-1599, s]`, so scan s+1 always misses (inference from runner.py:3017-3031).

**`range()` is eager and keeps nothing.**
- The call chain is `History.range` (history.py:145) → `_causal_history_range` (runner.py:2899) →
  `_local_history_range` (runner.py:2922) → `_replay_range` (runner.py:4095).
- It returns a list of every state in the window.
- Whatever the recent cache lacks is replayed from the checkpoint before `lo`.
- Nothing is cached, so a second call over the same window replays it again.

**Records readers need recorded scans.**
- `_unrecorded_scans` (runner.py:1751, 4794) lists three kinds of scan:
  - compiled runs
  - loop stretches
  - scans stepped after the records fill (`_records_full`, runner.py:5065)
- A records reader must settle those scans first, by stepping them again (`_settle_compiled_ranges`, runner.py:2495).
- Settling refuses past `SETTLE_SCAN_LIMIT = 250_000` (rung_firings.py:27).
- The raw readers (`_causal_raw_committed_transition_before`, runner.py:1971) do not check for unrecorded scans
  themselves.

## 1. Caller sweep

### 1.1 Records-backed readers that exist

| Reader | Where | Public | Answers |
|---|---|---|---|
| `history.previous_transition(tag, to=, at_or_before=)` | history.py:204 | yes | a tag's latest change at or before a scan |
| `_causal_raw_committed_transition_before` | runner.py:1971 | no | (scan, value) of the latest committed change, across epochs |
| `_causal_last_committed_transition_before`, `_causal_committed_transition_at` | runner.py:2001, 1987 | no | the same as a `Transition`, or the change at one scan |
| `changed_replay_scan_ids` (committed_tag_timelines.write_scans) | pilot/effect_observation.py:822 | no | the scans in a window where these tags changed |
| `write_replay_scan_ids`, `value_or_varied_replay_scan_ids`, `terminal_target_replay_scan_ids` | effect_observation.py:775, 718, 860 | no | the scans where a writer attempted a tag or a value |
| `tag_transition_candidate_scans_before` | pilot/requirement_repair.py:176, causal/history.py:305 | no | candidate change scans by a writer set |
| `rung_firings(scan)`, `_recorded_rung_firings`, `_node_firings_at` | runner.py:2054, 2086, 2118 | first only | which rungs wrote what on one scan |
| `_changes_within` (DAP) | dap/handlers/history_seek.py:243 | no | each tag's newest changes in a window, with a settle and a deadline |
| `cause()`, `effect()` | runner.py:2309, 2414 | yes | causal chains |
| `_replay_rung_write_projection_at` and siblings | runner.py:3857-3947 | no | exact rung occurrences on one scan (interpreted replay, LRU of 16) |

### 1.2 Callers of whole-state readers

| # | Site | Real question | Records answer? | Hotness | Could the answer differ? |
|---|---|---|---|---|---|
| 1 | pilot/correction_candidates.py:110, from :152 | one tag's last change in a window | yes | per hypothesis × per tag, same window, no memo | Same `==` equality (sp_values.py:34, runner.py:5000). Records may be more faithful on IO scans (3.1). tail-speed switches it |
| 2 | pilot/regression_requirements.py:182, same function | the same | yes | per source tag, per regression attempt | as #1 |
| 3 | pilot/causal.py:169 `_program_written_changes`, full `range` | which scans changed these tags | yes (`changed_replay_scan_ids`) | per derivation (corrections.py:1310). Also per probe at skiff.py:257 with `start_scan=0`, the whole recorded run | runs only when the nominator fails closed, but then replays every scan from 0 |
| 4 | pilot/causal.py:176, ascending `at(s-1), at(s)` pairs | before and after values at each nominated scan | yes | per candidate | two slab refills per candidate on cold history (inference) |
| 5 | pilot/causal.py:64 `action_caused_change` fallback | one tag's last change in a trial window | yes | per trial × tag × action tag, when the pen timeline lacks the change (outcome.py:135, steer.py:838) | as #1 |
| 6 | pilot/corrections.py:325 `_causal_history_range` | the first scan in a window where a source became True | yes, oldest-first changes | per source, per exposure derivation, fallback only | the clamp at the oldest scan is handled at :344 |
| 7 | pilot/regression_requirements.py:232, backward `at()` loop | the first scan of the current run where a condition held | partly (the condition's read tags) | per owner obstruction, one state per scan | no |
| 8 | regression_requirements.py:223, :248 | the tag map at one scan, for `plan`/`demand_holds` | named tags only | per completion | no |
| 9 | pilot/departure.py:518, investigation_replay.py:362, corrections.py:1436 | a whole tag map kept as a snapshot | no | per incident | a cold miss costs a 1,600-state slab |
| 10 | runner.py:2023, :2025 `_committed_transition` | one side of one change on an Unknown column or a gap | partly | per boundary in those cases | ascending pair |
| 11 | causal/recorded.py:1964 forward `effect()` walk; :2220 via the capture path (runner.py:3804) | the state each scan of a forward slice evaluates | partly (SP-tree reads) | per user `effect()`, up to 1,000 scans | up to 1,000 slab refills on cold history (inference) |
| 12 | causal/recorded.py:484, 536, 795, 798, 1075, 1362; causal/history.py:45-46, 161-162, 268-269; crossings_recorded.py:133-134, 197-199; projected.py:717, 1070 | one or two tags at one scan, or an SP view | mostly | per causal step, mostly backward | see 3.2 |
| 13 | causal/_rung_writes.py:640-641 | entry and exit tags of one scan | the capture usually supplies them | rare | ascending pair |
| 14 | (deleted, in 5d0569a) dap/condenser.py:511 | the last change of a few tags in a span | yes | per command, forward pairs | slab thrash (inference) |
| 15 | (deleted, in 5d0569a) dap/miner.py:413 (called twice, :83 and :648), :633, :795-798 | edges, Bool values per scan, values after a trigger | yes | per command, forward loops | as #14 |
| 16 | (deleted, in 5d0569a) dap/console.py:1508 | the last n states for a log | mostly changes | per command, already newest first | no |
| 17 | dap/handlers/history_seek.py:286-287 | how named tags differ between two scans | yes | per page | no |
| 18 | runner.py:2035 seek, 2044-2049 rewind, 2236 diff, 2403; history.py:291-331, 337-411 labels | a whole state, a timestamp, a diff | named-tag `diff` yes | per user call | `find_all` walks forward |
| 19 | prove/blackbox.py:772, :941 | the state just before the tip | not needed (cached) | per explored state | no |
| 20 | analysis/graph.py:564 `Plan.replay` → `replay_to(tip)` | an independent replay | no, by design | per call | no |

Also, `query.hot_rungs` (query.py:869-880) settles everything with no bounds. It reads the lineage's `scan_ids()`
against the local epoch's timelines. Nobody has checked whether those match on a fork with inherited epochs.

### 1.3 Cost ranking (inference)

1. Rows #1 and #2: H × T full-window replays.
2. Row #3: a whole-run replay from skiff whenever the nominator fails closed.
3. Row #4: two slab refills per candidate.
4. Row #5.
5. Rows #6 and #7.
6. Rows #8 and #9: one slab each.
7. Outside how(): `effect()` on cold history (#11).

## 2. The API

### 2.1 The three layers

| Layer | Commit | Methods | What the name promises now |
|---|---|---|---|
| 1. Every state kept | a92edc0, 2026-02-21 (`at` was `self._by_scan_id[scan_id]`) | `at`, `range`, `latest`, `find`, `find_all`, `contains`, then `seek`, `rewind`, `diff`, `fork_from` | a lookup |
| 2. Record and replay | 2203a9d, 2026-04-24 (`ScanLog`, `RungFiringTimelines`) and later | `replay_to`, `replay_trace_at`, `rung_firings`, `at_or_before_timestamp`, `history=`/`cache=`/`history_budget=`, checkpoints, slabs | layer-1 names kept, bodies became replays |
| 3. Pilot's ways around replay | b5d2303, 2026-08-25 and later | public `previous_transition`. Private `_committed_transition_at`, `_last_committed_transition_before`, `_causal_window`, epoch lineage, `_settle_compiled_ranges`, `UnrecordedScansError`, the effect_observation nominators, `_replay_*_projection_at` | the cheap readers, mostly private and spread over three modules, each caller adding its own settle and fallback |

Layer-1 names that now hide a replay:
- `at()`: one 1,600-state slab per miss, and a refill on every forward step.
- `range()` and `latest()`: a replay of the whole window on every call.
- `find_all()`: reads labels oldest first, so it walks forward.
- `diff(a, b)`: builds two whole states even when the question is about a few tags.

The scatter:
- "Last change in a window" is written four times (see Summary).
- "Which scans changed" lives inside a pilot module, so DAP and causal cannot reach it.
- `previous_transition` is the only public records reader, and it has the wrong shape for a window (3.3).

### 2.2 The questions pilot asks

The counts are call sites in pilot/*.py, not run-time calls.

| Question | Sites | Answered today by |
|---|---|---|
| Q1. One tag's last change in a window or before a scan | 4: correction_candidates.py:110 (via :152 and regression_requirements.py:182), causal.py:64, requirement_repair.py:190 | 2 whole-window replays, 1 timeline walk |
| Q2. A tag's first change in a window, to a value | 1: corrections.py:325 | a whole-window replay |
| Q3. The scans in a window where any of these tags changed | 6: causal.py:160 plus 4 nominators used by observe_execution_window (effect_observation.py:925) | private records, failing closed to the full window |
| Q4. Values before and after each such scan | 1: causal.py:176 | ascending `at()` pairs |
| Q5. How long a condition has held | 1: regression_requirements.py:232 | an `at()` per scan |
| Q6. The tag map at one scan, kept as a snapshot | 5: departure.py:518, investigation_replay.py:362, corrections.py:1436, regression_requirements.py:223, :248 | `at()` |
| Q7. Recorded rung writes on one scan | 2: causal.py:197, :210 | records |
| Q8. Exact rung occurrences on one scan | 24: 19 `_replay_rung_write_projection_at`, 3 pilot variant, 2 `_replay_rung_runs_at` | an interpreted replay of one scan, LRU per epoch |
| Q9. Why a tag changed | 9 `cause()` sites | records plus Q8 |
| Q10. Which epoch owns a scan | 16 `_causal_lineage` sites | lineage |

Q1 to Q5 ask about a few named tags, and records can answer all of them. Only Q6 and Q8 truly need a whole state or a
replayed scan.

### 2.3 Proposed readers

Each reader settles only the scans it reads, routes through the epoch that ran each scan, and fails closed.

1. **`history.changes(tags, lo, hi, *, newest_first=True)`.** It yields `Transition(tag, scan, before, after)` for
   every change in the inclusive window `lo..hi`.
   - It serves Q1 to Q5, `previous_transition`, and DAP's `_changes_within`.
   - It settles lazily, one segment at a time, newest first.
   - The before-value fallback is the one in `_committed_transition` (runner.py:2012).
   - Size: about 100 lines, moved from effect_observation.py:822, runner.py:1971-2026 and history_seek.py:243.
   - Risk: medium. Cross-epoch stitching and the before-value rule must match `_committed_transition`. Extend
     tail-speed's tests/core/analysis/test_pilot_last_transition_scan.py as the agreement test.
   - Public: yes.
2. **`history.value(tag, scan)` and `history.values(tags, scan)`.** Each reads the latest change at or before the scan,
   or the initial tags. An Unknown column falls back to one `at()`.
   - They serve Q6 when a caller names its tags, `diff` of named tags, `_window_entry`, and most of row #12.
   - Size: about 30 lines. Risk: low. Public: yes.
3. **`history.state_at(scan)`.** This is the whole-state read, named as the expensive case. `at()` stays as an alias,
   with a docstring that states the cost. Public: yes, plus docs.
4. **`history.states(lo, hi)`.** A lazy iterator that replaces `range()` for long windows. A caller that stops early
   stops the replay.
   - Keep `range()` as a list documented as "replays the window". Making `range()` itself lazy breaks the `states[1:]`
     slicing in its callers.
   - Size: about 40 lines. Public: yes.
5. **Q8 readers** stay private and replay-backed. The records are lossy on purpose (causal/history.py:34-37).
6. **A private replay counter for tests.** It counts slab fills, range replays and settles.
   - It follows `ExecutionReceipt.projection_replay_count` (pilot/types.py:666-686) and `_last_replay_slab_stats`
     (runner.py:3059).
   - Size: about 20 lines. Not public.

How the other consumers map onto these readers:
- **DAP.** Seek uses `changes`. `_window_entry` uses `values`. The variables view uses
  `state_at`.
- **Causal.** `_find_last_transition_scan` and `_boundary_transition_at_scan` become wrappers over `changes`. `effect()`
  keeps `state_at` for SP views, behind a forward-aware slab.
- **Users.** The docs lead with `changes`, `value` and `previous_transition`. `state_at` comes after them, with its cost
  stated.

What still needs whole states:
- snapshots (Q6)
- seek, fork and rewind
- prover edge views
- SP-tree evaluation in `cause`, `effect` and crossings
- exact occurrences (Q8)
- `Plan.replay`

Pilot invariants (pilot/AGENTS.md) that bind these readers:
- **Epoch identity.** Every read routes through `_causal_lineage`, as `lineage.newest` does at runner.py:1977. An old
  scan is never read under a newer overlay.
- **Fail closed.** A reader raises rather than skip a gap. In a search, `UnrecordedScansError` already becomes
  `SearchQuestionRefused` (runner.py:2092-2095), so pilot's `except Exception` still means only "no evidence".
- **Pricing.** The counter is a diagnostic only and never feeds `SearchBudget`.

Migration and CHANGELOG impact:
- These callers move:
  - 6 pilot sites
  - 4 nominators
  - 4 DAP sites
- Causal changes are wrappers only.
- Docs to update: docs/guides/runner.md:249-283 and docs/internal/debug-spec.md:93-98.
- New public methods: `changes`, `value`/`values`, `state_at` and `states`.
- Nothing is removed. Any later deprecation of `at` or `range` is its own change.

## 3. Other footguns

- **3.1 The `range()` IO gap.** See Summary.
  - Test: compare `plc.history.range(1, 7)` with `recorded` in `_session_after_one_answer`
    (tests/core/test_kernel_io.py:229).
  - Unknown: whether a program with a live `receive()` gets a replay kernel.
  - Fix: about 6 lines and a CHANGELOG fix line.
- **3.2 Projected modes do not settle.** See Summary. Fix: about 4 lines. Afterwards they may raise
  `UnrecordedScansError`.
- **3.3 `previous_transition` settles from the oldest scan and has no lower bound.** Fix: settle lazily and add
  `since=`. About 25 lines, public.
- **3.4 Window conventions are mixed.**
  - `range` is half-open.
  - `cause(since=)`, `previous_transition(at_or_before=)`, `_unrecorded_in` and `_settle_compiled_ranges` are inclusive.
  - Pilot writes `end_scan + 1` by hand (causal.py:66, :169, correction_candidates.py:110, corrections.py:327).
  - New readers should be inclusive.
- **3.5 The errors differ.** `at()` raises `KeyError` (history.py:136-143), but `range()` and `latest()` clamp silently
  (history.py:150-154). corrections.py:339-348 works around the clamp by hand.
- **3.6 Lists searched in O(N).** causal/recorded.py:1905 and :2203 build `list(history.scan_ids())` and call
  `.index()`. `_scan_index` (causal/history.py:15) already does this in O(1). Fix: about 6 lines.
- **3.7 `_rank_hypotheses` has no memo**, even with records. Fix: about 4 lines.
- **3.8 A possible off-by-one in FIXED_STEP `rewind()`.** It uses `int(timestamp / dt)` (history.py:287) on summed
  float timestamps, so it may land one scan early (inference). Needs a test with `dt=0.1`.

## 4. Proposals ranked

| # | Proposal | Benefit | Size | Risk | Public |
|---|---|---|---|---|---|
| 4.1 | Forward-aware slab: extend when a miss is the slab's end + 1, or centre the slab on the miss | ends per-scan refills in `effect()` and the pairs, with no caller changes | ~15 lines | low (update test_replay_capture.py:194-224) | no |
| 4.2 | IO writes in `_replay_range_compiled` | `range` agrees with `at` | ~6 lines + test | low | fix line |
| 4.3 | Settle in projected modes | no stale `held_since` | ~4 lines | can now raise | fix line |
| 4.4 | `changes()`, with pilot Q1-Q4 and DAP moved onto it | one cheap reader replaces four copies | ~120 added, more removed | medium | yes |
| 4.5 | Lazy settle and `since=` on `previous_transition` | cost tracks distance | ~25 lines | low | yes |
| 4.6 | Replay counter | tests can bound cost | ~20 lines | low | no |
| 4.7 | Cost stated in the docstrings and docs of `at`/`range`/`latest`/`diff`/`find_all` | the cheap answer is the obvious one | docs only | none | docs |
| 4.8 | `values`, `state_at`, `states` | named whole-state reads | ~70 lines | low | yes |
| 4.9 | Inclusive windows and the `_rank_hypotheses` memo | fewer off-by-ones | small | low | with 4.4 |

## 5. Branches

- **bob/tail-speed.** It fixes cost #1 in the recorded case, with the same equality. It falls back to `range` when the
  window holds unrecorded scans. It uses private readers.
- **bob/replay-one-fork.** It merges both `replay_to` paths into `_build_replay_fork`, and its new walker applies IO
  writes. `_replay_range_compiled` is unchanged, so 3.1 still stands. The slab's direction is unchanged.

## 6. Not determined without running code

- Run-time call counts. The counts above are call sites.
- Whether a live `receive()` program gets a replay kernel (3.1).
- The real cost of slab thrash. A test using `_last_replay_slab_stats` or the 4.6 counter would show it.
- Whether `hot_rungs` misreads inherited epochs on a fork.
- The `rewind()` floor.

## 7. Found by the tail-speed review (2026-11-09), binds the new readers

- **The commit records miss edits made between scans.** A tag set by `_edit_tip_tags` (Harness install seeding, set_overrides, unit carries, and how()'s own harness at the work fork's root) changes the state but writes no record. cause(), previous_transition and any records-backed `changes()` share this gap. A `changes()` reader must either see edits as transitions (a change to cause()'s current rule that an edit is only the next change's "before") or fall back to a replay across an edit. After bob/replay-one-fork, every edit is a checkpoint with a `(scan, None)` entry in `_plant_changes`, which makes the fallback cheap to detect.
- **The records use `!=`, so a NaN copied onto itself reads as a change every scan.** The state keeps the same object and shows no change. bob/tail-speed-2 fixes the writers so one rule holds: a change only when not (old is after or old == after).
