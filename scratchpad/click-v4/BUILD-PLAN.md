# CLICK v4 — build plan

Work happens on `dev`. Build agents use their own worktrees and merge back into dev one at a time
(rebase onto dev, fast-forward). Landed history lives in `git log`; this file holds what is decided and open.

## Source documents

| Doc | What it holds |
|---|---|
| `RESEARCH.md` | Decided design + syntax, CLICK semantics, probe IDs (O/D/M/F), capture list, conformance plan |
| `TOUCHPOINTS.md` | First survey: per feature, categories A-L, file:line; addendum on crossings |
| `TOUCHPOINTS-GAPS.md` | Mechanical gap audit: missed sites + 24 corrections to TOUCHPOINTS.md (corrections win) |
| `PILOT-GOTCHAS.md` | PILOT audit: 12 cross-cutting items by severity, per-feature notes, suggested PILOT gates |
| `BINARIES-PC.md` / `BINARIES-FIRMWARE.md` | Compile.dll func-code table; firmware encrypted (no semantics) |
| `tools/` | Perf meters, brute-force prover harness, heavy-job lock (see "Measuring" below) |

When two docs disagree, the later audit wins, unless this file settles it.

Settled here: **crossings coverage test.** Putting a new class in `EXPECTED_WITH_CROSSINGS` forces an explicit
`register()`, because `registered_classes()` must match that set exactly. Putting an `OutInstruction` subclass in
`EXEMPT` passes the test while `crossing_for()` still hands it `OutCrossing` at runtime. The test only sees
classes from modules that have been imported.

## Fold and prover contract (decided 2026-10-23)

- **Fold has no caveats.** It is on by default, so it equals stepping (whole state, scan count) or declines.
  Holes are found by `make test-fold`, not by more review rounds. One documented gap (runner guide,
  "Folding"; strict xfails `TestIndexedDisplays` in `tests/core/test_fold.py`): a display copied from an
  accumulator into a block element nothing reads by name goes stale inside a window when a rung reads it back
  through an unbounded index (`DS[Idx]`, no min/max/choices).
- **The prover may carry caveats.** A construct outside the exact fragment returns Intractable naming it.
  Common idioms stay exact: coarse-unit timers, self-resetting TON pulses, the head-start race (counter vs
  timer, timer vs timer). Weigh how common an idiom is before choosing Intractable over exact.

## Rules for every feature brief

- New instruction classes subclass neither `OutInstruction` nor `TimeDrumInstruction`. New expression classes
  are siblings, not subclasses of `SumExpr`/`MathFuncExpr`/`LiteralExpr`; that applies to `RandIntExpr`.
- Declare every written tag as its own `Tag` field in the field-declaration layer: `write_sites.py:73-88`
  (`_writes`, `_status_fields`) and `pdg.py:946-969` (`_reads`, `_conditions`, `_exclusive_fields`). For a
  Timer, store Done and Acc as separate fields, as `on_delay` does. If Done/Acc aren't declared, they show up
  as unwritten inputs and PILOT may set them directly. Fold, the PDG and `prove/deadness.py` all trust these
  declarations: an undeclared read is a soundness bug (a time drum's accumulator was one).
- Write all internal memory keys every scan (the prover discovers keys from one scan).
- Crossing per class, registered explicitly. `forward()` returns `UNKNOWN` unless the value is exact
  (`sp_values.writer_value_facts` and `multitarget._writes_off` trust it).
- Anything timed registers as a fold source (`core/fold.py` `_collect_acc_sources`) and as a cyclefold `_frac`
  source (`pilot/cyclefold.py`), and gets a fold-vs-unfold equality test.
- Hidden-memory instructions go in the elision bail list (`prove/elision/slice.py`).
- A new finding code needs its row in `core/validation/registry.py`; without it the report hits a KeyError.
- `make lint`, `make test` (both runner backends) and `make test-fold` must pass. Draft conformance cases go
  in with pyrung's current answer marked unconfirmed.
- Each feature checks that `how()` on a program using it still has its prover context (see Known risk).
- Docs page, CHANGELOG Unreleased entry, cheatsheet. The two cheatsheets are one file: the docs copy includes the bundled one.

## Features, in order

| # | Feature | Size | Read | Status / notes |
|---|---|---|---|---|
| 1 | Conformance rename + Modbus runner | S + M | RESEARCH 6, TOUCHPOINTS 6, GAPS 6 | **Done.** One program, a `Conf_<Group>` subroutine per group, manifest, `confirmed=`; `math` group added. CLICK confirmed: pasting one subroutine works, nested calls unsupported, rail-wired outputs valid |
| 2 | Math aggregates + abs | S-M | RESEARCH 3, TOUCHPOINTS 1, GAPS 1, PILOT 8 | **Done** |
| 3 | `time_event_drum` + drum hint | M | RESEARCH 2, TOUCHPOINTS 3, GAPS 3 | **Done** (reviewed). Own class; level events; Acc keeps counting while a timed step waits (D1 = pyrung's answer, unconfirmed); fold source (event-only steps place no crossing); preset rule is a v4 hint in every mode; `drum` conformance group (D1, D3). Prover: named Intractable, see Open queue |
| 4 | Out family | L | RESEARCH 1, TOUCHPOINTS 4, GAPS 4, PILOT 2-5 | **Done** (reviewed). Debounce needs both sides; every phase shows for at least one scan; TD-only CSV token; `CLK_TIMER_PAIR_MISMATCH`; Timer/Counter retentive now CLICK's bank defaults and the importer conforms clones to their addresses (no invented nicknames). `out_variants.py`, own classes; state in tags (coil bit + each phase Timer's Done/Acc) except toggle's prev-rung key. Answers to O1-O6 are pyrung's, unconfirmed: debounce sides are TONs on the rung / its negation (TD counts past preset, a bounce restarts it), flasher Dones mark the running phase, timed is IEC TP (needs a fresh edge). Fold sources per phase; prover exact for toggle, Intractable naming the verb otherwise; PILOT profiles for toggle/debounce/timed (flasher has none: its writer trace reaches both values); CSV token is pyrung's; `out` conformance group (O1-O6) |
| 5 | For/Next | L | RESEARCH 4, TOUCHPOINTS 5, GAPS 5, PILOT 6 | **Next.** Today: body instructions get no enable condition (`count_up` in a loop declines in PILOT); `with Rung()` in a loop silently hoists; journals need a body-rung + iteration address; return-shadow treatment for break/continue; `comment()` before a body rung. Index-heavy: do the PDG affine fix first (Open queue) |
| 6 | `randint` | M-L | RESEARCH 3, TOUCHPOINTS 2, GAPS 2, PILOT 7 | Seed lives in `SystemState.memory`, not on the runner (forks); stable site ids for `PLC(logic=[...])`/synthesis/pilot rungs; override via `io_drains_by_scan` with interpreted replay; scan_id resets on restart |

One at a time, each merged before the next starts.

## Measuring

- **Heavy-job lock:** one heavy suite at a time machine-wide (make test, test-fold, test-pilot, test-prove,
  tumbler, PackML lock, fuzz). `tools/heavy.sh <owner> <worktree> <timeout_s> <cmd...>` takes the mkdir lock
  `C:/Users/Sam/Documents/GitHub/pyrung-wt/.heavy-lock` (15-min stale rule, exits 99 after 600 s waiting).
  Timeouts: make test 600, make test-fold 1200, test-prove 120, test-pilot 270, tumbler 900, PackML lock 420.
- **Fold/PILOT meters** (`tools/burner_meter.py` + `tools/fold_meter.py`): run from a worktree with
  `PYTHONPATH="<wt>/src;<wt>;<repo>/scratchpad/click-v4/tools" uv run --no-sync python tools/burner_meter.py
  <workload> out.json`, workloads `how_burner`, `run_for_idle`, `run_until_cmds`, `seek_replay`. The tumbler
  meter: `FOLD_METER_OUT=out.json uv run --no-sync python -m pytest -p fold_meter -q tests/tumbler/`.
  Baselines on dev 9a075b6 (kernel scans): how_burner 2675 (3 cycle folds), run_for_idle 12008,
  run_until_cmds 7399, seek_replay 78, tumbler 19893 (15 cycle folds, ~180 s).
- **Prover:** `uv run pyrung lock examples.packml_bench -o x.lock`: ~91 s on dev (accepted), 484 states; compare
  byte-for-byte against a lock from the base commit. `tools/adv.py` `check(name, build, depth)` compares a
  verdict with brute force (HOLE = wrong Proven).
- make test ~6986 passed / 8 xfailed, ~6.5 min.

## Open queue

Not blocking features unless noted. Repro paths under `tools/` are in the repo; others were in session
scratchpads and may be gone, so each item states its program.

Fold:
- **Unbounded-index display gap** (documented above). Fix needs pointer bounds from their writers (the
  value-set analysis in `prove/deadness.py` is a starting point) or the probe scan recording what each index
  resolved to (the compiled kernel inlines indexed reads, `circuitpy/codegen/compile/_primitives.py`).
  Marking every element of an indexed block read is sound but cost the tumbler most of its folds (how
  2275 -> 3355; AlmHistorian indexes DS with unbounded pointers).
- **Cycle-fold jumps don't register fold windows** (`pilot/cyclefold.py`), so `cause()` inside a PILOT coast
  jump isn't refused. PILOT calls `cause()` heavily: check its callers before making it raise.

Prover (wrong Proven unless noted):
- Timer-race stop points (three-timer fix) leave gaps, none seen in a wrong Proven yet: an idle absorbed
  integer/real counter gets no stop points (rate unknown); a start more than `_STOP_START_DELAY` (2) scans
  behind its input; a retentive timer frozen at a key at different values is still one first node plus one
  seed (`cycles.py` "What is left"). Sweep: `tools/race_sweep.py` against brute force.
- MEDIUM: CTUD, `count_up(c, 4).down(rise(Y))` (classification: don't absorb, or model as bidirectional).
- Absorption gate blind spot (done): `_has_forbidden_data_read(program, graph, ...)` also counts what an
  unbounded index reads, from `ProgramGraph.indirect_read_reach` (`pdg.unbounded_read_names`); a counter
  in `DS[7]` read by `copy(DS[Ptr], N)` was absorbed and lost the 3 the read needed (wrong Proven, any
  block size). `_visit_indirect_refs` stops at `Tag`/`Block` (it walked whole blocks through `vars()`:
  3.3M calls, +3 s on PackML's context build).
- Low: `_pilot_sweep_domains` and the lints step without the system model (`bounds.py`, `lintsys.py`: false
  CMP_ALWAYS_FALSE on `calc(rtc...)` destinations).
- Perf, accepted for now: PackML lock 91 s vs 61 s before the timer fixes; the gap is pulse laps
  (`a11909b`, ~+26 s) and tag presets (`e0fe4fe`, ~+13 s), unprofiled.

PDG (shared with prover, PILOT, causal, lints):
- Unbounded index reads (done): the prover builds `build_program_graph(unbounded_index_reads=True)`
  (`_pass_build_graph`, BFS only; the how() prior keeps PILOT's graph). `pdg._UnboundedReads`: an index
  with no declared bounds reads the block's changing elements (written, `external`, `InputBlock`),
  narrowed by `analysis/value_sets.py` (moved out of `prove/deadness.py`; new opt-ins
  `follow_indirect_reads` for jump tables, `declared_inputs` for inputs with choices/min/max). PackML
  needed both opt-ins plus readonly sources counting as init-constant literals (`init_constants.py`) to
  stay at dev's dimensions and time.
- Over-cap indexed writes (done): in the `unbounded_index_reads` graph a write through an unbounded index
  into a block past `_INDIRECT_BLOCK_CAP` writes the elements its value set reaches
  (`_UnboundedReads.reached`, by id of the destination ref), recorded on `ProgramGraph.indirect_reach`;
  classify's writer and domain sites take that map (`_all_write_targets(instr, reach)`,
  `_expand_indirect_tag_names(dest, reach)`). A write whose addresses are unbounded too stays
  unregistered; its pointer has no domain, so the result is Intractable. Value sets drop the default of a
  tag `unconditional_write_before_read` covers (`written_first`; an indexed read of its slot still takes
  it), else the historian reaches DS5/6/9 at `HistIdIdx = 0` and `UnitModeCurrent` widens to 18 values.
  The historian slots DS3000..3040 then became 5 stateful dimensions (PackML 106 -> 245 s); slice
  elision's `_local_slice_check` (one call of the subroutine, its own closure, `HistIdIdx` /
  `HistStatusIdx` written first, 10 combinations) elides them: lock byte-identical, 100 s. Follow-ups
  (done): the absorb accumulator sites, `_has_inert_writer` and the literal-domain target list take the
  reach too (an over-cap write could jump a counter in `DS[7]` that was then absorbed); `blockcopy`/`fill`
  into an indexed window (`DS.select(Ptr, Ptr + 1)`) is a writer of what `_ValueSets.window` covers
  (lowest start to highest end; the whole block when unbounded, as before), and `_domain_from_blockcopy`
  gives those elements the union of the source elements' domains (was: every element of the block
  Intractable, at any size). Left: `HistorianStatusEcho`'s domain reads 0..17 where it holds 0/1
  (seeding only); PackML context build +0.3 s.
- Index mapping (done, affine fix): `pdg.PointerUse` per access (`DS[Ptr + k]`, `DS[k - Ptr]`, integer k),
  `ProgramGraph.pointer_uses`, `opaque_index_blocks` (non-affine index: reads/writes the whole block up to
  `_INDIRECT_BLOCK_CAP`), `index_tags` (every tag an index reads, counted as comparison-influencing so an
  unwritten pointer is an input, not default-pinned constant storage). Nested affine (`Ptr + 1 + 2`) is
  non-affine for now. Block slots read through an index stay constant storage.

time_event_drum (from feature 3):
- **Prover has no exact model.** Its Acc is unmodeled whatever its declared domain (`classify.py`,
  `unmodeled_accs`; the `_frac:` carry is in no state key, so a declared domain gave a wrong Proven before
  the review fix), so any program using the drum is Intractable "unbounded domain on Acc" with a hint
  naming `time_event_drum`, even for unrelated properties (as with any unbounded counter). how() keeps
  its context. Exact route: a saturating key for Acc (only `Acc >= preset[step]`
  matters when nothing else reads it), or a done/acc kind whose crossing is gated by the event.
- laddercodec: no T&E encoding yet (`drum_kind` T&E, func codes 10505..10538, register-duration codes).
  pyrung exports and imports the `time_event_drum(...)` CSV token, `None` for a blank preset or event.
  Until then the `drum` conformance group (D1, D3) cannot reach a real CLICK.
- D2 (which banks may hold a duration register) has no Click check.
- Fold perf, also for `time_drum`: a coarse-unit (`sec`) drum steps most scans inside a step
  (~1600 kernel scans of 2000 in `TestTimeEventDrum._event_only_tail`); not measured further.
- Fuzz grammar has no T&E kind (`_TERMINAL_KINDS` etc. would need it).
- PILOT declines (never unsound) on an Acc target past the preset: on a time-only last step
  (`time_drum` plans `_auto` there) and on a timed+event step (holding the event off would work).
- **The ladder export precheck still blocks register drum durations.** RESEARCH says
  `CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED` was downgraded to a hint, but `pyrung_to_ladder()` raises
  `LadderExportError` on `time_drum(presets=[DS1302, ...])` (`click/ladder/validator.py` `_run_precheck`).
  The capture generator (`captures/v4_capture.py`) works around it with `validate=False`.
- `CMP_PRESET_STAYS_ZERO` (`cmp_conditions._preset_sites`) does not cover drum duration registers, and
  Click validation does not role-check register presets on either time drum.

Out family (from feature 4):
- **laddercodec**: no Out-type encoding (func codes 8224-8235). Until then the `out` conformance group cannot
  reach a real CLICK. First capture (debounce, 2026-10-23): class `Out`, type 0x2715 like a plain coil, 15
  fields, a 3-row cell. `0x6066` target, `0x60B3`/`0x60B4` on preset/TD, `0x60B5`/`0x60B6` off preset/TD,
  `0x3218` func code (8226 as predicted). Unknown: `0x1246`=0, `0x1247`=0, `0x21F9`=0, `0x2248`=1,
  `0x20CA`=2 (output type?). No T field. laddercodec decoded it as a plain `out(C1)` until its coil parser
  learned to reject unknown func codes (unreleased).
- Out CSV token is TD-only (done): `debounce(Y001,on_acc=TD1,on_preset=100,off_acc=TD2,off_preset=DS5,unit=Tms)`;
  the importer derives T<n> from TD<n>; `CLK_TIMER_PAIR_MISMATCH` covers a Done not on the T of its TD's
  number (all timers and counters). `debounce()` requires both sides (done).
- Semantics not probed yet: a phase no longer than a scan shows for one scan (pyrung's choice); the flasher's
  first on phase counts the rung's first scan, so it is one scan shorter than later ones.
- O6: if CLICK does not write T<n>, add `CLK_OUT_TIMER_DONE_UNSET` (a read of an Out-owned `Timer.Done`).
- PILOT declines `how()` on a counter fed by a debounced bit's edges (strict xfail
  `test_pilot_counts_debounced_edges`): the edge driver flips the input every scan, so the debounce never fires. The
  oscillation would have to hold each level until the coil follows.
- Prover: no exact model for debounce/flasher/timed (their Acc is unmodeled, as the time & event drum's).
- Crossings fall through for all four; `forward()` must stay `UNKNOWN` (sp_values, multitarget proof).
- Lint gaps (declines, not wrong): `stranded_bits` doesn't count a toggle target as persistent, `physical_realism`
  ignores the variants, `CMP_PRESET_STAYS_ZERO` doesn't cover their presets. Fuzz grammar has no Out kinds.

Found by the feature-4 review, pre-existing on HEAD:
- A callable property that reads a tag the program never touches (`lambda s: s["M0"]`) raises KeyError from
  `guarded_state`. Loud, not wrong; the condition form of the same property is handled (property-only tags
  join the graph as inputs).
- Fold: two `on_delay`s on one Timer (one rung false and resetting it, one ticking) plus any other moving timer
  folds Acc ahead (350 vs 70). A duplicate-out program, but it breaks "equal stepping or decline".
- A Real preset tag: interpreted compares `acc >= 2.5`, compiled `acc >= int(2.5)` (on_delay, timed, debounce).
- The importer drops the comment on an un-nicknamed TD row.

Other:
- **Function spelling in math export.** CLICK spells a call `NAME ( a , b )`, with spaces around each paren and
  separator (v4 capture: `ABS ( DS1411 )`, `RAND ( DD111 , DD112 )`, `SUM ( DS301 : DS350 )`). laddercodec
  now normalizes to that form on encode (unreleased; `ABS( x )` was a Math instruction error in CLICK v4,
  because CLICK splits the internal form on spaces). pyrung's translator still renders `ABS(DS1)`
  (`tests/click/test_ladder_export.py` `test_calc_abs_renders_uppercase`), so its CSV doesn't match what a
  copy-back decodes to. Render the spaced form, and check that the importer takes it.
- `cause("T")` on a stepped `calc(T + 3, T)` raises RecursionError (not fold).
- Click exporter doesn't enforce CLICK's 24-character nickname limit (`pyclickplc.validation.NICKNAME_MAX_LENGTH`);
  add a validation finding.
- Conformance subroutine-call cases ("call into subroutine", "subroutines: same rules") need a home called from
  main, since groups are already subroutines.

## Known risk, not scheduled

- PILOT's prover context is all-or-nothing (`drive_setup.py:300-385`). Any compile or prover failure silently strips
  value domains, the world key and transition evidence from every `how()`. A new instruction that the prover can't
  absorb yet degrades all `how()` calls on that program.

## Waiting on the user

- CLICK v4 capture session (RESEARCH 5, plus copying the `Compile\` folder), for laddercodec blobs (the T&E drum and
  the Out types wait on it).
- Real-PLC conformance runs (O/D/M/F).
