# PILOT migration — PilotRungs, gauge vocabulary, HELD handshake, clean route

Shipyard rule applies to every phase: gate program first, born strict-xfail, flipped when
the mechanism lands. Renames kill the old words dead in the same commit — code, docstrings,
tests, CLAUDE.md vocabulary, handoff. No half-migrated vocabulary.

---

## Phase 1 — All holds are PilotRungs

Unify steady holds and `ConditionalHold` into one ordered, append-only list of rungs in the
synthesis holds overlay. Every rung is `PilotRung(dest, value, guard)` and **the guard is
required**. There is no unconditional form: steering without a condition that makes it yield
is a permanent force wearing ladder syntax. The proposer owns the guard and must state while
its evidence remains applicable. If it cannot, the intervention stays a one-scan patch/pulse
instead of becoming a rung. “Steady” describes a rung whose honest guard may remain true for
many scans, not a separate kind of hold. Refinement = append. **Last active rung wins** (scan
order — a later rung's write overwrites an earlier one's; a guarded later rung whose guard is
false leaves the earlier value standing).

Patch/pulse/rung boundary:
- A **patch** is consumed after one scan and overrides a pilot rung for that scan.
- A **pulse** is an edge-producing sequence of patches (release, assert, settle); ACT owns it.
- A **pilot rung** is per-scan reactive steering and must carry its own release condition.
- A button press is a pulse, never a rung. A value that must survive several scans is a rung,
  but only when its proposer can name the condition under which it remains justified.
- Pilot owns the simulated physical-input image. Lowering first writes each managed Boolean
  `False`, then applies guarded PilotRungs in append order. Thus no active Boolean rung means
  the simulated input turns off; an inactive later rung performs no write and cannot erase an
  active general rung; the last active write wins. Non-Boolean inputs retain their prior or
  plant-supplied value when no rung is active.

Deletes: `_merge_hold`, `_split_holds`, every `isinstance(val, ConditionalHold)` branch, the
`forced_holds` dict registry, `hold_defeats_needed` release machinery (superseded by
counter-rungs, Phase 3 — see Q1.6).

Renames in `_ops.py`: `ConditionalHold` → `PilotRung`, `_HoldRule` → `_RungRule`,
`_install_holds` → `_append_rungs`, `_sync_holds` → `_set_rungs`,
`fork_with_holds` → `fork_with_rungs`. State field `forced_holds` → `rungs`.

Semantics to pin (each wants a test):
- **Last-wins replaces first-wins** for steady holds (currently `elif hold_tag not in
  forced_holds` protects the first value). Latest evidence wins everywhere now.
- **Guard snapshot discipline**: pilot rungs are conceptual records lowered as ordered
  branches of **one** synthesis rung. Every guard therefore reads the same pre-overlay
  snapshot, never an earlier pilot rung's write; active branch writes still execute in list
  order, so last-wins means supersession, not chaining. Separate synthesis rungs do not have
  this property and are not a valid lowering.
- **Install always** (no more coast-only conditional install): scope through guards, not
  install sites. The `cycle_fold_until` vs `run_until(fold=True)` branch survives as a
  property of the installed list, not an isinstance test.
- **Checkpoint = rung-list cursor**: checkpoints record list length; revert = truncate.
  Rungs appended during a failed leg vanish with the revert.
- **Order is semantics now**: traces serialize the rung list; investigation replay rebuilds
  it verbatim.
- Keep: the no-dest force fallback in `_set_rungs` (the one non-rung escape hatch), the
  avoid/`_hold_allowed` gates (evaluate each rung's driven value, per rule as now).

Gate: miniature door-cycle program — earned unguarded rung, later guarded counter-rung wins
during its window, yields after, gauge advances.

**Q1.1** Last-wins flip: any current behavior *relying* on first-wins protection of an early
steady hold? (Audit `_install_holds` call sites before assuming no.)
**Q1.2 — decided:** canonical precedence remains `pilot rung < patch < force`. During
`_apply_pulse`, release/assert patches win their individual scans and an applicable rung
reasserts on the next unpatched scan. Pin this with a test.
**Q1.3** Fold-strategy predicate: is it "any *guarded* rung present → cycle_fold", or only
"any multi-rule oscillator"? A single guarded counter-rung is not an oscillation — does
`run_until(fold=True)` remain safe under it? Decide, document, measure.
**Q1.4** Dead-shadow accumulation: accept unbounded (compaction is a later optimization,
never semantics), or cap with exact tag+guard subsumption pruning now?
**Q1.5** Every append invalidates fold/replay caches (`_set_synth_holds` already nukes
them) — confirm per-round recompile cost is acceptable at burner scale.
**Q1.6** Delete `hold_defeats_needed` in this phase, or leave it in place until Phase 3
proves counter-rungs cover its cases? (Recommend: leave, delete in Phase 3.)

---

## Phase 2 — Renames (rides Phase 1's commit where files overlap)

**credential.py → gauge.py**
- `CredentialCut` → `Gauge`, `CredentialComponent` → `GaugeComponent`,
  `build_credential_cut` → `build_gauge`
- `_EraseWriter` → `_ResetWriter`, `.erasers` → `.resets`, "eraser" → "reset" everywhere
- `mark()` / `compare()` / `ordinal_advanced()` keep their names
- Internals (`_discrete_condition`, `_self_limiting_advance`, `_tag_coupled`,
  `_classify_stride_tag`, `_stepper_shapes`, `_literal_or_affine_write`) untouched —
  they describe construction, not metaphor. Logic changes are out of scope for this phase.
- `_pilot_state_key` is the *search* key, not the gauge — untouched.

**detour.py**
- `DetourLoan` → `Detour`, `loan_signature` → `detour_signature`
- `anchor_mark` → `gauge_at_departure`, `bailout_len` → `pre_detour_checkpoint_len`
- `_eraser_blocked_values` → `_reset_blocked_values`
- **road → route everywhere** (there are no roads in the ocean): `_clean_road` →
  `_clean_route`, `DepartureVerdict.road` → `.route`, "clean forward road" reason string →
  "clean forward route", docstring "roads back" → "routes back"
- Docstring drops loan/credit/settle/bailout for: a stopover is provisional — at rejoin
  the detour **worked** (gauge advanced → checkpoint) or **failed** (revert, remember the
  signature, re-ejection classifies as regression).
- `_settle_departure` simplifies to `fork_with_rungs` + stability loop (Phase 1 dependency).

**State fields**: `credential_cut` → `gauge`, `failed_loans` → `failed_detours`.

**Events**: `detour_promoted` → `detour_worked`; add `detour_failed`. Hard switch — update
every probe/gate that greps the old name in the same commit (handoff lists them).

**Docs**: CLAUDE.md vocabulary gains **rung** (a `PilotRung`; guard=None = unconditional;
append-only, last wins) and **gauge** (the progress instrument, two families, fails to
`unknown`); **hold** entry retired. Add the overfit honesty line: *the gauge currently
recognizes the two clue shapes one machine taught it (hop=1 alias resolution,
`_RELAY_DEPTH=3`, ordinal+stepper); elsewhere it says unknown.*

**Q2.1** Keep the filename `detour.py` (yes, presumably) and the `DepartureVerdict` name?
**Q2.2** Any external scratchpad probes beyond the handoff's receipts list that grep old
event names? Grep `scratchpad/` before the switch.

---

## Phase 3 — HELD handshake on the new floor

Land the detour as the reactive mechanism: the pilot was headed for Completing, the program
had different ideas, and we got bumped to HELD. The detour classifier correctly identifies
it as a stopover, the gauge proves nothing was lost. Now make the handshake actually
complete so the detour works.

The frontier: 105→107 needs a door cycle (`x_DoorClosed` False→True at HoldForShine) but
`x_DoorClosed=True` is an earned rung; then `C_Unhold` (the clean route's own edge action).

Mechanism: **append a counter-rung** — drive `x_DoorClosed=False` while ⟨guard⟩ — which wins
while active and lets the earned rung reassert after: the cycle, self-releasing,
self-restoring. No release machinery borrowed into the stopover path. Then press `C_Unhold`.

Done means (from handoff): `how(S_StateCurrent==17, avoid=C_Complete)` reaches 17 on the
bench recipe; the detour **works** at the Execute rejoin (`detour_worked` in the stream);
never presses `C_Complete`.

**Q3.1** Counter-rung guard: what condition, computed by whom? Options: the channel context
(`S_StateCurrent == HoldForShine`), or the frontier-need condition itself. Who proposes —
trace (it owns frontier needs) or the detour route's edge requirements?
**Q3.2** Is the door cycle a counter-rung at all, or a pulse through steer.py (patch False,
step, release)? A pulse keeps ACT the owner of presses; a rung survives coasts. The R18
rising edge must actually fire — which form guarantees the edge is seen?
**Q3.3** `C_Unhold` source: currents.py (one legal button for this state) or the route's
edge action recorded by `_clean_route` (né `_clean_road`)? Handoff says currents territory —
confirm, and decide whether `_clean_route` should start carrying edge actions on the route
(it currently
stores values only).
**Q3.4** Delete `hold_defeats_needed` / self-defeat-release here once the gate passes?
(`test_pilot_self_defeating.py` must survive on the counter-rung mechanism.)

---

## Phase 4 — Clean route forward

Phase 3 landed the reactive case: the program bumped us, we classified it, the detour
worked. Now fix the forward-looking logic so the pilot doesn't need to get bumped. For this
program, with `avoid C_Complete`, HELD is the only surviving route — the pilot should already
see that and take it deliberately.

The pilot should always know the plausible routes — reading the charts, consulting the
compass, maintaining awareness of what's open and what's closing, the way a navigator does.
Today it only reads routes reactively, after a departure already happened. The concrete bug:
the charts say "you can use C_Complete!" but the captain said "no, don't go that way" — and
the pilot keeps trying to coast from Execute to Completing as though that route is still
open. It never re-reads the charts with the avoid constraint applied, so it never discovers
the only surviving route to 17 runs through HELD. It has to get bumped there before it
notices.

- Extract the route query from `detour.py`'s `_clean_route` + `_reset_blocked_values` into a
  read-side module (static reader: graph BFS + gauge resets, runs nothing; `WalkContext`
  in signature; lower layers only). detour.py becomes its reactive caller.
- The bearing (recomputed every iteration, per doctrine) includes which routes survive the
  avoid constraint *right now*. When avoid closes a route, the bearing shifts to what's left.
  No stored waypoints, no plan to go stale — just the chart re-read with the constraint
  applied.
- Ejection guard corollary: a departure landing on the surviving route isn't an incident —
  no classify/settle dance; gauge receipt still taken as a sanity check.
- `Detour` gains `initiated: bool` for the transcript (purposeful vs bumped) — record,
  signature memory, worked/failed settle identical.

Gate: small sequencer, direct edge avoided, only route loops through a side state — pilot
reads the chart, sees the surviving route, takes it deliberately.

**Q4.1** Module home and name for the route query — new file (`routes.py`?) or into
`evidence.py`/`charts.py`? It reads compass graphs *and* the gauge, which neither currently
imports.
**Q4.2** Re-read trigger: the route query is a read-side instrument, so it can run every
ORIENT without violating one-owner (route choice is still `_prepare_route`'s; the query is
just a chart the bearing consults). Confirm this doesn't change ORIENT's cost profile
unacceptably.
**Q4.3** When the bearing shifts mid-run (a route closes that was open last iteration), is
that a compass observation or a silent re-read? (Lean: silent — the chart didn't change,
the constraint was always there, we just hadn't looked.)
**Q4.4** Does the surviving route through HELD change avoid-gate bookkeeping (the route's own
edge presses must still pass the three avoid gates)?

---

## Phase 5 — Other programs

Most target programs are more sequential than burner: pure-stepper gauges (well covered),
fewer program-initiated exits (detours rarer). The gauge's burner-shaped constants stay
untouched until a second machine's gate demands otherwise.

- Gates to write (strict-xfail where v1 honestly loses a stopover): PackML-style sequencer;
  a wrapping stepper (cycle-end `:= 0` is earned progress, v1 calls it a reset); a two-hop
  alias program (v1's hop=1 leaves resets unresolved → everything regresses).
- Re-baseline `repro_regression.py` (RED on clean HEAD, pre-existing) before trusting it as
  a gate for any of this work.

**Q5.1** Which second program first — do you have a real one, or build the PackML gate
synthetically?
**Q5.2** The cyclefold frontier (0 folds in every HELD-era coast) is independent of this
migration but Phase 1's fold-strategy predicate touches the same seam — same branch or
separate? (Recommend: separate; `probe_cyclefold.py` is the starting point either way.)

---

## Standing context

- Kept program bug (heat.py R5 pre-empts R14) stays as the stall-honesty bed — untouched
  until Phase 3 and folding land.
- Uncommitted in-tree work not ours: `circuitpy/codegen/compile/_core.py` snapshot fix +
  `test_compiled_replay.py`, two ruff joins in investigate.py/steer.py.
- Regen gotcha: re-export can drop init logic; canaries `probe_boot_state.py`
  (`S_P6_HeatMaxRetry == 1`), backup at `scratchpad/pyrung_project_preedit`.
