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
| 4 | Out family | L | RESEARCH 1, TOUCHPOINTS 4, GAPS 4, PILOT 2-5 | **Next.** PILOT: non-OUT writers treated as retentive (`ote_writes`); in-instruction edges invisible (`program_facts.py:173`); advance profile = "hold until output equals v". Needs the CLICK v4 capture |
| 5 | For/Next | L | RESEARCH 4, TOUCHPOINTS 5, GAPS 5, PILOT 6 | Today: body instructions get no enable condition (`count_up` in a loop declines in PILOT); `with Rung()` in a loop silently hoists; journals need a body-rung + iteration address; return-shadow treatment for break/continue; `comment()` before a body rung. Index-heavy: do the PDG affine fix first (Open queue) |
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
- HIGH, also on dev: three timers where B must be pressed partway through T1. `Rung(A): latch(L1)`,
  `Rung(B): latch(L2)`, `Rung(L1): on_delay(T1,100)`, `Rung(L1): on_delay(T3,120)`, `Rung(L2): on_delay(T2,50)`,
  `Rung(T1.Done,~T2.Done): latch(X)`, `Rung(T2.Done,~T3.Done): latch(Y)`, `Rung(X,Y): out(H)` is Proven;
  brute force reaches H in 11 scans (presets 60/30/80 and 80/40/100 too). Suspect `cycles.covered()`: a
  revisit ahead on one time-like timer and equal on another is treated as reachable by stuttering, but
  stuttering advances both. `tools/prove_three_timers_r4.py`.
- MEDIUM: CTUD, `count_up(c, 4).down(rise(Y))` (classification: don't absorb, or model as bidirectional).
- Absorption gate blind spot: `_has_forbidden_data_read` misses reads through an unbounded index
  (`_indirect_ref_tags` returns None), so an accumulator mapped into a bank (`LoopIndex = ds[7]`) could be read
  by `copy(ds[ptr], ...)` unseen. PackML's pointer never reaches it; the gate doesn't check.
- Low: `_pilot_sweep_domains` and the lints step without the system model (`bounds.py`, `lintsys.py`: false
  CMP_ALWAYS_FALSE on `calc(rtc...)` destinations).
- Perf, accepted for now: PackML lock 91 s vs 61 s before the timer fixes; the gap is pulse laps
  (`a11909b`, ~+26 s) and tag presets (`e0fe4fe`, ~+13 s), unprofiled.

PDG (shared with prover, PILOT, causal, lints):
- Affine index narrowing, easy: `DS[Ptr + k]` is narrowed by Ptr's range ignoring k (`pdg.py`
  `_indirect_expr_base_tag`). Map the pointer's bounded addresses through `analysis/affine.py`
  `extract_affine_expression`; non-affine stays unresolved. Prover-visible: needs its own PackML lock check.
- `DS[agg]` with no base tag and `DS[Ptr]` with unbounded Ptr register no reads. A naive full-block fallback blew
  PackML from 58 s to over 300 s (446 DH tags became ND inputs); a fix needs offset-aware narrowing plus "only
  elements with a writer" for unbounded reads.

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
- `CMP_PRESET_STAYS_ZERO` (`cmp_conditions._preset_sites`) does not cover drum duration registers, and
  Click validation does not role-check register presets on either time drum.

Other:
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

- CLICK v4 capture session (RESEARCH 5, plus copying the `Compile\` folder), for laddercodec blobs. Needed from
  feature 4.
- Real-PLC conformance runs (O/D/M/F).
