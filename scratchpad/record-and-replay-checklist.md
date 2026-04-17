# Record-and-replay migration — checklist

Working doc: `scratchpad/record-and-replay.md`.
Design decisions locked; verified file:line references updated against live code.

Key audit corrections (done once at the top, not repeated per stage):
- `_evaluate_monitors` at `runner.py:1331`, `_evaluate_breakpoints` at `runner.py:1344`.
- `capturing_rung` diff site in `context.py:289-313`; called from `runner.py:1392`.
- `plc.effect()` exists at `runner.py:542` (audit false negative; spec unchanged).
- `_dt_override_for_next_scan` does not exist — introduced in Stage 4.
- Current `_rung_firings_by_scan: dict[int, PMap]` at `runner.py:346`, maintained at `runner.py:1304-1318`. Stage 7 replaces.

Invariants to protect across all stages:
- Force diff-guard in `input_overrides.py:121-129` — was an optimization, now a replay correctness invariant. Do not revert.
- Rung-trace trimming from `e97138c` — keep. Unrelated to history; DAP still reads `_current_rung_traces` live.
- Checkpoint write path unconditionally writes full force map to `force_changes_by_scan[checkpoint_scan_id]` — not an optimization, a replay invariant.

---

## Stage 0 — scan_counter as derived tag (warmup, absorbs memory-savings.md #1) ✅

- [x] Verify `sys.scan_counter` and `SystemState.scan_id` are in lockstep. Confirmed: committed states always equal; within-scan reads return the entering-state value for both. No user-observable divergence window.
- [x] Added `system.sys.scan_counter.name` to `_DERIVED_TAG_NAMES` with resolver branch returning `ctx_or_state.scan_id`; also simplified `scan_clock_toggle` to read `scan_id` directly.
- [x] Deleted increment + `_set_tag_internal` in `on_scan_end`.
- [x] Removed the entire `_NO_PREV_TRACKING` frozenset (scan_counter was its only member); `_capture_previous_states` iterates `state.tags` and derived tags aren't there.
- [x] Updated `tests/core/test_system_points.py::test_scan_counter_and_scan_min_max_stats_update` to read via resolver; added `test_scan_counter_is_derived_from_scan_id` regression test.
- [x] Updated `tests/core/test_history.py::test_diff_reflects_system_tag_changes_between_scans` → renamed `test_diff_is_empty_for_idle_scans` (idle scans now produce zero tag diffs — the desired property).
- [x] Added `tests/core/test_scan_pmap_sharing.py::test_idle_scan_reuses_tags_pmap` locking in `state.tags` structural sharing across idle scans.
- [x] `make test`: 2655 passed. Lint: no new ty errors (4 pre-existing in `pytest_plugin.py` unrelated).

## Stage 1 — Recorder shim (no-op sink) ✅

- [x] New module `src/pyrung/core/scan_log.py` with `ScanLog`, `ScanLogSnapshot`, `LifecycleEvent`, `LifecycleKind`. Atomic `snapshot()` deep-copies the `dts` array and shallow-copies the sparse maps.
- [x] `runner.py.__init__`: instantiates `self._scan_log`, `self._forces_last_recorded`, `self._this_scan_drained_patches`.
- [x] `_commit_scan` hook records patches / force-map changes / dt at the new scan_id after commit.
- [x] `input_overrides.py:apply_pre_scan` returns the drained patches (dict); runner stashes for `_commit_scan` to consume.
- [x] New `_set_rtc_and_record` wraps `_set_rtc_internal` and records to `rtc_base_changes[scan_id+1]`. Wired as the SystemPointRuntime `rtc_setter`; `set_rtc()` uses it too. Lifecycle-internal `_reset_runtime_scope` still uses the bare `_set_rtc_internal` (no double-record).
- [x] Lifecycle recorders in `stop()`, `reboot()`, `battery_present.setter` (with same-value short-circuit), `clear_forces()` (no-op when empty).
- [x] `_set_time_mode` reinitializes the scan_log so `dts` presence matches the mode — safe because only `fork()` calls it and the log is always empty at that point.
- [x] No consumers yet — purely a capture shim.
- [x] `tests/core/test_scan_log.py` — 17 tests covering every channel + snapshot isolation + fork independence + 10k-idle-scan zero-bytes assertion.
- [x] `make test`: 2673 passed. `ruff check` clean; `ruff format` applied.

## Stage 2 — Per-channel determinism tests (foundation) ✅

- [x] New file `tests/core/test_record_and_replay.py` (13 tests).
- [x] `assert_plc_state_equal(live, replayed)` helper with explicit
  field coverage: tags, memory, scan_id, timestamp, `_rtc_base`,
  `_rtc_base_sim_time`, `_time_mode`, `_dt`, `_input_overrides._forces`,
  `_input_overrides._pending_patches == {}`, `_running`, `_battery_present`.
- [x] Per-channel tests: `test_replay_idle_scans`, `_patches`,
  `_forces_add_remove`, `_forces_interact_with_patches`,
  `_rtc_changes_via_set_rtc`, `_rtc_changes_via_apply_tags` (split the
  one "rtc_changes" into both sub-paths), `_lifecycle_stop`,
  `_lifecycle_battery_present_toggle`, `_lifecycle_clear_forces`,
  `_realtime_dt`, and `_with_logic_present`. Plus 2 smoke tests for
  the helpers themselves.
- [x] `_replay_from_log_for_test` helper (forks from scan 0, no
  checkpoints yet — Stage 4 replaces with `PLC.replay_to`).
- [x] **Mutation verification** — exercised 5 mutations, each caught
  by the relevant tests (reverted after confirming):
  - skip forces → `forces_add_remove` + `lifecycle_clear_forces` fail.
    `forces_interact_with_patches` passes because forces are cleared
    by the end; redundant coverage from the first two is enough.
  - drop final patch → `patches`, `forces_interact_with_patches`,
    `rtc_changes_via_apply_tags` all fail.
  - ignore `rtc_base_changes` → `rtc_changes_via_set_rtc` fails.
    `rtc_changes_via_apply_tags` *passes* because the in-scan
    `_apply_rtc_date/time` path re-fires `_rtc_setter` during replay
    and reconstructs the base independently — this gap closes in
    Stage 4 when the `_replay_mode` guard makes the log entry
    load-bearing.
  - skip lifecycle events → `lifecycle_stop` +
    `lifecycle_battery_present_toggle` fail.
    `lifecycle_clear_forces` passes because the empty force map is
    also captured in `force_changes_by_scan` — redundant capture.
  - ignore `dts` in REALTIME → `realtime_dt` fails (timestamp mismatch).
- [x] **Reboot lifecycle is deferred to Stage 4.** After `reboot()`
  resets `state.scan_id` to 0, `_record_lifecycle("reboot")` records
  with `at_scan_id=1` — indistinguishable from "reboot immediately."
  The current Stage 1 log format can't sequence the pre/post-reboot
  boundary. Stage 4 gets proper sequencing (sim_time-based ordering
  or reset-invalidates-log). Test commented out in test file.
- [x] `make test`: 2686 passed (+13 from Stage 1). `ruff check` +
  `ruff format` clean.

## Stage 3 — Checkpoints ✅

- [x] `self._checkpoints: dict[int, SystemState]` added; `_CHECKPOINT_INTERVAL_DEFAULT = 200` module constant, `checkpoint_interval=` keyword-only PLC constructor param with ValueError on <1, propagated through `fork()`.
- [x] `_nearest_checkpoint_at_or_before(scan_id)` helper — `max((c for c in self._checkpoints if c <= scan_id), default=None)`.
- [x] Force-map bypass in `_commit_scan`: `is_checkpoint = new_scan_id > 0 and new_scan_id % self._checkpoint_interval == 0`; recorder fires unconditionally at checkpoint scans, then `_checkpoints[new_scan_id] = self._state`. `scan_id > 0` skip matches the "fork(0) handles the initial state" contract.
- [x] `tests/core/test_record_and_replay.py::test_replay_forces_across_checkpoint` runs a force held across scans 5 and 10 at `checkpoint_interval=5`; asserts `force_changes_by_scan` keys == {1, 5, 10} with full `{"X": True}` at each, and exercises a mini replay from the nearest checkpoint that would KeyError if the bypass were removed. Plus `test_checkpoint_interval_rejects_non_positive` and `test_checkpoints_cleared_on_fork`.
- [x] Eviction policy: `_checkpoints = {}` reset in `_set_time_mode` alongside the existing scan-log reset — shares the fork boundary. Otherwise unbounded (Stage 5 adds log-trim coupling).
- [x] Stage 1 test adjusted: `test_idle_scans_cost_zero_bytes` now builds an idle PLC with `checkpoint_interval=10_001` so the 10K-idle-scan log-level zero-bytes claim still holds. Checkpoint cost is a separate budget line.
- [x] `make test`: 2689 passed (+3 new). `make lint`: clean aside from the pre-existing ty error on `_calculate_dt` method-assign in Stage 2's `_replay_from_log_for_test` helper (resolved in Stage 4 when `_dt_override_for_next_scan` replaces monkey-patching).

## Stage 4 — `replay_to(scan_id)` and `_replay_mode` guards ✅

- [x] `self._dt_override_for_next_scan: float | None` added in `__init__`; consumed at the top of `_calculate_dt` before the FIXED_STEP/REALTIME branches.
- [x] `self._replay_mode = False` added; `fork()` inherits False (fresh PLC default); `replay_to` manually sets it on the forked instance.
- [x] Guards: combined `if not self._replay_mode:` around `_evaluate_monitors` + `_evaluate_breakpoints` in `_commit_scan`; early-return guard at the top of `_set_rtc_and_record` (covers both `set_rtc()` and the `_apply_rtc_date/time` setter-call sites in system_points.py without propagating a new getter).
- [x] `replay_to(target_scan_id)` implemented next to `_nearest_checkpoint_at_or_before`. Anchors on nearest checkpoint <= target, falls back to `fork(scan_id=0)` when none exists. Walks log applying lifecycle → force map → RTC base → patch → `_dt_override_for_next_scan` → `step()`. Trailing lifecycle at `target_scan_id + 1` applied before return. Module-level `_apply_lifecycle_to_replay` helper; reboot is an AssertionError (can never appear in a live log under Option B).
- [x] **Reboot lifecycle: Option B.** `PLC.reboot()` resets `_scan_log`, `_checkpoints`, `_forces_last_recorded`, `_this_scan_drained_patches` to fresh state. No reboot lifecycle event recorded. Rationale: post-reboot scan_ids would alias pre-reboot entries in every sparse channel, so Option A (sim_time ordering of lifecycle alone) doesn't fix the collision; reboot is treated like a fresh recording session. Pre-reboot history not replay-addressable — user forks before rebooting if they need it.
- [x] Stage 2 tests rewritten through `replay_to` (public API). `_replay_from_log_for_test` + `_make_source_and_replay` retired. `assert_plc_state_equal` switched from raw `_rtc_base`/`_rtc_base_sim_time` comparison to effective-RTC-at-shared-timestamp comparison (fork()'s RTC rebase is mathematically equivalent to applying the trajectory, but not bit-identical). RTC tests additionally capture intermediate historical bases and compare against `replay_to(intermediate_scan)._rtc_base` so mutation 3 stays detectable despite the effective-RTC forgiveness.
- [x] Stage 1 test `test_reboot_records_lifecycle_event` rewritten as `test_reboot_resets_scan_log_and_checkpoints` (Option B semantics).
- [x] New tests: `test_replay_lifecycle_reboot`, `test_replay_across_multiple_checkpoints` (exercises fork-from-anchor and mid-range replay), `test_replay_to_rejects_invalid_target`, `test_replay_fork_is_in_replay_mode`.
- [x] **Mutation verification sweep** — all five mutations confirmed to surface the expected failing tests (see test file's module docstring for the mapping). The Stage 2 rtc-via-apply-tags gap closed (`_replay_mode` blocks the in-scan setter).
- [x] Pre-existing ty error on `_calculate_dt` method-assign in the Stage 2 helper is gone (helper retired).
- [x] `make test`: 2693 passed (+4 net from Stage 3: added `_reboot`, `_across_multiple_checkpoints`, `_rejects_invalid_target`, `_fork_is_in_replay_mode`; renamed one Stage 1 reboot test). `make lint`: clean.

## Stage 5 — Swap history consumers ✅

- [x] `History` rewritten as a stateless facade over the owning PLC. `at(scan_id)` routes through a new `PLC._state_at` helper that checks the recent-state window, the checkpoint dict, and the pinned `_initial_state` before falling back to `replay_to` — no recursion through `fork()`.
- [x] `_recent_state_window: deque[SystemState]` (`maxlen=20`, module constant `_RECENT_STATE_WINDOW_SIZE`) on `PLC`. Populated in `_commit_scan`, refreshed on tag-default seed and `_reset_runtime_scope`. Backs hot-path monitor `previous_value` / `_prev:*` reads and cheap recent `at()`/`range()`/`latest()`.
- [x] `_initial_scan_id` + `_initial_state` pinned at construction (and on reboot) so replay anchors and `oldest_scan_id` survive window rotation. Forks pin their own `_initial_scan_id` to the fork point.
- [x] `History` storage retired: `_order`, `_by_scan_id`, `_append`, `_evict_if_needed`, the `limit` ctor param all gone. Labels overlay (`_label_to_scan_ids`, `_scan_id_to_labels`, `_label_scan_metadata`) stays. `_label_scan` no longer requires a retained scan — any addressable scan_id is valid. New `History.scan_ids()` and `History._reset_labels()`.
- [x] `PLC._replay_range(start, end)` added — single-anchor batched walk used by `History.range`/`latest` for older slices.
- [x] `seek`, `rewind`, `diff`, `fork`, `fork_from` now route through the facade. `_commit_scan` no longer evicts `_rung_firings_by_scan` (Stage 7 replaces that storage); the obsolete `playhead.contains` fallback is gone.
- [x] Consumers rewired: `analysis/causal.py` (`_order` → `scan_ids()`, with `# TODO(stage-7)` perf-floor markers at each transition-walk site), `analysis/query.py` (cold/hot-rungs iteration), `dap/handlers/history_seek.py` (count + retained scan-id list).
- [x] `history_limit` ctor param preserved as a no-op for backward compat (still validates `>= 1` or `None`). Stage 8 picks the real bound.
- [x] Tests updated: `tests/core/test_history.py` rewritten for facade semantics (eviction tests inverted to assert no eviction; label tests use a runner-owned `History`; new `test_history_labels_survive_window_rotation`). `tests/core/test_breakpoints_labels.py::test_snapshot_labels_are_evicted_with_history` → `..._survive_history_window_rotation`.
- [x] Stage 4 mutation sweep + Stage 1/3 invariants intact: `make test` 2692 passed (+3 net rewrites). `make lint` clean.

## Stage 6 — DAP trace regeneration

- [ ] `_current_rung_traces` remains live-state on main PLC — no change.
- [ ] Historical trace request: replay up to N-1 with `_scan_steps`, run scan N with `_scan_steps_debug`, return.
- [ ] `fork._debug_mode = True` in `PLC.fork()` at `runner.py:727` — forks are investigation sessions.

## Stage 7 — Rung firings refactor (absorbs memory-savings.md #2)

- [ ] PDG-filtered capture in `context.py:289-313`: filter through `ProgramGraph.readers_of` (`analysis/pdg.py:69`) consumed-tag set.
- [ ] `_pdg_consumed_tags: frozenset[str]` on PLC, refreshed on rung change; `record_all_tags=True` escape hatch.
- [ ] New module `src/pyrung/core/rung_firings.py`: `RungFiringRange`, `PatternRef`, `AlternatingRun`, `FiredOnly`, per-rung intern dicts.
- [ ] Replace `_rung_firings_by_scan: dict[int, PMap]` (`runner.py:346, 1304-1318`) with `_rung_firings: dict[int, list[RungFiringRange]]`.
- [ ] Append logic: A,B,A detection for `AlternatingRun`; 100-pattern threshold for cycle→fired-only transition (one-way).
- [ ] **Parity-relative-to-run-start test** — guards the off-by-one trap called out in design doc :575-579.
- [ ] Rewrite `plc.cause()` / `plc.effect()` (`runner.py:420, 542`) lookup path to use `rung_firings_at(scan_id)` binary search over per-rung timelines.
- [ ] Sweep-on-log-trim eviction: trim ranges, preserve `AlternatingRun` parity on odd-delta `start_scan_id`, walk intern dicts for unreferenced patterns.

## Stage 8 — Tune K, finalize

- [ ] Benchmark `K ∈ {100, 200, 500}` on `click_conveyor.py` and a busy program — scrub latency vs. checkpoint memory.
- [ ] Verify success criteria: idle 0 bytes/scan, 1-hour FIXED_STEP <10 MB, fork-at-history <100 ms worst case.
- [ ] Retire `_rung_firings_by_scan` residuals; delete `tests/core/test_scan_pmap_sharing.py` if it now tests an obsolete property (replaced by direct log-bytes assertions).
- [ ] **Scrub stage markers from the codebase.** `git grep -n 'Stage [0-9]'` across `src/` and `tests/` — module/test docstrings, section banners, comments all reference Stages 0–9 during the migration. Once everything ships the staging is ancient history; leaving the markers in rots into noise. Keep only commentary whose meaning outlives the migration (e.g. "replay correctness invariant"), drop the "Stage N ..." framing.

## Stage 9 — Derived edge tags (side quest; small, optional)

- [ ] `_DERIVED_EDGE_TAGS: dict[str, Callable[[int], tuple[bool, bool]]]` in runner or system_points.
- [ ] `rise()`/`fall()` check the registry before falling through to `_prev:*` lookup.
- [ ] Populate for `sys.scan_counter` (always rises, never falls) and `sys.scan_clock_toggle` (alternates on scan_id parity).
- [ ] Test correctness on both; remove any obsolete `_NO_PREV_TRACKING` entries whose tags are now derived-edge.

---

## Out of scope (explicitly)

- Log persistence / export-import.
- Parallel/background replay for predictive scrubbing.
- Compression of the log.
- Generalized period-N alternating detection (only period-2 in Stage 7).
