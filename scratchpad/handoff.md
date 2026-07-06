# PILOT generalization — handoff (2026-07-06)

State of the "genuinely generalized pilot" effort. Completed work is committed
on `dev`; this file covers what remains, with enough context to resume cold.

## Where things stand (committed)

Full suite green (4786 passed / 34 skipped / 7 xfailed at `ff5b614`), `make lint`
clean. Tasks 5, 6, and 8 landed 2026-07-06 (via parallel Opus agents):

- `bd00f36` — **Task 5**: fire-time pins for non-affine calc decodes.
  `_transition_fire_pins` (trace.py) gains an `env`-threaded fallback after
  `calc_source_binding` punts; new `solve_calc_preimage` (table_oracle.py)
  solves `calc_expr(srcs…) == value` over complete finite domains and pins
  only FORCED source values (projection shared by every satisfying assignment).
  Affine/copy path byte-identical; punts on live operands / incomplete domains
  / `_MAX_FREE_INDICES`+`_MAX_COMBOS` guardrails.
- `4c6677b` — **Task 6**: route machinery for word-valued and `Bool==False`
  targets. `enumerate_trace_choices` was already value-generic; the only gate
  was `_prepare_route`'s `_target_is_bool_true` → replaced with
  `_target_is_value_route` (admits any concrete equality target, excludes live
  relational predicates). Multi-target `avoid`/`via` ValueError lifted entirely
  (the predicate is over tag values, target-agnostic); avoid gate now enforced
  during multi-drive.
- `ff5b614` — **Task 8**: corrections menu opened to any implicated writer.
  `_implicated_writers` dispatches via `plc.cause` + `_can_produce`
  producibility gate (not the old ResetInstruction class list); suppression
  reuses `_best_forcing_holds` with inverted polarity (`break_guard_holds`,
  `satisfies=not v`); live-word guards escalate to `run_pinned_scan` skiff
  nominations, all confirmed through investigate's existing replay gate. Menu
  vocabulary stays FLIP/FREEZE/OSCILLATE.

Prior waves (all on `dev`):

- `e49b669`..`019fd27` — first sweep: `_TT` via accumulating profile,
  blast-radius drop → ordering tier, semantic journal labels, Bool-domain
  closure in `guard_satisfiable`, sandbox split, calc-source unification,
  fire-time-pin oracle trigger, skiff boundary gates, CLAUDE.md.
- `8a60302` — crossings inverts affine expression-source copies (exact in the
  clamp interior, rail punts; shared with the prover — test-prove green).
- `ebda5ca` — set-valued guard-lattice coil aliases; `guard_satisfiable`
  rejection arm wired into `_trace_back` writer admission (three-valued
  `guard_verdict`; DEAD only over complete domains; `TraceNode.live_guard`
  punt signal).
- `814511e` — corrections handle multi-read guards: prime-implicant
  enumeration over read domains, coordinated multi-lever holds, honest
  declines.
- `e1b532b` — **the skiff is wired**: `probe_live_guard_frontiers` at both
  stuck exits; singles→pairs isolated probes on condition-read levers;
  composite causes → `prescribed_batch` batch trials; `Compass.contradict`
  falsifies stale seeded edges. The command-selected-mask boundary gate
  (`tests/core/analysis/test_pilot_sandbox_gate.py`) passes end to end.

Invariants that must survive any further work:
- Skiff results only ever feed `Compass.record` — bearings, never plan steps;
  the verify pipeline is the sole source of confirmed edges.
- Enumeration rejects/pins only over COMPLETE finite domains (nd_domains,
  declared choices, Bool); plausible-value fallbacks never reject.
- Punt on anything unreadable; never fabricate an edge or an alias.

## Tasks 5, 6, 8 — DONE (2026-07-06)

Landed as `bd00f36` / `4c6677b` / `ff5b614` — see "Where things stand" above.
Design note surfaced during Task 8: the static forcing enumerator is more
capable than this handoff assumed — `_guard_operand_domain` falls back to a
current-value singleton for free words — so the skiff-escalation boundary is
narrower than "any word guard"; it triggers specifically on calc/opaque
operands whose finite domain is unreadable. Task 7's `_minimal_forcing_sets` /
`_best_forcing_holds` are the shared forcing machinery Task 8 reused
(inverted-polarity via `break_guard_holds`).

## Task 9 — long tail (fold in opportunistically)

- **`compute_reference_constants` misses indirectly-read table rows** — a
  never-written ds/dh slot read only via `ds[computed]` classifies steerable.
  The skiff filters these by condition-read; the proper fix is in
  ref_consts' read-set walk. (Found live: the skiff was probing
  `cfg_prod_row=-1`.)
- Drums: return an `accumulating_profile()`; pass `fork=`/`harness` at
  corrections.py's `scans_to_eject`/`iter_profiles` call sites so Tier-2
  empirical `measure_scans` is reachable.
- `_resolve_inequality_target` strict-inequality `±1` step is wrong for Real
  tags / non-unit domains — make it domain/epsilon-aware.
- `_counter_driver_leaf` enumerates only Bool advance candidates — solve int
  advance conditions over declared domains.
- `_scan_transient_rest` multi-scope producer variants fall through.
- `_rewrite_internal_compare` requires exactly one Cmp per branch.
- `table_from_indirect_src` 3-hop pointer chase proceeds on a partial
  `eval_addr` at hop 4 instead of punting cleanly.
- Free-word skiff tier (strict xfail `test_skiff_boundary_gate_live_mask_guard`
  in test_pilot_sandbox_gate.py). **Reframed 2026-07-06 — NOT a value-synthesis
  instrument.** The free word (`CfgWord`, an unconstrained `external` Int with no
  `choices=`) has no complete domain, which is *why* every static layer punts.
  The honest resolution is to decline and point the captain at the fix, not to
  synthesize a bitwise-complement value (that would be PILOT guessing the
  captain's intent — the one thing the design forbids). Deliverable:
  1. **Actionable decline.** `how()` already declines with a `path.reason`
     (`test_unreachable_today_fails_honestly`); make that reason *name the
     unconstrained external word(s) feeding the guard* and nudge "declare
     `choices=`/a domain". No synthesis.
  2. **`choices=` proof test.** Same `_live_mask_program`, add `choices=` to
     `CfgWord`; assert `how()` now reaches it with **no new instrument** — the
     declared domain propagates through `copy(CfgWord, DisabledMask)` and Task 5's
     `solve_calc_preimage` + guard enumeration resolve `StateMask & DisabledMask
     == 0`. This is the thesis: give it a domain and the existing machinery works.
  3. **Verify the establish-sequence half.** The old xfail reason cited two
     blockers — value synthesis *and* establish-staged sequences (set word →
     pulse `CfgLoad` → command). The reframe dissolves the value half; confirm
     (don't assume) that the load-before-command sequence falls out of trace's
     establish/preserve pipeline once the value is known. If it doesn't, that
     staging — not synthesis — is the real remaining work.
  - **Design decision (choices= location):** the fix is the **tag annotation**
    (`Int("CfgWord", external=True, choices=...)`), the single source of truth the
    prover / bounds / validators / sandbox all read — never a `how()`-only
    override, which would split PILOT's domain from the prover's (violating
    prove-agreement) and assert a plan premise absent from the program. A per-query
    hint, IF ever added, must be a clearly-labeled hypothesis (`assume_domains=`,
    Plan records "supplied not declared", never shadows a declared domain) — a
    scaffold toward the annotation, added only if annotate-and-rerun proves too slow.
  - Sequenced AFTER the Tier-1/2 trace.py batch (both edit trace.py / the gate).

## Working agreements

- Never `git stash`; stage by explicit path (other agents may share the tree).
- `make test-pilot` (~7s) for the loop; `uv run pytest tests/core/analysis/`
  for the subsystem; full `make test` + `make lint` before committing anything
  touching core/instruction or crossings (crossings is shared with the
  prover — run `make test-prove` too).
- Conventional Commits; end with `Co-Authored-By: Claude Fable 5
  <noreply@anthropic.com>`.
- The boundary gates are the acceptance discipline: every new instrument or
  tier gets a hand-driveable, statically-punting, honestly-failing gate
  program *before* the wiring, and a strict xfail as the tripwire.
