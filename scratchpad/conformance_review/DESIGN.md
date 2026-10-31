# Design: site memory, counters on edges, false-For skip, live row solve (spike 2026-10-29)

Read-only design spike against dev 050f1eb. Companion to FINDINGS.md. Line numbers are from that commit.

## Verdict on the site-memory hypothesis

Mostly right, but half of it already exists. `Instruction.memory_key(prefix)` gives
`prefix:i{N}`, numbered in walk order by `Program._assign_state_keys`
(program/context/_program.py:72); codegen reuses it via `CodegenContext.state_key_for`
(circuitpy/codegen/context.py:634). Coil/For one-shots, drum jump/jog/event memory
(drums.py:265-392), shift clock (advanced.py:326) and `_toggle` already use it.

- Counters are a missing consumer (`_cu:i{N}`, `_cd:i{N}`), not the primitive's origin.
- Edge contacts are the new part: `Condition` objects have no site identity and can be
  shared (`r = rise(B)` used twice), so object id is not a key.

Identity is stable for a fixed program but breaks in five places (some are bugs today):
1. Hot reload: `dap/reload_console.py:105` carries `memory` whole; ordinal keys shift on
   rung insert (already wrong for one-shots/drums).
2. Overlays: synthesis program built with `Program.__new__` (runner.py:2594) never assigns
   keys; codegen fallback hands out `i1, i2...` colliding with user keys; interpreter falls
   back to `id()`.
3. Prover key discovery: `_pass_discover_memory_keys` (prove/passes.py:1994) learns keys
   from one trial scan; sites not run on scan 0 (uncalled subroutines, skipped For bodies,
   unpowered contacts) are missed -> distinct states merged. Probably an existing soundness
   gap for one-shots in subroutines; confirm.
4. None vs False: kernel memory starts empty (kernel.py:67); unwritten = None, written =
   False -> identical states split in the prover key.
5. First-scan inconsistency: interpreter edge memory defaults False (condition.py:286),
   compiled replay seeds from the tag (compiled_plc.py:677).

One site per place in the program is CLICK's model, so `rise(X)` stops firing on every
subroutine call / For iteration within a scan (behaviour change).

## Inventory

**Previous-scan / edge state**
- Core: condition.py Rising/FallingEdgeCondition (+ `_DERIVED_EDGE_TAGS`);
  condition_trace.py:155,169; runner.py `_capture_previous_states` (4253), commit step 7
  (4493), `_refresh_known_tags_and_edges` (3611); compiled_plc.py `_initialize_from_state`,
  `_capture_previous_states`; kernel.py `ReplayKernel.prev/capture_prev/reset`,
  `CompiledKernel.edge_tags/_prev_template`; system_points `_CLOCK_HALF_PERIODS`.
- Instruction sites: coils.py:79-90, utils.py `guard_oneshot_execution`, control.py:210-223,
  drums.py, advanced.py, out_variants.py:88. `OneShotMixin._has_executed` is on the Python
  object, not state (harmless today).
- CircuitPython: compile/_core.py:438-459; render.py:581,895,909,1334;
  render_kernel.py:151,232,362,407; context.py `edge_prev_tags`; _instructions_block.py:391-395,564.
- Prover: kernel.py (edge dims in state key, `_EdgeCompressor`, `_collect_edge_tag_exprs`,
  `_EDGE_DEAD`); passes.py:1442 (edge demotion), 1741, 1994; bfs.py:1331-1417 (prev merge
  across factored groups); events.py:1458-1498 and `_fixup_unfired_counters` (856);
  elision/slice.py:583,721; expr.py `_has_edge_atom`; inputs.py (single-flip enumeration).
- Fold: fold.py:557-612 (dormant edge gates), 1709-1729, counter sources 203-266,
  `_scans_to_clock_edge` (3802).
- Causal/pilot/other: causal/projected.py:1102,1187 (injects `_prev:`);
  crossings/accumulating.py; pilot program_facts `compute_edge_tags`, route_options,
  earned_work; conformance/_runner.py:180.

**Atomic-snapshot assumptions**
- Runtime: rung.py (`_resolve_condition_view`, `_compute_branch_enable_map`,
  `_execute_in_loop`); context.py `ConditionView`; instruction/utils.py:72
  `instruction_condition_view`; executor.py natural path (1109, 1180, 1209, 1261) and
  observed path (1317, 1333, 1397, 1508, 1565), `ConditionViewCapture.begin_condition` (879);
  debugger.py:103, `_build_branch_maps` (541); debug_handlers.py Call/For;
  intrascan_counterfactual.py:212.
- CircuitPython: _core.py `_collect_helper_conditions`, `_collect_rung_snapshot_bindings`,
  `compile_rungs` continued chains (782), branch vars precomputed in `_compile_rung_items`.
- Analysis: pdg.py:1366 `emit_condition_prepass`; prove classify `_uses_prior_snapshot`,
  `_has_continued_reader`, `_has_instruction_snapshot_reader`, `_has_same_rung_branch_reader`;
  slice.py:210 (ConditionView subclass detecting reads); fold.py:945,1301,1404,1454;
  validation/pointer_sites.py:121; program/context/_rung.py:110.
- Export/import: _exporter.py `factor_outputs` (factors shared prefixes -> can invent shared
  nodes; same unsoundness as the importer finding); importer `_split_continued`.

## Site-memory design

- New `core/site_memory.py`: `bind_sites(roots, subroutines, namespace)` -> `SiteTable`.
  Instructions keep `i{N}`; edge contacts get `e{N}` in solve order (rung contacts, items,
  branch-local contacts, helper legs like `i7.reset.0`, For-body rows). An edge condition
  object met twice is deep-copied. Overlays use `syn:`/`pilot:` namespaces. Missing key
  raises; no `id()` fallback. Entries record kind, tag, scope, row, and a fingerprint
  (scope, source line, kind, operands) for reload.
- Storage: `SystemState.memory` as `_edge:e{N}`, `_cu:i{N}`, `_cd:i{N}`. No new state field.
- Switches in this module: `EDGE_MEMORY_UPDATE = "scanned" | "powered"`,
  `SITE_MEMORY_POWER_UP`, `FOR_ZERO_ITERATIONS`.
- Interpreter: per-row "edge frame" reads each edge site's tag live before the row's
  boolean; `evaluate()` stays pure. Scanned mode writes every site; powered mode writes only
  where the contact's left node is energized (non-short-circuit pass, only rows with edge
  sites). Explain/trace read the frame, never write.
- Codegen (P1AM + prover kernel): `_eK = bool(X)` before the row, `_eK and not _mem.get(...)`
  in it, then write `_mem[...]` unconditionally or behind the power expression. Remove
  `_prev`/`edge_tags`/`prev` arg. Keep derived-clock shortcut only for always-scanned sites.
- Prover: `memory_key_names` from SiteTable; state key reads `get(k, False)`; edge-prev dims
  become site dims; edge liveness per site from its row expression; edge demotion restated
  as "B is not a dimension, only its site memories are"; factored merge and events
  combinations include memory deltas; counter input memory is dead for single-scan pulse
  inputs (rise()-wrapped counters cost nothing extra).
- Fold: guard that site memory is identical before/after the probe scan, else decline.
- Causal/pilot: projected.py injects memory per site reading the tag
  (`SiteTable.sites_reading(tag)`). Pilot stays tag-level; executed scans are ground truth.
- Migration: `migrate_legacy_edge_memory` maps `_prev:X` to every site reading X. Reload
  remaps by fingerprint, drops the rest.

## Live row solve

Smaller than FINDINGS estimated:
- Evaluate each branch's local contacts when the solve reaches it, not in the precomputed map.
- Read helper legs live when their instruction executes.
- `ConditionView` becomes a thin live proxy (drops the per-rung copy).
- `.continued()` becomes semantically an ordinary rung; snapshot special cases in codegen,
  classify, fold, pointer_sites deleted.
- No writes within one row, so live vs atomic differ only between rows.

Exactness: prover oracle is the compiled kernel -> exact once codegen changes; static rules
stay conservative (live reads are at least as fresh). HARD EXCEPTION: slice.py read
detection hooks `ConditionView`; under live reads it misses reads and concludes "written
before read" (unsound). It must move to the context read sink IN THE SAME COMMIT as the
runtime change, with pdg's prepass emitting reads in walk order. Fold checks each window
against a stepped landing scan; its guards only simplify.

Hard parts: causal capture/counterfactual rely on "entry" read origin; debugger/VS Code
show branch power at rung start; exporter `factor_outputs`; large test churn.

**Feasibility: go live.** Fall back if: test-soundness failures from read-position
assumptions beyond pdg/slice/classify; fold-check mismatches that declining can't fix;
`factor_outputs` can't keep row order without redesign; hardware shows box legs read at
rung entry. Even under fallback, S3 alone fixes `rise(X)` above X's writer.

## Serial landing order

| step | content | size | main files | parallel with |
|---|---|---|---|---|
| S0 | SiteTable + binding, prover keys from table, None/False normalization, reload remap. No behaviour change except possible new prover dims (soundness fix) | M ~300 LOC | site_memory, _program.py, runner, codegen context, prove passes/kernel, reload_console | S1 |
| S1 | False For skips body; count `n & 0xFFFF`; pdg.py:1204 stops treating For-body `out()` as following the rung | S-M | control.py, executor for-loop fns, debug_handlers, circuitpy For compile, pdg, classify:371 | S0 |
| S2 | Counters count edges; down Done `acc <= preset`; `advance_is_pulse=True`; remove fold counter sources + prover per-scan counter events | L | counters.py, _instructions_basic, fold:203-266, absorb, events, crossings, why, cmp_conditions | none |
| S3a | Per-site edge contacts in interpreter, codegen, replay; prover treats site memory as generic memory keys, edge optimizations off | XL | condition.py, rung.py, executor, runner, compiled_plc, kernel, circuitpy _core/render/render_kernel | none |
| S3b | Prover edge optimizations restored per site (incl. slice.py:583,721) | L | prove/* | S3c |
| S3c | Fold dormant edges + memory guard; causal projected; conformance seeds | M | fold, causal | S3b |
| S4a | Live solve runtime + slice.py hook + pdg prepass, one commit | L | rung, context, utils, executor, debugger, debug_handlers, counterfactual, _core, pdg, slice | none |
| S4b | Delete continued special cases; exporter factoring guard; docs | M | classify, fold, pointer_sites, _exporter, docs | none |

Each step green on make test, test-fold, test-soundness (+ test-pilot and tumbler for S2).
The order-dependence check should take read positions from pdg's walk and coordinate with S4a.

## Risks and open questions

Risks: prover counter progress now needs input edges (some proofs -> named Intractable);
make bench may regress until S3b; VS Code stepping protocol may change.

Hardware: edge memory scanned vs powered (edge contact behind open contact, in a branch of
a false rung); site memory/counters on power-up and STOP->RUN; For count 0; box legs solved
at the box's row, box below other outputs; rise() per-iteration in For bodies and on a
subroutine called twice in one scan; up+down edges same scan; edge on the reset scan;
down-counter start value (sign of Done threshold).

User: reload remap vs drop; shared condition clone vs raise; `.continued()` export-only
hint vs deprecate.

## S0 as landed: limitations S3a must address first (BOB, 2026-10-29)

S0 stamps site keys ON the shared objects (`instr._state_key`, `cond._site_key`). Combining
rungs into a new bare list renames clashing keys in place, so (a) a Program can change after
it is combined into a list (its own site table goes stale; generated names like
`_mb_client_logic_i1` depend on construction history), and (b) another live PLC holding the same
rung objects finds its memory under the old key. Harmless while edges are not evaluated per
site, but S3a stores edge memory under `e{N}`: **precondition for S3a: keep keys per PLC
(a key map owned by the runner/SiteTable), not stamped on shared objects**, or copy rungs on
combine. Also: a Program-owned object without memory can keep a key over a bare-list object
with memory (prefer any memory holder). Reload contact text renders Expr/Indirect contacts
as class names (coarse). Review log: pyrung-wt/buildbot/jobs/s0/REVIEW.md.
