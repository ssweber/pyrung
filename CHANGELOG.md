# Changelog

<!-- Style guide: one sentence per entry. Describe the user-visible effect, not the
     implementation. Group related fixes/features into a single entry when they share
     a theme. Breaking changes and migration steps can be longer — users need the
     specifics. Detail belongs in commit messages and PR descriptions, not here.

     Review and condense before release — entries accumulate during development and
     should be edited into shape before moving from Unreleased to a version heading. -->

## Unreleased

### Features

- `run_until()` and `run_for()` now fold past timer/counter plateaus by default (`fold=True`), computing threshold crossings in closed form instead of stepping scan-by-scan — a 5-second timer at 10ms/scan completes in a handful of real steps instead of 500.
- The projected-oracle writer-selection substrate (overlay/pin/one-hop-derive + counterfactual guard check) now lives in its neutral home `sp_values`, shared by the PILOT planner, `cause(to=)`, and the verifier (removing the pilot→causal dependency); it gains a bounded *backward-enabler* projection that can prove a gate-guarded self-referential step counter cannot advance past its gate — groundwork for the prover to bound such value domains (e.g. an SFC step counter to `{1, 2, 3}`) instead of enumerating them.

### Breaking Changes

- **Click singleton blocks removed.** `from pyrung.click import x, y, c, ds, ...` no longer works. Use the `ClickBlocks()` factory instead — it returns a named tuple of 18 fresh, instance-scoped blocks with no shared mutable state between callers:
  ```python
  from pyrung.click import ClickBlocks
  x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
  ```
- `reset_banks()` removed — block cleanup is automatic via `Block._all_instances` tracking; no manual reset needed between builds.
- `Block.select(OwnTag, OwnTag)` no longer resolves own-block tags to a static `BlockRange` — pass integer addresses instead. Ranges are a hardware concept; use `ds.select(1, 100)`, not `ds.select(FirstTag, LastTag)`.
- `query.cold_rungs()` / `hot_rungs()` and `CoverageReport.cold_rungs`/`hot_rungs` now return rung **labels** as strings (`"3"` for a main rung, `"MySub:3"` for a subroutine rung) instead of integer rung numbers; the `pyrung_coverage` whitelist is string-keyed too (existing integer entries are accepted and coerced for back-compat).

### Fixes

- `run_until()` / `run_for()` time-folding now collapses three more plateau patterns instead of stalling: a clock-gated rung whose body reads only window-frozen state (e.g. `with Rung(rise(clock_1s)): calc(AlarmExtent, alarms.sum())`) no longer caps the whole program's fold at the clock period — its edges are skipped once an edge is observed inert; an unread, never-completing timer/counter folds its ramp to the preset crossing (the Done bit is a visible change) instead of stepping scan-by-scan; and `run_until(Tmr.Acc > 500)` folds onto the predicate's own accumulator threshold instead of overshooting to the preset.
- Coverage surveys now see subroutine rungs: `query.cold_rungs()` / `hot_rungs()` (and the `pyrung_coverage` pytest fixture) account for rungs inside subroutines, so a never-called subroutine is reported cold and an always-firing subroutine rung is reported hot — previously subroutine writes rolled up under the calling main rung, hiding them from coverage entirely.
- `cause()` no longer hangs on subroutine-written tags with long history — the timeline lookup was using PDG node indices instead of main-rung capture indices, falling through to an O(S) state-reconstruction path on every call.
- `cause()` now resolves subroutine writes from branch call sites through the parent rung's firing timeline, so branch-scoped subroutine writers appear in recorded causal chains instead of vanishing during timeline lookup.
- `cause()` now attributes recorded subroutine writes to the semantic writer rung through the PDG, even when timeline storage is keyed by the caller rung.
- `cause()` now descends correctly through subroutine command pipelines: recorded chains name the precise subroutine writer rung from the node firing timeline (instead of dead-ending at the call-site main rung), classify each contact against the state the rung read *when it fired* rather than at end-of-scan (so a command gate consumed later in the same scan — e.g. an abort pulse — is reported at its true at-fire-time value), and surface the call-site caller gate as a linked enabler (`ChainStep.caller_rung_index`) for reversing the whole subtree.
- `cause()` now follows indirect-indexed copy writes (`copy(src, ds[ptr])`) through blocks that exceed the static enumeration cap — previously the chain dead-ended because the PDG couldn't list the destination tag in `writers_of`; now the PDG emits `IndirectWriteRef` descriptors and `cause()` resolves the target at runtime via block membership.
- `cause()` now traces through tags set and immediately reset within the same rung evaluation (e.g. a parameter bit copied before a subroutine call that resets it) — the fire-time view fallback re-evaluates edge-triggered conditions (`rise`/`fall`) at rung entry where the edge is still live.
- Node-level firing capture now merges writes across multiple calls to the same subroutine within one scan (dict union) instead of keeping only the last call's writes; this makes `cause()` and `cold_rungs` see tags written by earlier calls.
- `cause()` no longer reports a written tag as its own root when its rung only has held enabling conditions, leaving those enablers available as unresolved choices.
- `how()` now retries the target corridor after recovery clears the last blocker on a later scan, so next-scan call gates can settle instead of failing with "no recovery goals".
- `how()` now defers derived/elided implementation-detail scratch goals behind program-visible blockers when ordering recovery and writer alternatives.
- `upstream_slice` now includes `exclusive_reads` (timer/counter accumulators) and subroutine call-site conditions by default, fixing five soundness-critical callers (causal `why()`, walk independence checks, waypoint fragility, hold extraction, decomposition overlap) that previously missed accumulator and cross-subroutine dependencies.
- `cause(to=)` and the PILOT planner now resolve targets behind self-referential affine step counters (`calc(CurStep + 1, CurStep)`) and multi-writer one-hot pipeline tags: a shared projected-oracle classifies each candidate writer in the prerequisite state it would fire in — pinning the held one-hot state family and the affine source's one-hop-derived values — so a parity-gated or wrong-state writer is rejected as counterfactual instead of dead-ending or yielding a wrong plan.

### Performance

- `how()` / the PILOT planner no longer stalls on cold-start runs over densely-coupled state machines: each historical-view fork that `cause()` spins up for causal attribution now reuses the parent's tag/edge/constraint index (the program is identical, so re-walking its object graph in `__init__` was pure waste), and the planner's cause-chase memoizes `cause()` per `(tag, scan)` so the same register isn't re-resolved dozens of times across overlapping chains — a single compass observation that fanned into 40+ redundant replays now resolves each register once.
- `cause()` over long folded histories is dramatically faster (≈3× on multi-thousand-scan incidents): the historical-state replay slab now always reaches the requested scan even when folding leaves checkpoints sparse and irregular (it previously capped the slab one interval past the anchor, so a tail scan was never cached and every lookup re-replayed the same interval and then replayed to the tail anyway), keeps one slab per checkpoint interval across a backward walk instead of thrashing a single slot, and reconstructs each at-fire-time rung view by positioning at `target-1` through the compiled replay path rather than an interpreted scan-by-scan run-up.
- The exhaustive verifier (`prove()`, `reachable_states()`, `how()`) builds its analysis context faster: the structural value-domain fixpoint is computed once per run instead of twice across passes, and PDG slice/influence-cone queries are memoized so repeated lookups resolve as dict hits.
- Analysis over programs with large data blocks is faster: writer-membership and slot-default lookups no longer materialize or rebuild whole-block state on every access.

### Features

- **Click codegen emits slot aliases instead of standalone tags.** Generated code now uses `ds.slot(1, name='SpeedCmd'); SpeedCmd = ds[1]` instead of `SpeedCmd = Int("SpeedCmd")` + `TagMap({SpeedCmd: ds[1]})`. One object per tag — the block slot IS the tag. Flat tags never appear in the TagMap; structures and timer clones still do. This applies uniformly: adding a structure annotation to the CSV changes the structures section, not the flat tags. Range instructions get boundary nickname comments (`ds.select(1, 100)  # SpeedCmd..SpeedFbk`).
- One tag per hardware register: `Tag.map_to(ds[N])` registers the tag as the canonical occupant of the bank slot, so `block[addr]` returns the original tag and indirect reads (`ds[expr]`, `dh[expr]`) resolve to the same value as direct references — in simulation, with or without a `TagMap`, and on the twin.
- `TagMap.to_nickname_file()` now writes rows for bank-slot-configured scalars that have no TagMap entry, so nickname CSV round-trips survive the new slot-carried identity.
- Compound `how()` goals protect committed conjuncts: `how(State == 4, Mode == 1)` re-checks earlier conjuncts after each later goal's walk and retries with the clobbering goal first when one regresses (e.g. a mode change that resets the state machine), instead of returning a false "not reachable"; failure diagnoses now name the failing conjunct, and the DAP console's `how` accepts comma-separated comparison conjuncts (`how State == 4, Mode == 1 avoid ...`).
- `ProgramGraph.from_program(prog)` is now available as a classmethod alias for `build_program_graph(prog)`.
- `cause(to=)` and `effect(from_=)` now handle `copy()` and `calc()` instructions — projected causal chains trace through computed values instead of returning false "unreachable" for non-coil rungs.
- `effect(from_=, to_value=)` accepts an explicit destination value for non-Bool tags, enabling what-if analysis on integer and real tags.
- `why()` now traces through subroutine rungs — programs using `call()` get full causal attribution across subroutine boundaries instead of stopping at the call site.
- `why()` output groups steps by subroutine with section headers (`--- subroutine_name ---`) and includes a legend for blocked (`*`) steps.
- `how()` appends a `Commands:` block with executable `force`/`step`/`clear_forces` lines that can be copy-pasted into the console or saved as a replay transcript.
- `simplified()` now carries subroutine call guards into the resolved Boolean form, so outputs gated by a subroutine's calling context reflect the full reachability condition.
- `copy(True/False, BoolTag)` now emits a warning — use `latch()`/`reset()` instead, which are the correct PLC coil instructions for boolean tags.
- `PLC.fork()` now propagates installed Harnesses — forked runners inherit feedback couplings and pending patches, so feedback timing is preserved across forks without manual re-installation.
- `how()` now respects `Physical` feedback timing: linked feedback tags (via `link=`) are driven by the Harness instead of being steered directly, and profile-gated goals (analog ramps to a comparison threshold) are solved by holding inputs while the profile advances.
- `Harness.unlink(["Feedback"])` drops named couplings so the Harness no longer synthesizes feedback for those tags — models a broken sensor or fault scenario. Also available as `how(unlink=["Feedback"])` to force feedback tags directly in path search.
- `how()` finds plans through Or-gated writers via state-aware regression: when static extraction and oracle recovery come up empty, a frontier-terminated `why()` on the live fork traces the nearest actionable sub-goals through the active branch of Or-gates.
- `how()` can learn per-walk temporal cycle rules from recursive cause evidence, using timer-derived dwell bounds to validate alternating input recovery candidates instead of fixed delays.
- Crossing registry `forward()` now classifies writer output values: `Literal(value)` for fixed-value writers (copy-literal, latch, reset, fill-literal, copy-readonly), `Affine(source, scale, offset)` for self-referential calc patterns (`calc(tag ± N, tag)`). `_written_value_for_tag` is now a thin wrapper over the registry; callers use `isinstance(result, Literal)` / `isinstance(result, Affine)` instead of tuple tags.

### Fixes

- `ProgramGraph` and `why()` now see static copy fan-out, converted copy/blockcopy fault writes, range/status writers, and specific writer labels across public `Rung` instructions instead of hiding those effects behind missing or generic writes.
- Implicit fault-flag writes (`fault.division_error`, `fault.out_of_range`, `fault.address_error`) no longer appear in `writers_of` or `RungNode.writes` — they are tracked on `RungNode.implicit_writes` (with `all_writes` as the union property), so `always()`/`how()` cone computations no longer widen through every `calc()`/`copy()` rung when a program conditions on a fault flag.
- `strict=True` now catches `comment()` inside a `with rung():` body — the call silently attaches to the wrong rung (or is lost), causing missing `#` comment rows in exported ladder CSV.
- `simplified()` no longer collapses to `True` on reset-dominated outputs — outputs whose reset path is always satisfiable now correctly resolve to their set-path condition instead of an unconditional `True`.
- `Condition.__bool__` now raises `TypeError` instead of silently returning truthy — prevents `assert val == SomeTag` from passing vacuously and catches accidental boolean use of condition objects at the call site.
- `CompareEq`/`CompareNe` and all `IndirectCompare*` conditions now resolve `IndirectRef` operands during `evaluate()` instead of comparing the raw object reference — fixes incorrect condition results when comparing a tag value against an indirect array element (e.g. `DebugStep == Step[CurStep]`).
- `_is_zero_literal` in the prover's absorption pass no longer misidentifies Tag/IndirectRef sources as zero literals — the old `value == 0` produced a truthy `CompareEq` instead of `False`, which could cause incorrect threshold absorption.
- Compiled kernel now initializes block slots with per-slot defaults from `default_factory` instead of flat type defaults (0). Blocks accessed only via indirect addressing (`blk[Idx]`) were affected; interpreted runner was unaffected.
- `how()` no longer crashes with `'method' object is not iterable` when the target path traverses a `FillInstruction` — `BlockRange.tags` is a method, not a property.
- `how()` no longer hangs on programs with large cyclic state machines: every search is now bounded by a deterministic kernel-evaluation budget (returning a bounded "not reachable within budget" instead of running forever), and the waypoint planner skips decomposition for near-program-sized cones that the undecomposed search solves no slower.
- `how()` no longer reports a reachable multi-step target as unreachable when its waypoint cone is padded by combinationally-derived signals (alarms, warnings) or fixed setpoints: the planner now gates decomposition on the cone's search-relevant width — stateful tags and wide nondeterministic inputs — rather than its raw tag count, so a target like `how(fill_solv_nc, avoid=HMI_fill)` solves in seconds instead of exhausting its evaluation budget.
- `always()`/`never()` no longer return a false `Proven` when the property couples inputs the program logic treats independently (e.g. `never(A, B)` where `A` and `B` gate unrelated rungs): inputs the property reads directly are now kept live (never pruned) and excluded from free-input factoring, so the coupled corner is always explored.
- Click codegen no longer emits `default=` for retentive registers — retentive tags keep their last value across power cycles, so the CSV initial_value is not a meaningful simulation default; they now get the type zero (0/False/"") instead.

### Performance

- Recorded `cause()` now derives input-only transitions from `ScanLog` replay history on demand, preserving fast TimelineView lookups without an unbounded duplicate input log.
- `classify_dimensions` pipeline pass is ~8× faster on block-heavy programs (49.5s → 6.4s on BurnerLoop) by caching resolved tag names on `BlockRange` objects.
- `how()` waypoint decomposition now caches the outer context's classification, domain seeding, and threshold absorption results for reuse in per-waypoint context builds. On large programs (~2800 tags), this eliminates ~150 seconds of repeated static analysis per waypoint.
- `how()` waypoint ordering uses Tarjan's SCC algorithm to merge only true dependency cycles into mega-waypoints, instead of merging all remaining waypoints when the dependency graph has no zero-in-degree nodes.
- `how()` sub-decomposes SCC mega-waypoints when the primary tag steps through intermediate values (e.g. a step counter going 0→1→2→3). Detects literal-write transitions via direct condition guards and a recursive, depth-bounded multi-hop chase through timer/interlock enablers, producing per-step sub-waypoints with tighter cones instead of one large BFS.
- `how()` waypoint discovery uses landmark extraction (Hoffmann et al. 2004) to prune non-essential waypoints — only facts required by ALL first achievers of a downstream goal survive. Waypoint ordering merges greedy-necessary and reasonable orderings for better sequencing of coupled state machines.
- `how()` recognizes `calc(tag + N, tag)` and `calc(tag - N, tag)` arithmetic patterns for SCC sub-decomposition, enabling step-counter programs that use `calc()` instead of `copy()` to decompose into per-step sub-waypoints.
- `how()` frontier-based refinement: when a waypoint's mini-BFS exhausts its depth budget, the planner samples frontier states and mines new intermediate predicates — value gaps, condition-blocking tags, and dependency-chain roots — to synthesize sub-waypoints and retry before falling back to undecomposed BFS.
- `how()` domain seeding fills intermediate values for arithmetic writers (`calc(tag + N, tag)`) and threads seeded domains into frontier refinement as a fallback when the static transition graph can't parse the pattern.
- `how()` fallback BFS uses `max_states=10_000` (down from 100K) to prevent out-of-memory on large programs.
- `how()` probes the compiled kernel to widen a waypoint's search cone when the static cone proves a target unreachable, recovering dependencies — including transitive intermediaries — reachable only through indirect array addressing (`blk[Idx]`) that the program-dependence graph cannot trace, so the waypoint planner can solve such targets instead of falling back to undecomposed BFS.
- `how()` decomposes cyclic counter sequencers whose advance gate cannot be statically inverted (modulo-parity auto-advance, deep timer/sub-state enabler chains) by domain-stepping the counter through its declared values, keeping only the rest-states — a one-scan kernel probe drops transient values the machine passes through within a single scan and which no settled state could satisfy — and restricts each per-step mini-BFS to vary only the nondeterministic inputs inside its from-value-narrowed cone, so a step gated by one command no longer branches over every unrelated plant input.
- `how()` collapses a wide-range analog input (e.g. a 0–100 tank level) to its few behaviorally-distinct band-crossing values when it gates a step only through downstream arithmetic and a comparison, so the per-step search no longer enumerates all ~100 values and exhausts its evaluation budget.

### Internal

- `Block` slot overrides consolidated from 15 parallel per-field dicts into a single `addr → SlotConfig` map; no behavior change.

## v0.10.0 (2026-06-03)

### Features

- `plc.why(*tags)` — backward reachability from a frozen snapshot, no scan history required. Load a tag dump from a faulted machine, call `why(Alarm)`, and get the causal path through the program: which instructions wrote each tag, which contacts matter, and which external inputs are at the root. Handles both "why is this ON?" and "why isn't this running?", with latch/reset path analysis and multi-tag merging. Available from the DAP console (`why Tag1 Tag2`) and `pyrung live`.
- `plc.how(condition)` finds the minimum input-change sequence to reach a target state from the current snapshot, with `avoid=` and waypoint decomposition for multi-step targets. Heuristic domain seeding resolves programs with unbounded tag-to-tag comparisons (cross-correlated Reals, calc/copy chains). Path output shows semantic constraints (`Pressure > Setpoint`, `Temp=51 (> 50.0)`) with only changed inputs per step. DAP console syntax: `how State == RUNNING avoid State == FAULTED`.

- `ladder_to_pyrung_project()` now emits a complete agent workspace: `CLAUDE.md` and `AGENTS.md` with program-specific metadata (rung counts, subroutine descriptions, tag distribution, tractability estimate), `click-cheatsheet.md` (bundled as package data), `.claude/settings.json` (tool permissions), four `.claude/skills/` workflow definitions (diagnose, fix, review, failure), and a `tests/` scaffold with a smoke test and coverage plugin. New `machine_name` parameter sets the CLAUDE.md header.
- `ladder_to_pyrung_project` preserves user-edited scaffolding files (pyproject.toml, README.md, .vscode/) on rebuild — only logic files are regenerated. Pass `overwrite=True` to force-write everything.
- `pyrung_to_ladder(..., index=True)` numbers rung markers sequentially (R1, R2, ...) instead of bare `R`; counter restarts per program scope. `ladder_to_pyrung` / `ladder_to_pyrung_project` now accept CSVs with numbered Rn markers.
- `ladder_to_pyrung_project(..., index=True)` annotates each emitted `with rung():` line with an inline `# R1`, `# R2`, ... comment showing the 1-indexed rung position; counter restarts per file. Continued rungs are not annotated.

- DAP console / `pyrung live`: new `get <tag> [tag2 ...]` command prints current tag values without the overhead of `why`.
- DAP `launch` accepts an optional `snapshotPath` argument — a path to a Click CSV data dump. When provided, the PLC is seeded with the snapshot values as its initial state, so the simulation starts from real plant data instead of defaults.
- `StuckBitReport.grouped()` collapses stuck-bit findings that share a write site into one `StuckBitGroup` — a range reset/fill that clears a whole block of coils now reads as a single entry (with `.common_prefix` and per-tag `.findings`) instead of one near-identical finding per tag.


### Breaking changes

- DAP `reload` now re-imports all `.py` files from the program directory instead of relying on Python's module cache. Previously, editing a subroutine file (e.g. `io.py`) and running `reload` would silently keep the old logic. The `watch`/`unwatch` console commands are renamed to `autoreload`/`autoreload off` to avoid confusion with DAP watch expressions. **Migration:** replace `watch` with `autoreload` and `unwatch` with `autoreload off` in any scripts or muscle memory.
- DAP `launch` accepts a new `autoReload` boolean argument. When `true`, the adapter monitors all `.py` files in the program directory and automatically reloads on changes (equivalent to typing `autoreload` in the console). The generated `launch.json` now includes `"autoReload": true` by default.
- `Char` tag default is now `"\x00"` (null character) instead of `""` (empty string), matching Click TXT register hardware default ($00). Empty strings in assignments and comparisons are normalized to `"\x00"` — existing code like `State == ""` continues to work.
- `prove()` is renamed to `always()` and a new `never()` complement is added. `always(logic, condition)` proves the condition holds in every reachable state; `never(logic, A, B)` proves `A and B` is never simultaneously true. **Migration:** replace `from pyrung.core.analysis import prove` with `from pyrung.core.analysis import always` (and/or `never`), then rename call sites. The `prove` module path (`pyrung.core.analysis.prove`) is unchanged. The DAP console command is now `prove always <expr>` / `prove never <expr>`.

### Changes

- Rung references in all user-facing output are now **1-indexed** to match Click/PLC convention and the 1-indexed `Block` pattern — the first rung is `Rung 1`. This covers `cause()`/`effect()`/`why()` chain output, validation findings (`rung N`), debugger stack-frame labels, and the values returned by `plc.query.cold_rungs()` / `hot_rungs()` and emitted in the coverage report JSON. **Migration:** if you maintain a coverage whitelist (`[cold_rungs] allow = [...]`), add 1 to each rung number — a previously whitelisted index `22` becomes `23`. Internal/structural data (`ChainStep.rung_index`, `CausalChain.rungs()`, DAP `rungId`/`rungIndex` trace fields, `to_dict()`/`to_config()`) remains 0-based.
- `ChainStep.proximate_causes` and `ChainStep.enabling_conditions` are renamed to `.triggers` and `.enablers` — the old names remain as read-only properties. Serialization (`to_dict()`) now emits `"triggers"` / `"enablers"` keys.
- `PLC.state` is now the preferred property for accessing the current state snapshot; `current_state` remains as an alias.
- `always()` / `never()` / `reachable_states()` now report all infeasible tags at once instead of stopping at the first pipeline pass that finds a blocker — unbounded-domain tags from classification and unclassified tags discovered during elision appear together in a single `Intractable`, with a state-space estimate from the surviving dimensions.

### Performance

- `always()` / `never()` / `reachable_states()` now factor independent free inputs into separate groups, evaluating each group independently and composing via delta merge instead of enumerating the full cross-product. Programs with 3+ independent input groups see ~3x speedup.
- `split_at=["AutoMode"]` on `always()` / `never()` / `reachable_states()` (also `__lock__["split_at"]`) promotes a stateful coupling tag to nondeterministic, enabling factoring across zones that would otherwise be inseparable. `Intractable` hints now suggest candidates automatically.
- `_grid_to_graph` (ladder codec) is ~4x faster — per-cell function calls replaced with flat arrays and precomputed connectivity bitflags.
- `pyrung_to_ladder(..., validate=False)` skips pre-export checks and round-trip verification for a ~8x speedup (22 ms → 3 ms on a realistic program). Default remains `validate=True`.
- Tag construction with an explicit name (`Bool("Pump")`) no longer runs AST inference, eliminating ~2 ms per tag of `executing` library overhead. Programs with hundreds of named tags see noticeably faster load times.
- `SystemState` scan commits no longer write `_prev:*` entries for non-edge tags, reducing per-scan overhead.

### Fixes

- `always()`/`never()`/`reachable_states()` now validate that kernel-produced values respect user-declared `min=`/`max=`/`choices=` bounds before BFS exploration. Programs where `calc()` or other instructions write values outside declared constraints raise `ValueError` immediately instead of silently using wrong domains.
- Unwritten tags are now auto-promoted to nondeterministic inputs — `external=True` is no longer required for tags the program never writes to. `external=True` remains meaningful for tags that are both written by the program and changed externally.
- Under-specified nondeterministic tags (no `min`/`max`/`choices`, no comparison-derived domain) now surface as `Intractable` instead of silently defaulting to `(0,)`. Add `readonly=True` for genuinely constant tags or declare bounds for HMI/operator inputs.
- `Char` tags now participate fully in domain inference — string-literal copies (`copy("g", State)`) and string comparisons (`State == "g"`) are recognized as domain values. Previously Char state machines were classified as `Intractable`.
- `how()` replay verification no longer fails on paths that require `rise()`/`fall()` transitions through edge-demoted tags — BFS traces now carry the prev values needed for edge detection during replay.
- Hidden-event jumps (timer/counter fast-forward) no longer produce unreplayable traces when the jump fires on an input step that transitioned into an already-visited state — jumps now fire only as a self-loop on the current plateau. Affects `how()`, `always()`, `never()`.
- Slice elision no longer incorrectly elides conditionally-written tags that have no readers — a latch or conditional copy whose entry value persists on the no-write path is now correctly kept as cross-scan state.
- `prove()` no longer returns a false `Proven` (missed violation) when a free input gates a `receive()` whose destination is itself a nondeterministic tag — free-input factoring now keeps the gating input and the written destination in the same group instead of evaluating them independently and dropping the states where the receive does not fire and the injected value survives.
- Pointer-default validator now suppresses findings when a `copy()` or `calc()` unconditionally writes the pointer before any dereference, including writes behind `return_early()` guards where both the write and all reads share the same guard.
- Stuck-bit validator now recognizes `copy()` to a Bool tag as a latch or reset — `copy(True, C)` counts as a latch, `copy(False, C)` as a reset, and `copy(tag, C)` as both, eliminating false stuck-high/stuck-low findings.
- Conflicting-output and stuck-bit validators now detect mutual exclusivity when subroutine callers compare a tag against another tag (e.g. `DS404 == ModeTag` vs `DS404 != ModeTag`) — previously only literal-value comparisons were recognized.
- Choices validator now correctly inspects `fill()` instructions — previously used stale attribute names, causing choice violations on fill targets to go unreported.
- `prove()` domain inference for Real tags with fractional `min`/`max` bounds no longer silently truncates to integers — fractional bounds now seed the partition path, producing a correct finite domain instead of an empty or integer-only one.
- Scoped kernel snapshots no longer drop Char tags written by text fan-out (`copy_convert` with `to_text`, or string-literal copies) — dynamically-created sequential keys are now captured and cleaned up on restore.
- `always()`/`never()` no longer returns a false `Proven` for oneshot `calc()`/`copy()` accumulators — threshold absorption incorrectly classified oneshot writes as constant-stride progress sources, but oneshot instructions only fire on rising edges, not every scan.
- Comparing a `choices`-typed tag against a label string (e.g. `StateCurrent == "IDLE"`) now resolves the label to its underlying key — previously the comparison used the literal string, producing a type mismatch in domain inference.

## v0.9.2 (2026-05-21)

### Changes

- The VS Code debugger extension now resolves the Python interpreter from the `ms-python.python` extension instead of defaulting to bare `python`, and shows a clear error if pyrung is not installed in the selected environment.
- Python 3.11 is supported again — the minimum version is now `>=3.11`.
- `ladder_to_pyrung` and `ladder_to_pyrung_project` now emit `default=` on tag declarations when the nickname CSV carries a non-zero `initial_value`, and inject standalone nickname tags that aren't directly referenced in any rung so they still appear in `tags.py` with their TagMap entry.

### Fixes

- `prove()` no longer returns false results (false `Proven` or false `Counterexample`) in programs with self-resetting counters, counter accumulator comparisons, absorbed condition-gating tags, or `return_early()`/pulse/reset patterns — the traced elision pass has been replaced by a sound-by-construction slice analysis.
- `reachable_states()` no longer misses states when an input drives both a timer enable condition and a downstream comparison through a copy/calc chain.
- `prove()` / `reachable_states()` no longer report Intractable for tags written by indirect-ref copies (e.g. `copy(block[pointer], target)`) — the domain classifier now resolves the pointer's finite domain to bound the target.

### Internal

- `prove()` soundness is now cross-checked by a subset-differential fuzzer that runs every optimization subset — not just all-on — against the unoptimized baseline, catching interaction bugs between optimizations that an all-optimizations-on check misses.
- Traced influence-graph elision replaced by slice elision (sound-by-construction write-before-read enumeration), eliminating a class of soundness bugs while recovering aggressiveness through two new projection passes (functional-dependency and init-constant).

## v0.9.1 (2026-05-19)

### Fixes

- `prove()` / `reachable_states()` could miss violations or reachable states in programs using copy/calc chains, edge-triggered conditions with transient outputs, subroutine-scoped writes, or `latch()` targets.
- Multiple timers expiring on the same scan now correctly reach all combined output states in `reachable_states()`.
- `reachable_states()` now retains locked tags that `prove()` elides, ensuring lock checking explores all reachable values.

## v0.9.0 (2026-05-18)

### New features

- Tag-name inference — `Bool()`, `Int()`, `Real()`, `Dint()`, `Word()`, `Char()` infer their name from the assignment target, so `Foo = Bool()` is equivalent to `Foo = Bool("Foo")`. Typed block constructors (`IntBlock`, `BoolBlock`, `DintBlock`, `RealBlock`, `WordBlock`, `CharBlock`) provide the same inference for memory blocks. Existing explicit names are unaffected.
- DINT truthy conditions — `Rung(dint_tag)` now works the same as `Rung(int_tag)` (nonzero = true); Click validation catches both with `CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED`.
- `rung` lowercase alias — `rung` is now the preferred spelling; `Rung` still works.
- `__lock__` `joint` / `exclusive` input group keys — `joint_inputs=` replaces `input_groups=`; new `exclusive_inputs=` prunes mutually exclusive input combinations from the state space.
- `prove(paced=True)` — forces a stutter scan after any input change, suppressing violations that require back-to-back input flips with no settling time. An automatic aggressive second pass attaches `aggressive_counterexample` to `Proven` results.
- `prove(journal=True)` — per-tag `Journal` showing classification, domain inference, elision, and absorption decisions with proof detail for diagnosability.

### Breaking changes

- Verifier `depth_budget` rename — `max_depth` / `--max-depth` renamed to `depth_budget` / `--depth-budget` on `prove()`, `reachable_states()`, `check_lock()`, and CLI commands.

### Performance

- Scan hot-path micro-optimizations — condition `evaluate()` methods resolve deferred imports, `isinstance` checks, `_contact_tag` resolution, and f-string allocation once at construction instead of every call. Branchless rungs skip branch-enable-map allocation.
- `prove()` significantly faster — optimizations across both state exploration (edge-source demotion, cached walks, identity short-circuits, reduced `isinstance` overhead) and the compiled kernel (per-type store helpers, codegen improvements).

### Fixes

- `prove()` / `reachable_states()` — substantially reworked soundness, backward propagation, and counterexample fidelity, backed by agreement oracles, known-answer tests, and fuzz coverage.
- `reachable_states()` now settles chained hidden events (e.g. counter Done firing a second counter via a transient boolean), fixing missed reachable states.
- `pyrung live` shows usage hint when invoked with no command.
- `prove()` now models time drum instructions as timer-like progress sources, enabling correct reachability proofs for drum-driven outputs and step advancement.
- Off-delay timer (`TOF`) initial Done state is now False when the enable has never been True, matching Click PLC hardware.
- Oneshot `out()` writes False after firing instead of retaining the entry value, matching Click spec.
- `blockcopy()` and `fill()` with indirect ranges set `fault.address_error` instead of crashing on out-of-range pointers.
- `calc()` treats expression overflow as an out-of-range math fault instead of crashing.
- `forloop()` rejects non-positive literal counts; tag-based counts resolving to zero or negative execute one iteration.
- Compiled kernel parity — copy converters (`to_text`/`to_value`/`to_ascii`) expand into sequential tag writes, address-fault classification preserved for indirect sources, and block tag materialization matches interpreted behavior.
- Interpreted runner parity — subroutine-only tags seeded at scan 0, `forloop(..., oneshot=True)` latch stored in scan memory, and instruction memory keys use stable sequential IDs instead of `id()` values.

### Internal

- `prove()` elision replaced with trace-based approach — instruments actual program execution to build a dependency graph, then backward-cone analysis from observers determines elidable tags, replacing the previous static analysis.
- Interpreted runner and prover share the same execution walker (`execute_program`).
- Fuzz reproducers are structurally minimized via delta-debugging before being written.
- Prove agreement oracle — `pytest --prove-agreement` re-runs every `Proven` result with optimizations disabled; opt out with `@no_agreement`.
- Known-answer reachability oracles — `pytest -m known_answer` for hand-enumerated `reachable_states()` ground-truth tests.
- Fuzz test duration configurable via `FUZZ_MAX_EXAMPLES` and `FUZZ_SCANS` environment variables.

## v0.8.0 (2026-05-04)

Major overhaul of `prove()` and `reachable_states()`. Single-flip BFS, pre-BFS elision via abstract interpretation, accumulator absorption (threshold vectors and comparison-only), and a blockless compiled kernel mode (~8× faster steps) together make `pyrung lock` practical on industrial-scale programs that previously hit `Intractable`.

### Breaking changes

- Python 3.12 minimum — bumped from 3.11.
- Lock file default projection is now `lock=True` tags — programs using `TagMap` get physical outputs automatically; others need explicit `lock=True` or `__lock__ = {"include": [...]}`.
- Lock file omits False values — each state now reads as "what's ON"; `check_lock` handles both formats transparently.

### New features

- `lock` tag flag and `TagMap` auto-stamping — new `lock` flag includes tags in the default `pyrung lock` projection; `TagMap` auto-stamps `lock=True` on output-mapped tags and `external=True` on input-mapped tags, and `InputBlock` tags are automatically treated as nondeterministic.
- `band` tag attribute — predicate-based value grouping (`band={"ZERO": 0, "POSITIVE": "> 0"}`) collapses numeric lock file values into categorical labels.
- `__lock__` `joint` key — declares multi-flip input groups for BFS exploration of inputs that must change in the same scan.
- Lock file improvements — progress reporting with queue trend arrows, choice labels instead of raw integers, and `--profile` flag for cProfile output.
- `Intractable.hints` — dimension diagnostics listing the largest state-space contributors when `prove()` or `reachable_states()` returns `Intractable`.
- Pointer-default core validator — `CORE_POINTER_DEFAULT_BEFORE_BLOCK_START` catches the common 1-based block + `default=0` mismatch before runtime.
- Click `[choices=Bool]` shorthand — nickname CSV comments accept `[choices=Bool]` for int-backed boolean dropdowns.
- `UnpackToBitsInstruction.dest` / `UnpackToWordsInstruction.dest` — property aliases matching the `dest` convention used by all other packing instructions.
- New examples — `fill_station.py` (Physical annotations, Harness, `prove()` fault coverage) and `packml_bench.py` (industrial-scale profiling benchmark).

### Fixes

- `call("missing")` now fails at build time instead of compiling cleanly and crashing at scan time.
- Mixed-type values in lock file state sorting no longer raise `TypeError` when choice labels mix with raw integers.

## v0.7.0 (2026-04-26)

### Breaking changes

- Lock file default projection is now terminals — existing lock files generated with the old public-first projection will need regeneration with `pyrung lock`.

### New features

- `__lock__` module-level projection override — `__lock__ = {"include": [...], "exclude": [...]}` customizes which tags the lock file tracks beyond the terminal default.
- Public `Coupling` API on `Harness` — `harness.couplings()` yields `Coupling` dataclasses for iterating all discovered enable→feedback pairings.
- `plc.tags` read-only tag mapping — `MappingProxyType[str, Tag]` of all known tags by name for introspection and test assertions.
- `prove()` settle-pending semantics — `prove()` now settles pending timer/counter Done bits before evaluating, eliminating false negatives for properties guarded by timing.
- `SumExpr` CircuitPython codegen — `BlockRange.sum()` expressions now compile to CircuitPython code.
- Fault coverage example — new `examples/fault_coverage.py` demonstrating `prove()`, `cause()`/`recovers()`, and the coverage plugin.
- `TraceStep` dataclass for counterexample traces — enables accurate replay of timer/counter fast-forward edges.

### Fixes

- `prove()` domain coverage — boundary partitions now emit lit-1/lit/lit+1, property expressions feed into domain analysis, and memory-backed state is included in the visited-state key.

### Internal

- `_AnalogCoupling` renamed to `_ProfileCoupling` for consistency with the `Physical` API terminology.

## v0.6.0

### Breaking changes

- `PLC(history_limit=...)` replaced by `history` / `cache` / `history_budget` — three knobs replace the single snapshot-count parameter: `history` (retention window, e.g. `"1h"`), `cache` (instant-lookup window), and `history_budget` (byte ceiling, default 100 MB).

### New features

#### Declare — tag metadata and physical annotations

- Tag flags: `readonly`, `external`, `final`, `public` — three semantic flags enforced by static validators plus one presentation flag for Data View visibility, with mutual exclusivity enforced at construction.
- `choices` tag metadata — tags carry a `choices` mapping (value→label) through DAP traces, Click CSV round-trip, and VS Code debugger dropdowns.
- `Physical` annotations and autoharness — `physical=`, `link=`, `min=`, `max=`, `uom=` on tags declare device feedback behavior (bool timing or profile functions); `Harness` reads these and auto-synthesizes feedback patches, replacing hand-written test toggles.
- Click nickname CSV physical metadata — tag flags and physical metadata (`min`/`max`/`uom`) survive the nickname CSV export/import cycle.

#### Analyze — static validators, causal chains, and test coverage

- `Program.validate()` with `select`/`ignore` filtering — unified validation entry point with dialect, mode, and finding-code filtering.
- Static validators — stuck-bit detection (`CORE_STUCK_HIGH`/`CORE_STUCK_LOW`), readonly write, choices violation, final multiple-writers, and physical realism checks (`CORE_RANGE_VIOLATION`, `CORE_MISSING_PROFILE`, `CORE_ANTITOGGLE`).
- Runtime bounds checking — tags with `min`/`max` or `choices` are checked per-scan; violations populate `plc.bounds_violations` without clamping values.
- Static program graph analysis — `build_program_graph()` produces rung summaries, `TagRole` classification, and SSA-style def-use chains.
- `plc.dataview` — chainable query API with role/physicality filters, abbreviation-aware name matching, and dependency slicing (`.upstream()`, `.downstream()`).
- `program.simplified()` — resolves each terminal's condition chain back to inputs, eliminating intermediate pivots while preserving series/parallel topology.
- `plc.cause()` / `plc.effect()` — causal chain analysis attributing proximate causes vs enabling conditions, with projected mode for reachability queries and what-if analysis.
- Mixed-fidelity causal chains — recent steps use full SP-tree attribution; older steps fall back to timeline-based approximation when state is out of cache.
- `assume={}` on `cause` / `effect` / `recovers` — scenario-pinning parameter that overrides tag values for projected walks without mutating state.
- `plc.recovers(tag)` — convenience predicate: `True` if the tag has a reachable clear path from the current state.
- `plc.query` namespace — `cold_rungs()`, `hot_rungs()`, `stranded_bits()` surveys with `report()` for mergeable `CoverageReport` objects.
- Pytest coverage plugin — `pyrung_coverage` fixture collects per-test reports, merges at session end, with CI gating via TOML whitelist (`--pyrung-whitelist`).
- Digital twin test harness (`pyrung.twin`) — plain-English `case("sentence", ladder=fn, expect={...})` test slots with `assert_all_passed(results)`.
- Exhaustive state-space verification (`prove()`) — BFS over reachable states using the compiled kernel; returns `Proven`, `Counterexample` (replayable trace), or `Intractable`.
- Lock file workflow (`pyrung.lock`) — `write_lock()` / `check_lock()` serialize reachable states to JSON; behavioral changes show up as diffs in PRs.
- Unified `pyrung` CLI — `pyrung lock`, `pyrung check`, `pyrung dap`, and `pyrung live` commands.

#### Commission — VS Code debugger and live tooling

- Hot-reload (`reload`, `watch`, `unwatch`) — re-execute the program file preserving PLC state; `watch` auto-reloads on save.
- VS Code Data View — panel for watching, forcing, and patching tags with live inline values, flag badges, and public-only filtering.
- VS Code Graph View — interactive Cytoscape.js tag dependency graph with role coloring, upstream/downstream slicing, and live value badges.
- VS Code Chain tab — interactive causal queries (`cause`/`effect`/`recovers`) in the History panel.
- Debug console command system — typed command dispatcher with verbs for stepping, forcing, analysis, monitoring, and annotation.
- `pyrung live` CLI — attach to a running debug session from another terminal with semicolon-chained commands and session discovery.
- Session capture pipeline — `record`/`replay` captures replayable transcripts; a condenser shrinks to causal-minimum reproducers and an invariant miner proposes candidates that generate pytest verification files.

#### Infrastructure and DX

- Byte-budgeted recent-state cache — `history.at()` serves cached scans directly; older scans reconstruct via replay from the nearest checkpoint.
- Timeline-routed transition finding — `cause()`/`effect()` consult per-rung firing timelines before touching state, eliminating per-contact `history.at()` reads.
- Modern Click timer/counter codegen syntax — `ladder_to_pyrung()` emits positional presets and friendly unit strings.
- Type stubs for IDE inference — `tag.pyi` gives IDEs accurate type information for tag imports and `Block` fields.

### Performance

- Sparse scan log + compiled replay kernel — history records only nondeterminism (idle scans contribute zero bytes) and reconstructs older states via a compiled kernel operating on plain dicts instead of immutable `SystemState` objects.
- Reduced per-scan memory overhead — system points are derived at read time instead of written into the PMap each scan.

### Bug fixes

- Modbus `send`/`receive` latching semantics — status flags now latch on completion and persist across disabled scans, matching Click PLC docs; `conflicting_outputs` validator now covers send/receive status tags.
- Snapshot-stable instruction helper conditions — `.reset(...)`, `.down(...)`, `.clock()` and drum inputs now evaluate against the rung's frozen `ConditionView` instead of live mid-rung writes.
- Click subroutine export filenames — `LadderBundle.write()` preserves original filenames instead of slugifying them.
- VS Code webview script regressions — fixed template-literal escaping bugs; `make lint` now syntax-checks embedded webview scripts.
- Derived edge detection on system clock tags — `rise()`/`fall()` on derived tags now uses a derived-edge registry instead of the broken `_prev:*` fallback.
- `scan_counter` wraps at 32768 to match the Click SD9 spec.
- Send/receive I/O replay — scan log now records I/O events for correct state reconstruction during history replay.
- Sparse block-element commit semantics — only elements actually written during a scan are committed to state.

### Migration

- Replace `PLC(logic, history_limit=N)` with `PLC(logic, history="1h")`, `PLC(logic, cache="5m")`, or `PLC(logic, history_budget=bytes)` — or drop the argument entirely to accept defaults.

## v0.5.2 — Friendlier timer/counter API

### New features

- Positional `preset` and `unit` — `on_delay`, `off_delay`, `count_up`, and `count_down` now accept positional arguments: `on_delay(MyTimer, 3000)`, `on_delay(MyTimer, 5, "sec")`. Keyword form still works.
- Human-friendly time units — `unit=` accepts `"ms"`, `"sec"`, `"min"`, `"hour"`, `"day"` (and plurals, abbreviations). Default is `"ms"`. Tag-name suffixes `Tms`/`Ts`/`Tm`/`Th`/`Td` still accepted — `FillTimeTm` stays short, and `Tm` sidesteps the minute-vs-minimum ambiguity of `Min`.
- `DoneAccUDT` protocol — Timer/counter functions now type as `timer: DoneAccUDT` instead of `InstanceView | _StructRuntime`. IDE hover shows the contract, not the implementation.
- `normalize_unit()` exported — Converts any unit alias to canonical form. Available from `pyrung.core`.
- `TimeUnitStr` Literal type — All valid unit strings in one type for IDE autocomplete.

### Migration

- No breaking changes. Existing `preset=` keyword and `unit="Tms"` code works unchanged.

## v0.5.0 — Timer/Counter cleanup

v0.4.0 introduced `Timer` and `Counter` as built-in UDTs with `.named()` for creating instances. That was one special case too many — `.named()` is gone, replaced by `.clone()` which matches how the rest of the tag system works.

### Breaking changes

- `Timer.named()` / `Counter.named()` replaced by `.clone()` — `Timer` and `Counter` are now `count=1` singletons. Use `Timer.clone("Name")` / `Counter.clone("Name")` for named instances. TagMap auto-resolve for timer/counter operands removed — all mappings are now explicit via `.map_to()`.

### New features

- Section comments in TagMap codegen — `TagMap` constructor output now emits `# --- Structures ---`, `# --- Timers & Counters ---`, `# --- Blocks ---`, and `# --- Tags ---` section headers when there are 2+ non-empty groups.

### Migration

- Replace `Timer.named(n, "Name")` with `Timer.clone("Name")`. Same for `Counter`.
- Add explicit `.map_to()` calls for any timer/counter tags that relied on TagMap auto-resolve.

## v0.4.0 — Cleaner surface, honest abstractions

### Breaking changes

- `all_of`/`any_of` renamed to `And`/`Or` — PascalCase combinators replace the old function names. `&` and `|` operators removed for conditions (kept for math/bitwise). Comma inside `Rung(...)` stays as implicit AND.
- Built-in `Timer` and `Counter` UDTs — `Timer` and `Counter` are now built-in structured types with `.Done` (Bool) and `.Acc` (Int/Dint) fields, exported from `pyrung`. Use `Timer.clone("Name")` for named instances. User-defined UDTs with the same shape still work.
- Single-argument timer/counter instructions — `on_delay(timer, preset=...)` replaces `on_delay(done, acc, preset=...)`. Same for `off_delay`, `count_up`, `count_down`. The two-tag form is removed entirely.
- `PLCRunner` renamed to `PLC` — `.active()` removed; `PLC` is now a context manager directly (`with PLC(logic) as plc:`). `dt=` (default `0.010`) and `realtime=True` kwargs replace `set_time_mode()`. `dt=` and `realtime=True` are mutually exclusive. `TimeMode` removed from public exports.
- `set_battery_present()` replaced by property — use `plc.battery_present = False`.
- `plc.debug.*` namespace — 11 debugger-internal methods moved off `PLC` into `plc.debug`: `scan_steps`, `scan_steps_debug`, `rung_trace` (was `inspect`), `last_event` (was `inspect_event`), `prepare_scan`, `commit_scan`, etc. `system_runtime` accessible via `plc.debug.system_runtime`.
- Force API renamed — `add_force()` → `force()`, `remove_force()` → `unforce()`, `with plc.force(...)` → `with plc.forced(...)`. DAP debug console commands updated to match (`remove_force` alias removed).
- `_fn` variants dropped — `run_until_fn` merged into `run_until`, `when_fn` merged into `when`. Both now accept Tag/Condition expressions or callable predicates directly.
- `Program` internals privatized — `add_rung`, `start_subroutine`, `end_subroutine`, `evaluate`, `current` → private. Legacy `call_subroutine` removed.
- `TagMap` internals privatized — `offset_for`, `block_entry_by_name`, `owner_of` → private.
- Time units as strings — `Tms`, `Ts`, `Tm`, `Th`, `Td` removed from public imports. Use `unit="Tms"` string form. `TimeUnit` enum stays internal.
- Validation entry points consolidated — `validate_click_program` and `validate_circuitpy_program` removed from public exports. Use `logic.validate(dialect=...)` or `mapping.validate(logic)` / `P1AM.validate(logic)`.
- `Tag.__rand__` precedence guard — `int & tag` and `BoolTag & tag` now raise `TypeError` with guidance to reorder operands, preventing the Python operator precedence trap where `2 & tag` silently evaluates wrong.

### New features

- Conflicting output target validation — detects multiple `INERT_WHEN_DISABLED=False` instructions writing the same tag from non-mutually-exclusive paths, with condition-complement detection on caller conditions.
- Click timer preset overflow validation — `CLK_TIMER_PRESET_OVERFLOW` warns when a preset exceeds the INT range for its time base.
- `P1AM.validate()` convenience method — mirrors `TagMap.validate()` for CircuitPython programs.

### Bug fixes

- Click bypassed imported contacts — codegen now warns on contacts that were bypassed during import.

### Docs

- System namespace section added to concepts (`system.sys.*`, `system.fault.*`, `system.rtc.*`).
- Operator precedence trap callout added to conditions reference.
- Structured timer (`@udt`) note added to timers/counters reference.
- Click timer preset INT cap table added to Click dialect docs.
- Counter/timer accumulator switched to positional form in reference.
- Fault flags named explicitly in math reference.
- `ScanContext` section rewritten without internal type name.
- System points cross-referenced from runner guide.

### Migration

- Replace `all_of(...)` with `And(...)`, `any_of(...)` with `Or(...)`. Remove `&` / `|` between conditions — use `And()` / `Or()` or commas.
- Replace `on_delay(done, acc, preset=...)` with `on_delay(timer, preset=...)` using a `Timer` instance. Same for `off_delay`, `count_up`, `count_down` with `Counter` instances.
- Replace standalone `Bool`/`Int`/`Dint` timer and counter tags with `Timer.clone("Name")` / `Counter.clone("Name")`. Access `.Done` and `.Acc` fields on the instance.
- Replace `PLCRunner` with `PLC` everywhere. Replace `runner = PLCRunner(logic); ctx = runner.active()` with `with PLC(logic) as plc:`.
- Replace `runner.set_time_mode(TimeMode.REALTIME)` with `PLC(logic, realtime=True)`.
- Replace `plc.set_battery_present(False)` with `plc.battery_present = False`.
- Replace `plc.inspect(rung_id)` with `plc.debug.rung_trace(rung_id)`.
- Replace `plc.add_force(...)` with `plc.force(...)`, `plc.remove_force(...)` with `plc.unforce(...)`, `with plc.force(...)` with `with plc.forced(...)`.
- Replace `plc.run_until_fn(fn)` with `plc.run_until(fn)`, `plc.when_fn(fn)` with `plc.when(fn)`.
- Replace `on_delay(..., unit=Tms)` with `on_delay(..., unit="Tms")`. Same for `Ts`, `Tm`, `Th`, `Td`.
- Replace `validate_click_program(logic)` with `logic.validate(dialect="click")` or `mapping.validate(logic)`.
- Replace `validate_circuitpy_program(logic, hw)` with `logic.validate(dialect="circuitpy")` or `hw.validate(logic)`.

## v0.3.1

### Bug fixes

- Tag defaults now seeded into initial state — tags are populated with their declared defaults at construction time, fixing a disagreement between `Tag.value` and rung condition evaluation for tags with non-False defaults.

### Docs

- Expanded and polished "Know Python? Learn Ladder Logic" tutorial — added ASCII diagrams, adversarial exercises, cross-lesson callbacks, NC naming conventions, and aligned all lesson examples with the Click conveyor reference.
- `pyrung.zen` — `import pyrung.zen` prints guiding principles for ladder logic in Python (à la `import this`).

### Examples

- Conveyor examples (`click_conveyor.py`, `circuitpy_conveyor.py`) updated to follow tutorial naming conventions and best practices.

## v0.3.0

### Breaking changes

- `system.storage.sd.save_cmd` removed — use `board.save_memory_cmd` (`from pyrung.circuitpy import board`).
- `generate_circuitpy(...)` now supports optional `runstop=RunStopConfig(...)` and board-only (zero-slot) codegen.
- `calc(...)` no longer accepts `mode=` — mode is inferred from referenced tag families.
- `send()`/`receive()` now use `ModbusTcpTarget` dataclass instead of inline `host`/`port`/`device_id` kwargs.
- Codegen API cleanup — `TagMap.to_ladder()` removed; use `pyrung_to_ladder(program, tag_map)`. `csv_to_pyrung()` renamed to `ladder_to_pyrung()`.
- Copy modifiers replaced by copy converters — `copy(as_value(source), target)` is now `copy(source, target, convert=to_value)`; all `as_*` functions, `CopyModifier`, and `pad=` removed.
- Search uses comparison expressions — `search(DS.select(1, 100) >= 100, ...)` replaces the old `condition=`/`value=`/`search_range=` form.
- Block slot API replaced — `rename_slot()` etc. removed; use `block.slot(addr)` which returns a `SlotView` with `.name`, `.retentive`, `.default`, `.comment` properties.

### Moved

- `send_receive` module moved from `pyrung.click` to `pyrung.core.instruction`; re-exported from `pyrung.click` unchanged.

### New features

- History time-travel slider — scrub across retained scan snapshots in the VS Code debug sidebar with live tag value updates.
- Raw Modbus TCP and RTU support — `send()`/`receive()` accept `ModbusAddress` for raw register access to any Modbus device, with new `ModbusRtuTarget`, `RegisterType`, and `WordOrder` types.
- `BlockRange.sum()` — `DS.select(1, 10).sum()` returns a `SumExpr` for use in `calc()`.
- Click ladder CSV export — `pyrung_to_ladder(program, tag_map)` generates deterministic CSV files importable into Click programming software.
- Click ladder semantic-loss guard — round-trip now fails loudly on mismatched objects or lossy conditions instead of silently dropping them.
- In-memory round-trip — `ladder_to_pyrung(bundle)` accepts a `LadderBundle` directly for program → ladder → Python source without disk I/O.
- Multi-file project codegen — `ladder_to_pyrung_project(source)` generates `tags.py`, `main.py`, and `subroutines/*.py` from Click ladder CSV, with nickname-based name substitution.
- `immediate()` wrapper — immediate I/O reads in contacts and coil targets, with Click validation for direct-only contacts and `Y` bank coils.
- `TagMap.tags_from_plc_data()` — converts a PLC data dump into logical tag values for initializing a runner.
- Click nickname CSV round-trip improvements — marker-only boundary rows and block-slot address comments now round-trip correctly.
- Empty rung preservation — empty and comment-only rungs survive Click ladder CSV round-trip via `NOP` emission.
- Bare text safeguard in codegen — raises `ValueError` on unrecognised bare AF tokens instead of emitting undefined names.
- Rung comments — `comment("...")` attaches comments to rungs, exported as `#,<text>` rows in Click CSV.
- Nested branches — `branch()` inside `branch()` is now supported, all depths evaluate against the rung-entry snapshot.
- `Rung.continued()` — reuses the prior rung's condition snapshot for multiple independent wires on the same visual rung.
- CircuitPython Modbus TCP codegen — `generate_circuitpy()` accepts `modbus_server=` and/or `modbus_client=` for P1AM-200 Modbus TCP via P1AM-ETH.
- `write_circuitpy()` — convenience function that generates and writes CircuitPython output files in one step.
- Split CircuitPython codegen output — produces `code.py` (program) and `pyrung_rt.py` (shared runtime); pre-compiled `.mpy` available from GitHub releases.
- Crash-safe retentive persistence — CircuitPython retentive tags auto-save to SD card every 30 seconds with atomic rename.
- `named_array` instance API — `instance(i)` and `instance_select(start, end)` for accessing single or ranged instances.
- New examples — `circuitpy_traffic_light_modbus.py` (intersection controller with Modbus) and `circuitpy_retentive_runstop.py` (retentive tags with RUN/STOP).
- Starter project release assets — ready-to-import Click ladder CSV and CircuitPython bundles with pre-compiled `.mpy`.
- "Know Python? Learn Ladder Logic" tutorial — multi-lesson series with a conveyor sorting station example.

### Bug fixes

- OR topology corrections — four fixes for ladder CSV export that could silently corrupt OR branch wiring.
- T junction propagation — bridge topology now propagates correctly through T junctions in codegen.
- Named array stride — corrected stride handling for `count=1` named arrays in the Click collector.
- Analyzer graph reduction — fixed edge cases in the Click rung analyzer.

### Performance

- Faster round-trip program construction in codegen.

### Internal

- Expression class hierarchy replaced with data-driven `BinaryExpr`/`UnaryExpr`/`ExprCompare`.
- Module splits: `tag_map`, `context`, `send_receive`, and `codegen` each split into packages.

### Migration

- Replace `out(system.storage.sd.save_cmd)` with `out(board.save_memory_cmd)`.
- Replace `calc(..., mode="hex"|"decimal")` — mode is now inferred. Split mixed WORD/non-WORD math into separate `calc()` steps.
- Replace `send(host=..., port=..., device_id=...)` with `send(target=ModbusTcpTarget("name", "host"))`. Same for `receive()`.
- Replace `copy(as_value(source), target)` with `copy(source, target, convert=to_value)`. Same for `as_ascii`→`to_ascii`, `as_text`→`to_text`, `as_binary`→`to_binary`. Remove `pad=` — use string literals instead.
- Replace `search(condition=..., value=..., search_range=...)` with `search(range >= value, ...)`.
- Replace `block.configure_slot(addr, ...)` with `block.slot(addr).name = ...` etc.

## v0.1.0

Initial public release.

### Core engine

- Pure-function scan cycle with immutable `SystemState` snapshots (via `pyrsistent`)
- DSL: `with Rung()` context managers for readable ladder logic
- Instructions: `out`, `latch`/`reset`, `copy`, `calc`, `on_delay`/`off_delay`, `count_up`/`count_down`, `shift`, `search`, `fill`, `blockcopy`, `event_drum`/`time_drum`, `pack_bits`/`unpack_to_bits`, `pack_words`/`unpack_to_words`, `pack_text`
- Tag types: `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`
- Structured tags: `@udt()` for mixed-type structs, `@named_array()` for single-type interleaved arrays
- Blocks: `Block`, `InputBlock`, `OutputBlock` with configurable indexing
- Control flow: `branch`, `subroutine`/`call`, `forloop`, `return_early`
- Conditions: `rise()`, `fall()`, `all_of()`, `any_of()`, comparison operators
- Time modes: `FIXED_STEP` (deterministic, default) and `REALTIME` (wall-clock)
- Runner: `step()`, `run()`, `run_for()`, `run_until()`, `scan_steps()`
- Forces: `add_force()`, `remove_force()`, `with runner.force()` scoped context manager
- Patch: one-shot inputs via `patch()` or `.value` writes
- History: `runner.history.at()`, `.range()`, `.latest()`, configurable `history_limit`
- Time travel: `runner.seek()`, `runner.rewind()`, `runner.playhead`
- Fork: `runner.fork()` / `runner.fork_from()` for independent runners from snapshots
- Breakpoints: `runner.when(condition).pause()` / `.snapshot()`
- Monitors: `runner.monitor(tag, callback)` on committed value changes
- Inspection: `runner.inspect(rung_id)` for `RungTrace`, `runner.diff(scan_a, scan_b)`

### Click PLC dialect

- Pre-built memory blocks: `x`, `y`, `c`, `ds`, `dd`, `dh`, `df`, `t`, `td`, `ct`, `ctd`, `sc`, `sd`, `txt`, `xd`, `yd`
- `TagMap` for mapping semantic tags to Click hardware addresses
- Nickname CSV import/export for Click programming software
- Validation against Click memory bank constraints
- `ClickDataProvider` for running programs as a soft PLC over Modbus
- Type aliases: `Bit`, `Int2`, `Float`, `Hex`, `Txt`

### CircuitPython dialect

- P1AM-200 hardware model with module catalog
- Slot configuration and I/O validation
- CircuitPython scan loop code generation from pyrung programs

### VS Code debugger (DAP)

- Step through scans, set breakpoints on rungs
- Force tags from the debug console
- Diff states between scans
- Time-travel through scan history
- Logpoints and trace decorations
