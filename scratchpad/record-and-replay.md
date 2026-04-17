# Record-and-replay architecture — design note

> **Plan status (updated as of Stage 1 commit).** Live stage-by-stage
> checklist lives in `record-and-replay-checklist.md`. Key decisions
> since this doc was first drafted:
>
> - **Checkpoint interval `K=200`** as the starting default (every
>   ~2 s at 100 Hz; worst-case replay <100 ms). Tuned in Stage 8
>   against `K ∈ {100, 200, 500}`.
> - **Stage 0 (`sys.scan_counter` as a derived tag) is done** —
>   committed to `dev`. Absorbs memory-savings.md #1 and makes
>   checkpoints genuinely cheap (idle scans no longer churn
>   `state.tags`).
> - **Stage 1 (recorder shim) is done** — `ScanLog` captures every
>   nondeterminism channel; no consumer yet. Tests in
>   `tests/core/test_scan_log.py`.
> - **Stage 7 (rung-firings refactor) is in-scope but separable** —
>   safe stop-points between any pair of stages. Absorbs
>   memory-savings.md #2.
> - **Stage 9 (derived edge tags for `rise()`/`fall()` on derived
>   system tags) added** as an optional side-quest.
>
> **Carry-overs from commits `e97138c` / `d85af97` (prior memory
> work) — what stays and what doesn't:**
>
> - **Force diff-guard in `input_overrides.py:127-135` — keep.** This
>   was an optimization, now a replay-correctness invariant (paired
>   with the checkpoint-writes-full-snapshot rule below).
> - **Rung-trace trimming — keep.** Orthogonal to history; DAP still
>   reads `_current_rung_traces` as live state.
> - **Skip `_prev:*` writes when unchanged — keep.** Hygiene, harmless.
> - **System-point diff helpers + skip `_dt` write — keep.** Hygiene.
> - **Exclude `sys.scan_counter` from `_prev:*` tracking — subsumed by
>   Stage 0**, which removed `scan_counter` from `state.tags` entirely.
> - **Reuse prior PMap for identical rung firings (`e97138c` #3) — goes
>   away in Stage 7** (replaced by per-rung range-encoded timelines).
> - **`tests/core/test_scan_pmap_sharing.py` — vestigial after Stage 7**;
>   the property it locks in (idle-scan PMap reuse) is replaced by
>   direct log-bytes assertions.

## Context

We've spent significant effort optimizing the snapshot-per-scan history model
(commits `e97138c`, `d85af97`, plus the follow-ups in `memory-savings.md`).
After those changes, idle scans leave `state.memory` structurally shared with
the prior scan, system-point no-op writes are diff-guarded, and rung traces
only retain the most recent debug scan. We landed at roughly **10 KB/scan**
of growth during a DAP session on `examples/click_conveyor.py`. At
`history_limit=1000` that plateaus around 10 MB — bounded, but not what we
actually want.

**The goal is hours of playtime with full recall.** At 100 Hz that's
360,000 scans/hour. Even at the theoretically-optimal ~1 KB/scan we'd still
hit 360 MB/hour, and we'd be forced to cap `history_limit` well below "full
recall." Incremental optimization of snapshot-per-scan cannot get us to the
target; the architecture itself is wrong for this workload.

**This note is a hard replace of the snapshot-per-scan history model with
record-and-replay.**

## Why record-and-replay fits this codebase

PLC scans are pure state transitions given inputs. The complete
nondeterminism surface is tiny:

1. `_pending_patches` — drained into `ctx.set_tags()` in
   `InputOverrideManager.apply_pre_scan`, then cleared. Externally injected
   via `plc.patch()`.
2. `_forces` — applied pre-logic and post-logic, diff-guarded. Externally
   mutated via `force()` / `unforce()` / `clear_forces()`.
3. `dt` — deterministic in `FIXED_STEP`; wall-clock in `REALTIME`.
4. RTC base changes — `_set_rtc_internal` is called from `set_rtc()` (user)
   and `_apply_rtc_date/time` in `system_points.py` (triggered by user
   writing `rtc.new_*` + `rtc.apply_*`, which is downstream of patch/force).
5. Lifecycle operations — `stop()`, `reboot()`, `battery_present = X`,
   `clear_forces()`.

That's it. Everything inside the scan — `on_scan_start`, logic evaluation,
`_capture_previous_states`, `on_scan_end`, `ctx.commit()` — is a pure
function of `(state, dt, patches, forces, rtc_base)`.

Also in our favor:

- `PLC.fork(scan_id)` already exists and does 90% of "resume from
  checkpoint" — creates an independent PLC rooted at a historical snapshot,
  restores RTC base, sets time mode.
- RTC is fully derived from `(rtc_base, rtc_base_sim_time, sim_time)`. No
  wall-clock reads during replay.
- `ScanContext.commit()` is the only state-transition sink, and it's
  deterministic.

## Log shape: sparse-by-field

The log is *not* an array of `ScanLogEntry` objects. A frozen dataclass
with five fields costs ~88–250 bytes in CPython even when every field is
`None` — multiply by 360K scans/hour and you're paying 30+ MB of pure
object overhead for idle scans that contain no information. The
rung-firings layer already commits to "only real state changes produce
timeline entries"; extend the same principle to the top-level log.

Instead, the log is a small collection of sparse side-structures, each
keyed by scan_id only when that scan has relevant data:

```python
class ScanLog:
    # The earliest scan_id still retained. Advances forward as the log
    # trims its leading edge. `dts` indexing is offset relative to this.
    base_scan: int

    # Sparse: only scans where a patch was actually applied.
    patches_by_scan: dict[int, Mapping[str, Any]]

    # Sparse: only scans where the force map changed from prior. Checkpoints
    # always carry a full snapshot here (never skipped), so replay never
    # needs to look back past the nearest checkpoint.
    force_changes_by_scan: dict[int, Mapping[str, Any]]

    # Sparse: only scans where RTC base changed.
    rtc_base_changes: dict[int, tuple[datetime, float]]

    # Dense, but only in REALTIME mode. In FIXED_STEP, dt is a PLC
    # constant read from config at replay time — no per-scan storage.
    # Indexed as `dts[scan_id - base_scan]`.
    dts: array.array | None  # array('d', ...) float64, None in fixed-step

    # Rare events between scans, ordered by sim_time.
    lifecycle_events: list[LifecycleEvent]
```

Replay reconstructs the "conceptual entry" for scan N by parallel lookup:

```python
patches = log.patches_by_scan.get(N)
forces_change = log.force_changes_by_scan.get(N)
rtc_change = log.rtc_base_changes.get(N)
dt = log.dts[N - base_scan] if log.dts is not None else plc._dt
```

An idle scan (no patches, forces unchanged, RTC unchanged, fixed-step
dt) costs **zero bytes** in the log. A scan with one patch costs one
dict entry (~100 bytes). A REALTIME session pays 8 bytes per scan for
the dt array regardless — that's ~2.9 MB/hour, cheap and contiguous.

Design rationale:

- **Log patches, not `patch()` calls.** Users may call `plc.patch()`
  multiple times before `step()`; only the final merged dict matters for
  replay. Record what actually entered the context at `apply_pre_scan`.
- **Force changes, not the full map per scan.** Forces are persistent,
  so "what forces are active at scan N" is recoverable by walking
  backward to the most recent `force_changes_by_scan` entry at or before
  N. The checkpoint invariant (checkpoints always write a full snapshot
  here) bounds that walk to the nearest checkpoint.
- **RTC base as a sparse dict.** RTC changes rarely; per-scan entries
  would be wasteful. Dict keyed by scan_id matches the access pattern.
- **`dt` elided in FIXED_STEP.** `dt` is constant and knowable from PLC
  config. In REALTIME mode, the array is the right shape — dense, tiny,
  fast to read.
- **Lifecycle events** (`stop`, `reboot`, `battery_present`,
  `clear_forces`) happen between scans, not during them. Their own list:

```python
@dataclass(frozen=True)
class LifecycleEvent:
    at_sim_time: float
    kind: Literal["stop", "reboot", "battery_present", "clear_forces"]
    value: bool | None = None  # used by battery_present; None otherwise
```

## Replay shape

```python
def replay_to(self, target_scan_id: int) -> SystemState:
    checkpoint_scan_id = self._nearest_checkpoint_at_or_before(target_scan_id)
    log = self._log.snapshot()  # cheap: dict views + array reference
    replay_plc = self.fork(scan_id=checkpoint_scan_id)
    replay_plc._replay_mode = True  # suppress monitors, breakpoints, labels

    # Establish starting force map from the checkpoint's force snapshot.
    replay_plc._input_overrides._forces = dict(
        log.force_changes_by_scan[checkpoint_scan_id]
    )

    for scan_id in range(checkpoint_scan_id + 1, target_scan_id + 1):
        if patches := log.patches_by_scan.get(scan_id):
            replay_plc.patch(patches)
        if forces := log.force_changes_by_scan.get(scan_id):
            replay_plc._input_overrides._forces = dict(forces)
        if rtc := log.rtc_base_changes.get(scan_id):
            new_base, new_base_sim_time = rtc
            replay_plc._set_rtc_internal(new_base, new_base_sim_time)
        dt = log.dts[scan_id - log.base_scan] if log.dts is not None else replay_plc._dt
        replay_plc._dt_override_for_next_scan = dt
        replay_plc.step()

    return replay_plc.current_state
```

The log must be append-only, and `self._log.snapshot()` must return a
frozen view of all five fields taken atomically. The sparse dicts can be
shallow copies (the inner Mapping values they point to are immutable —
patches were drained, forces are rebuilt per-scan, RTC tuples are
frozen). The `dts` array **must be copied** (`array.array('d',
self._dts)`) — `array.array` is mutable, and the live log will append
to and trim-slice the same underlying object. A bare reference is not a
snapshot and will produce stale or crashing reads if a scan or trim
lands mid-replay. The copy costs ~2.9 MB and ~1–2 ms at 1 hour of
REALTIME history, negligible against replay's overall cost.

Per "fork at a history point" UX (not continuous scrubbing): replay is
called once to produce a fork, then the forked PLC runs independently. This
is the dominant access pattern and keeps replay off the hot path for
analysis APIs.

## Checkpoint policy

- One full `SystemState` snapshot every K scans. **K=200 as the
  starting default**; Stage 8 benchmarks `K ∈ {100, 200, 500}` on
  `click_conveyor.py` and a busy program before locking it in.
- Snapshots are PMap-structurally-shared with neighboring states, so they're
  cheap. K trades fork latency (O(K)) against checkpoint memory.
- **The checkpoint write path unconditionally writes the current force
  map to `force_changes_by_scan[checkpoint_scan_id]`, bypassing the
  diff-guard that normally elides unchanged scans.** This is a replay
  correctness invariant, not an optimization — replay reads the force
  map from the checkpoint scan's entry without walking further back.
  Any future "optimization" that tries to elide unchanged force writes
  at checkpoint boundaries will break replay past the first checkpoint.
  Test this directly in `test_replay_forces_*`: run past a checkpoint
  with no force changes, confirm replay still reconstructs the force map.
- Evict checkpoints only on explicit session reset. Log entries between
  evicted checkpoints can be dropped together.

## Log trimming

When the log's retention policy trims its leading edge to scan N:

- Sparse dicts: delete keys < N from `patches_by_scan`,
  `force_changes_by_scan`, and `rtc_base_changes`. Dict shrinks
  naturally.
- `dts` array (REALTIME only): drop the leading `N - base_scan` slots
  (`dts = dts[N - base_scan:]`) and advance `base_scan = N`. Subsequent
  lookups `dts[scan_id - base_scan]` work correctly against the new
  origin.
- `lifecycle_events`: drop events with `at_sim_time` before the
  timestamp of scan N.
- Rung-firing timelines trim independently per the sweep-on-log-trim
  rule below. That sweep and the log trim should run together as one
  coordinated operation.

## What needs a `_replay_mode` guard

During replay these must NOT fire (side effects on user code / live data):

- `_evaluate_monitors` at `runner.py:1364` (post-audit; previously
  cited as :1327) — user callbacks.
- `_evaluate_breakpoints` at `runner.py:1365` (previously cited as
  :1328) — would add duplicate label snapshots and spurious pause flags.
- `_rtc_setter` via `_apply_rtc_date/time` in `system_points.py:515-549`
  — RTC base changes during replay come from the scan entry's
  `rtc_base` field, not from re-firing the RTC apply tags. Guard the
  setter inside the RTC apply path so running the path is a no-op in
  replay mode.

All three are a one-line `if not self._replay_mode:` guard each.

Everything else inside the scan pipeline runs normally — that's what
reconstructs state.

## What replaces History

- **Small recent-state window** (10 scans is plenty) for monitor callbacks'
  `previous_value` and `_prev:*` edge detection. This stays live.
- **Log** — sparse-by-field side-structures. Idle scans contribute zero
  bytes; only real state changes (patches, force changes, RTC changes,
  lifecycle events) take space. In REALTIME mode, an 8-byte dt array is
  dense (~2.9 MB/hour at 100 Hz); in FIXED_STEP, even dt is elided.
  Idle-dominated hour: a few hundred KB. Busy hour: single-digit MB.
- **Checkpoints** — one per K scans, each structurally shared. Negligible
  overhead.
- **On-demand replay** for any historical scan.

The existing `History` class and its deque/dict machinery goes away. The
public `plc.history.at(scan_id)` becomes `plc.replay_to(scan_id)` (or we
keep the method name and change the implementation — the API surface is
unchanged for most callers).

## Interaction with the debug path (DAP)

The DAP adapter currently reads live state: `_current_rung_traces`,
`_latest_committed_trace_event`, monitor callbacks. The UX is **fork at a
history point** (e.g. click a causal chain step, click "last changed
value") — not continuous scrubbing. A fork is a single `replay_to` call
followed by the forked PLC running independently from there.

When the user forks at scan N and wants to see traces from that scan:
replay up to N-1 with `_scan_steps`, then run scan N with `scan_steps_debug`
to produce the traces, then commit and return. The forked PLC accumulates
its own traces going forward. No scratch slots, no continuous
regeneration.

## Migration plan

> The live, checked-off version lives in
> `record-and-replay-checklist.md`. The checklist uses 10 stages
> (0–9) — this section is the original 7-step plan the checklist
> expanded from. Stage 0 (done) and Stage 9 (derived edge tags,
> optional side-quest) are additions since this doc was drafted;
> Stage 7 (rung-firings refactor) maps to the "Rung firings
> storage" section below and is explicitly separable.

Staged so each step is independently shippable and reversible:

1. **Recorder shim (no-op sink).** Add the sparse-by-field `ScanLog`
   structure (`patches_by_scan`, `force_changes_by_scan`,
   `rtc_base_changes`, optional `dts` array, `lifecycle_events`) and the
   `LifecycleEvent` dataclass. Populate on every commit. Don't consume the
   log anywhere yet. Measure actual bytes/hour on a representative
   session and confirm the capture is complete (every nondeterminism
   channel is recorded).

2. **Determinism test.** Structure as one case per nondeterminism channel,
   not a single mixed exercise — a regression in any one channel should
   surface as a specific failing test, not a mystery:

   - `test_replay_idle_scans` — N scans, no patches/forces/events.
   - `test_replay_patches` — patches applied at varied scans.
   - `test_replay_forces_add_remove` — forces added, removed, cleared.
   - `test_replay_forces_interact_with_patches` — both on the same scan.
   - `test_replay_rtc_changes` — `set_rtc()` between scans and via the
     `rtc.new_* + apply_*` tag mechanism.
   - `test_replay_lifecycle` — `stop`, `reboot`, `battery_present` toggles.
   - `test_replay_realtime_dt` — varying dt captured from the log.

   Each: run scans, then for every scan, fork from the initial state and
   replay the log up to that scan. Assert the replayed PLC matches the
   live PLC across **every field that replay is responsible for
   reproducing** — not just `tags`/`memory`/`scan_id`/`timestamp`, but
   also:

   - `_rtc_base` and `_rtc_base_sim_time` (RTC clock state)
   - `_time_mode` and `_dt`
   - `_input_overrides._forces` (live force map)
   - `_input_overrides._pending_patches` (should always be empty
     post-commit; assert that too)
   - `_running` and `_battery_present`

   Implement this as a dedicated `assert_plc_state_equal(live, replayed)`
   helper with explicit field coverage, not a generic `==`. A silent pass
   because equality is too lenient is worse than no test.

   **Before trusting any of the per-channel tests, run each once with a
   deliberately-broken replay** and confirm it fails loudly:
   - Skip applying forces → `test_replay_forces_*` must fail.
   - Drop the last patch → `test_replay_patches` must fail.
   - Ignore the `rtc_base` field on the scan entry → `test_replay_rtc_*`
     must fail (this one specifically checks that RTC fields are in the
     equality helper, not just tags/memory).
   - Skip a lifecycle event → `test_replay_lifecycle` must fail.

   This is the foundation — once these pass (and fail correctly under
   mutation), everything else is plumbing.

3. **Checkpoint snapshots.** Stash a `SystemState` reference every K scans
   into a dict. Add `_nearest_checkpoint_at_or_before`.

4. **`replay_to(scan_id)`.** The public entry point. Fork from nearest
   checkpoint, apply log entries forward, return state. Add `_replay_mode`
   guards to the three sites above.

5. **Swap history consumers.** `plc.history.at(scan_id)` switches from deque
   lookup to `replay_to`. Retain a small recent-state window for monitors.
   Delete the bulk of `History`'s storage; keep labels/metadata as a thin
   overlay on the log.

6. **DAP trace regeneration.** When the DAP adapter asks for traces at a
   historical scan, replay with `scan_steps_debug` into a scratch slot.

7. **Tune K.** Once everything works, benchmark scrub latency vs.
   checkpoint memory at different K values on representative programs.

## Success criteria

- Idle-scan memory growth: **zero bytes** — sparse-by-field log means
  idle scans contribute nothing at all.
- 1-hour debug session at 100 Hz FIXED_STEP: **<10 MB** total history
  overhead (vs. original 100 MB/hour target). REALTIME adds ~3 MB/hour
  for the dense dt array; still well under target.
- Fork-at-history-point latency: <100 ms worst case (target, not
  guarantee — depends on K and will be tuned in step 7).
- Determinism test passes for every scan in a 10,000-scan exercise with
  patches, forces, RTC changes, and lifecycle events — **and fails
  correctly when replay is deliberately broken**.
- Existing `plc.history.at()`, `plc.seek()`, `plc.rewind()`, `plc.cause()`,
  `plc.effect()`, `plc.diff()`, `plc.fork()` APIs unchanged from the
  caller's perspective.

## Rung firings storage

Rung firings are the input to `plc.cause()` / `plc.effect()` — they answer
"which rung fired on which scan, and what did it write." The firing log
needs to be O(1)-lookup per scan because analysis queries chain lookups
together, and the total query cost is the sum of those lookups plus a
handful of targeted replays for state inspection.

**Definition of "firing":** a rung "fires" on scan N when its evaluation
produces at least one tag write whose value differs from what was
already pending (`ScanContext.capturing_rung` at `context.py:289-313`
diffs against `_tags_pending`; the call site in the runner is
`runner.py:1429`). A rung whose condition evaluates true but writes
values identical to the already-pending ones does *not* register as a
firing in this log — a deliberate approximation acceptable for
causal-chain attribution. A rung that resets outputs when its condition
goes false *does* register, since that's a genuine state change.

This definition has a nice side effect for storage: a rung that stays
enabled and writes the same stable value doesn't churn the log at all.
Only real state changes produce timeline entries.

### Analysis queries are sparse, not sequential

The concern that "replay multiplies analysis cost by K" was only true
for a naive implementation that replays every scan in a range.
Real analysis doesn't do that. `plc.cause(x)` walks a causal chain,
which is inherently sparse — typically 3–10 steps, each landing on a
specific scan where a relevant transition happened.

The flow is:

1. **PDG**: "X depends on rungs R1, R4, R7."
2. **Firing log**: "R1 fired at scans [145, 892], R4 at [203], R7 at
   [645, 980]." — O(1) lookup per rung.
3. **Replay**: materialize state at each of those 5 scans to inspect
   causal context. Each replay is O(K) — bounded by the checkpoint
   interval, independent of how old the scan is.

Total cost: a handful of targeted replays, each bounded, not a walk of
1000 sequential scans. Same shape for historical watchdog
(`plc.when(Val > 50)` over past scans): PDG narrows to scans where `Val`
was written, firing log returns those scan_ids, replay materializes
state only at candidate scans, predicate evaluates. "Replay from this
checkpoint, then this one, then this one" — not "replay everything."

This is why `cause`/`effect` stay fast under record-and-replay despite
losing direct per-scan state indexing. The firing log gives O(1) access
to "when did relevant things happen"; replay fills in the state at
those specific scans on demand.

### PDG-filtered capture

This is a simulation. The firing log exists to serve the simulator's own
analysis APIs — `plc.cause()`, `plc.effect()`, historical watchdogs,
causal-chain walks. It is **not** a data-historian, not a Modbus feed, not
an external observer's audit log. Nothing outside the simulator reads it.

That fact is load-bearing. It means `capturing_rung` can safely drop
writes to tags that no rung consumes, because by definition no analysis
question can ever depend on them:

- Rung-level analysis (`cause`/`effect`) only chases causal dependencies,
  which by definition means a downstream rung reads the tag.
- State inspection (DataView, UI reads, user assertions) goes through
  replay-reconstructed state, not the firing log. The write still happens
  during replay; it just isn't indexed.

The PDG knows which tags are consumed by some rung. At capture time,
filter writes through it:

```python
def capturing_rung(self, rung_index):
    before = dict(self._tags_pending)
    try:
        yield
    finally:
        pending = self._tags_pending
        writes = {
            name: pending[name]
            for name in pending
            if (name not in before or before[name] != pending[name])
            and self._pdg_consumed_tags.__contains__(name)  # the new line
        }
        if writes:
            self._rung_firings[rung_index] = writes
```

For a typical timer rung where `Timer.done` is gated on by other rungs
but `Timer.acc` is unconsumed: `Timer.acc` writes are dropped silently.
The rung's firing pattern collapses from `{acc: 1420, done: False}` (a
monotonic cardinality bomb) to `{done: False}` (two canonical patterns).
Classic cycle mode, no fallback needed, no threshold dance.

For `Rung(Timer.acc > 50)`: the PDG sees `Timer.acc` as consumed,
captures record it, analysis has what it needs.

### Capture contract and the fallback

The capture layer's contract becomes: **store writes to every tag the
PDG marks as consumed.** Unconsumed writes are dropped at capture;
monotonic consumed writes fall back to fired-only mode (below). Both
reductions together make the common case (timer/counter rungs with
unconsumed acc) trivially cheap.

The three-layer separation still holds:

- **Capture layer** (`capturing_rung`): stores writes to consumed tags.
- **Storage layer** (per-rung RLE timelines, below): compacts those
  writes over time.
- **Analysis layer** (`cause`/`effect` + PDG): interprets which writes
  matter for a specific question.

The PDG is now load-bearing for capture, not just analysis — if it's
wrong (misses a consumer, or incorrectly marks a tag unconsumed), the
firing log silently loses data. Mitigations:

1. The PDG is already used for `cause`/`effect` projection; correctness
   is already required for those to work.
2. A `record_all_tags=True` debug flag skips the filter for cases where
   the user suspects the PDG is wrong.
3. Dynamic rung additions (adding rungs to a running PLC) must
   invalidate the PDG and refresh `_pdg_consumed_tags` before the next
   scan captures.

### Why this is safe here: the two runtime modes

pyrung has two runtime modes with very different history needs:

**Live Modbus soft-PLC.** Runs continuously, external observers present
(Modbus masters, HMI, maybe historians). History here is a short bounded
window — a few seconds for diagnostics, not hours — because production
runtimes don't need long recall. PDG-filtered capture is *nearly* safe
here: external observers can read any tag value, but they read *current
state*, not firing history. The firing log's role is still internal
analysis; external observers don't consult it. Still, for belt-and-
suspenders safety, the `record_all_tags=True` flag gives a clean escape
hatch when a diagnostic session wants the full firing history
unfiltered.

**Debugger / test generation.** Runs in the editor (DAP session) or
under a long-running random-input test harness. No external observers.
The user needs hours of recall to figure out why something misbehaved
or to triangulate a bug from a 10-minute fuzzing run. This is the
primary motivation for record-and-replay in the first place — the 10
KB/scan problem is a debugger problem, not a production problem.

In the debugger mode, PDG-filtered capture is unambiguously safe: the
complete set of consumers is "the program itself + the simulator's own
analysis APIs," both PDG-discoverable. Unconsumed writes cannot affect
any possible query.

The soft-PLC mode doesn't strictly need record-and-replay — a short
bounded deque would work fine for live operation. But having one
unified history mechanism is simpler than maintaining two, and the
record-and-replay path handles the short-history case without penalty
(small log, few checkpoints, O(1) lookup on recent scans). So both
modes use the same machinery, with PDG filtering on by default and
escapable via flag.

### The storage layer: per-rung range-encoded timelines

The naive shape — `scan_id → PMap[rung_index, PMap[tag, value]]` — couples
all rungs into one outer PMap. A single oscillating rung (scan-clock,
timer acc) defeats dedup for the whole program. Every real PLC has at
least one such rung, so this shape is pathological in practice.

Each rung owns its own timeline as a sorted list of ranges:

```python
# rung_index -> ordered list of (start_scan_id, end_scan_id, payload) entries.
# Lookup at scan N: binary search for the entry whose range covers N.
_rung_firings: dict[int, list[RungFiringRange]]

@dataclass(frozen=True)
class RungFiringRange:
    start_scan_id: int      # inclusive
    end_scan_id: int        # inclusive; extended when the pattern persists
    payload: FiringPayload  # PatternRef, AlternatingRun, or FiredOnly (see below)
```

A rung's list appends only when its firing *changes*. A stable rung
firing the same pattern for 10,000 scans is one entry. A cycle-oscillator
toggling between A and B collapses further (see `AlternatingRun` below).
A rung that didn't fire at all on scan N has no range covering N.

Lookup for `rung_firings(scan_id)` assembles the outer-PMap shape on
demand: iterate rungs, binary-search each timeline, collect payloads that
cover `scan_id`. O(R log S) per lookup.

### Three payload flavors

**Cycle mode** — for rungs whose firing patterns revisit a small set of
distinct PMaps. Patterns are interned via PMap hashability:

```python
_firings_intern: dict[int, dict[PMap, PMap]]  # rung_index -> canonical patterns

@dataclass(frozen=True)
class PatternRef:
    pattern: PMap  # reference to the canonical PMap in the intern dict
```

Two scans with the same firing share the same `PMap` object (and by
extension the same `PatternRef`). A rung that goes on-off-on across a
session with long stable runs gets one range per run, all referencing
two canonical PMaps.

**Alternating-run mode** — specifically for period-2 alternation at
scan rate, the scan-clock-toggle pattern. A rung in cycle mode that
produces length-1 ranges alternating A,B,A,B,... would otherwise cost
one range per scan (360K ranges/hour at 100 Hz, ~18 MB). Collapse the
whole run into one range:

```python
@dataclass(frozen=True)
class AlternatingRun:
    pattern_on_even: PMap   # pattern when (scan_id - start_scan_id) % 2 == 0
    pattern_on_odd: PMap    # pattern when (scan_id - start_scan_id) % 2 == 1
```

Lookup at scan N: binary-search to the `RungFiringRange` covering N,
then pick the payload by parity relative to `start_scan_id`. The
alternation cost collapses from ~18 MB/hour to a single range entry
for the whole continuous run.

**Parity is relative to the run's start, not to `scan_id` itself.**
Using `scan_id % 2` directly would give the wrong answer half the time
depending on when the run happened to begin. The dataclass above does
this right; this is the kind of thing that gets "simplified" into a
bug, so guard it with a test.

**Detection rule.** When about to append a length-1 `PatternRef` range,
check: is the previous range also length-1 with a different pattern,
and the one before *that* matching what's about to be appended? That's
an A,B,A signature — an alternating run is forming. Replace the last
two ranges plus the new one with a single `AlternatingRun` covering
all three scans, then extend it by one scan per subsequent alternating
firing. If the pattern breaks (a third distinct pattern, or A appears
twice in a row), close the `AlternatingRun` at the last confirmed
alternating scan and fall back to normal `PatternRef` ranges from
there.

**Period-2 only.** It's tempting to generalize to period-N (A,B,C,A,B,C,...)
but scan-clock is the only rung that actually needs this, and general
period detection opens a rabbit hole: how many repetitions before
committing? What's the maximum period? How do you handle near-periodic
runs with occasional glitches? Keep detection to the single A,B,A
signature; anything more complex stays in normal `PatternRef` mode and
eats the cost.

**Fired-only mode** — for monotonic rungs (counters, timer accumulators
whose `acc` is consumed elsewhere) whose value is different every scan
and can't be usefully interned:

```python
@dataclass(frozen=True)
class FiredOnly:
    pass  # presence of the range says "rung fired"; value recoverable via replay
```

`plc.cause()` / `plc.effect()` primarily need to identify *when* a rung
fired — the transition scan, not every intermediate value. For the rare
case that analysis needs the exact value at a specific scan, replay to
that scan and read state. This is the escape hatch, not the hot path.

### One-way mode transition

Every rung starts in cycle mode. Within cycle mode, the runtime may
collapse alternating-run patterns on the fly (above); this is a
compression within cycle mode, not a distinct mode. If a rung's intern
dict reaches a threshold (100 distinct patterns), it transitions
permanently to fired-only mode for that rung. The transition is one-way
— no flipping back — so the timeline can't oscillate between storage
shapes and produce off-by-one bugs at transition boundaries.

At transition: already-stored `PatternRef` and `AlternatingRun` ranges
stay in place; new firings append as `FiredOnly` ranges. The lookup
handles all three payload types. The intern dict for that rung can be
dropped (nothing new points into it) but existing ranges still resolve.

This gives up optimality for a rung that temporarily explodes then
stabilizes, but that case is rare and the cost is bounded by the
threshold (100 × ~500 bytes = 50 KB per such rung, amortized over the
whole session).

### Eviction: sweep on log-trim

When log retention kicks in and the log trims entries for scans < N:

1. For each rung's timeline, drop ranges with `end_scan_id < N`. Trim the
   start of any range straddling N to `start_scan_id = N`. For
   `AlternatingRun`, trimming must preserve parity — advancing
   `start_scan_id` by an odd delta swaps `pattern_on_even` and
   `pattern_on_odd`; by an even delta leaves them as-is.
2. Walk the intern dicts. Drop canonical patterns no longer referenced
   by any surviving `PatternRef` or `AlternatingRun` in any timeline.

The sweep runs at log-trim cadence, not continuously. Growth between
sweeps is bounded by the log-trim interval, not unbounded. Simple,
correct under all firing patterns (including pathological cycles),
no refcounts to keep in sync.

### Realistic memory budget (rung firings)

For a typical 50-rung program with a scan-clock-toggle rung, 1 slower
cycle-oscillator (say, a 1 Hz flashing lamp), 1 100ms heartbeat, and 1
consumed monotonic counter, running an hour at 100 Hz:

- 47 stable rungs (with PDG filtering dropping unconsumed writes)
  × ~100 real transitions × ~50 bytes/range ≈ 250 KB
- 1 scan-clock-toggle rung: collapses to a single `AlternatingRun`
  covering the whole hour. ~100 bytes total for the range, plus 2
  canonical PMaps in the intern dict. **Effectively zero.**
- 1 slower cycle-oscillator at 1 Hz: `AlternatingRun` doesn't apply
  (half-period is 50 scans, not 1 scan). Falls back to plain
  `PatternRef` RLE: 7,200 ranges × 50 bytes ≈ 360 KB.
- 1 100ms heartbeat: 720 ranges/hour × 50 bytes ≈ 36 KB.
- 1 monotonic counter in fired-only mode: one range per continuous-
  firing streak. Likely ~1 KB.

**Rung-firings total: well under 1 MB/hour.**

### Total session budget

Pulling it all together for a debugger session at 100 Hz, FIXED_STEP, 1
hour:

| Component | Idle-dominated | Busy (active patches/forces) |
|---|---|---|
| Sparse log (`patches_by_scan` etc.) | ~100 KB | ~3 MB |
| `dts` array | 0 (elided) | 0 (elided) |
| Lifecycle events | < 1 KB | < 10 KB |
| Rung firings (all timelines + intern) | ~600 KB | ~1 MB |
| Checkpoints (K=500 → 7 snapshots/hour, structurally shared) | ~1 MB | ~2 MB |
| **Total** | **~2 MB/hour** | **~6 MB/hour** |

In REALTIME mode add ~2.9 MB/hour for the dense `dts` array. Even
under that mode, an hour stays under 10 MB — an order of magnitude
below the 100 MB/hour target. Multi-hour sessions are comfortably
feasible.

## Known subtleties to handle

- **Patch + force ordering.** `apply_pre_scan` applies patches first, then
  forces diff-applied on top. Replay must preserve this — apply
  `entry.patches` before `entry.forces`.
- **`_apply_forces_if_changed` reads current tag state.** This is
  automatically preserved because replay reproduces the pre-scan state.
- **`history_limit` semantics.** Becomes a wall-clock-time or log-byte-size
  bound rather than a scan count. Default to something like "30 minutes of
  log at 100 Hz" (~20 MB). Keep scan-count as a secondary safety valve.
- **`plc.diff(scan_a, scan_b)`.** Requires two historical states. Two
  `replay_to` calls; cache the more recent one.
- **Forked PLCs run in debug mode.** A fork is an ephemeral investigation
  session. Running the fork in debug mode means traces are available for
  any scan the fork has executed — so "fork at N, explore to N+5, inspect
  the trace at N+3" works without re-forking. Debug-mode scan cost is
  higher, but forks aren't throughput-sensitive.

## Out of scope for this migration

- Log persistence / export-import (nice-to-have; add later).
- Parallel/background replay for predictive scrubbing (nice-to-have).
- Compression of the log (probably unnecessary at our sizes).

---

**TL;DR for Claude Code:** Replace snapshot-per-scan `History` with
record-and-replay. The log is **sparse-by-field** — `patches_by_scan`,
`force_changes_by_scan`, `rtc_base_changes` as dicts (only scans with
data); `dts` as a stdlib `array.array('d')` only in REALTIME mode (elided in
FIXED_STEP); lifecycle events as a list. Idle scans cost zero bytes.
Keep `fork()` as the primitive; add `replay_to(scan_id)` that plays
forward from the nearest checkpoint. Guard monitor/breakpoint/RTC-setter
side effects behind `_replay_mode`. For rung firings: filter captures
through the PDG (simulation-only — drop writes to unconsumed tags), then
store consumed writes as per-rung range-encoded timelines with three
payload flavors — `PatternRef` for stable/cycling, `AlternatingRun` for
scan-rate period-2 alternation (scan-clock toggle), `FiredOnly` fallback
when a rung's intern dict exceeds the pattern threshold. One-way
transition from cycle mode to fired-only. Sweep-on-log-trim eviction.
Stage: recorder shim → per-channel determinism tests with full-field
equality and mutation check → checkpoints → replay → swap consumers.
Goal: hours of debug playtime at <10 MB/hour.
